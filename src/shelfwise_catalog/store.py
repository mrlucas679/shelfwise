from __future__ import annotations

import os
from copy import deepcopy
from threading import Lock
from typing import Any

from shelfwise_storage import auto_schema_enabled, connect, jsonb
from shelfwise_storage.rls import apply_tenant_rls

from .models import Product, ProductIdentifier, ProductVariant


class ConflictingIdentifierError(ValueError):
    """Raised when an identifier is already mapped to a different variant."""


class InMemoryProductCatalogStore:
    """Product/variant identity resolution for the demo and test in-memory backend."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._products: dict[tuple[str, str], dict[str, Any]] = {}
        self._variants: dict[tuple[str, str], dict[str, Any]] = {}
        # (tenant_id, kind, value) -> variant_id
        self._identifiers: dict[tuple[str, str, str], str] = {}

    def upsert_product(self, product: Product) -> dict[str, Any]:
        with self._lock:
            payload = product.to_dict()
            self._products[(product.tenant_id, product.product_id)] = payload
            return deepcopy(payload)

    def upsert_variant(self, variant: ProductVariant) -> dict[str, Any]:
        with self._lock:
            payload = variant.to_dict()
            self._variants[(variant.tenant_id, variant.variant_id)] = payload
            return deepcopy(payload)

    def upsert_identifier(self, identifier: ProductIdentifier) -> dict[str, Any]:
        with self._lock:
            key = (identifier.tenant_id, identifier.kind, identifier.value)
            existing_variant_id = self._identifiers.get(key)
            if existing_variant_id is not None and existing_variant_id != identifier.variant_id:
                raise ConflictingIdentifierError(
                    f"{identifier.kind}={identifier.value!r} is already mapped to variant "
                    f"{existing_variant_id!r}, not {identifier.variant_id!r} - resolve the "
                    "conflict explicitly (human review) instead of silently overwriting it"
                )
            self._identifiers[key] = identifier.variant_id
            return identifier.to_dict()

    def resolve_identifier(self, *, tenant_id: str, kind: str, value: str) -> dict[str, Any] | None:
        """Return the variant a (kind, value) identifier resolves to, or None."""
        with self._lock:
            variant_id = self._identifiers.get((tenant_id, kind, value))
            if variant_id is None:
                return None
            variant = self._variants.get((tenant_id, variant_id))
            return deepcopy(variant) if variant else None

    def get_product(self, *, tenant_id: str, product_id: str) -> dict[str, Any] | None:
        with self._lock:
            payload = self._products.get((tenant_id, product_id))
            return deepcopy(payload) if payload else None

    def list_products(self, *, tenant_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                deepcopy(payload)
                for (tid, _pid), payload in self._products.items()
                if tid == tenant_id
            ]

    def list_variants(
        self, *, tenant_id: str, product_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            return [
                deepcopy(payload)
                for (tid, _vid), payload in self._variants.items()
                if tid == tenant_id
                and (product_id is None or payload.get("product_id") == product_id)
            ]

    def list_identifiers(self, *, tenant_id: str, variant_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"tenant_id": tid, "kind": kind, "value": value, "variant_id": mapped_id}
                for (tid, kind, value), mapped_id in self._identifiers.items()
                if tid == tenant_id and mapped_id == variant_id
            ]

    def clear(self) -> None:
        with self._lock:
            self._products.clear()
            self._variants.clear()
            self._identifiers.clear()


class PostgresProductCatalogStore:
    """Product/variant identity resolution backed by Postgres, tenant-RLS scoped."""

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresProductCatalogStore")
        self._database_url = database_url
        if auto_schema_enabled():
            self._ensure_schema()

    def upsert_product(self, product: Product) -> dict[str, Any]:
        payload = product.to_dict()
        with self._connect(product.tenant_id) as conn:
            conn.execute(
                """
                insert into shelfwise_products (tenant_id, product_id, payload)
                values (%s, %s, %s)
                on conflict (tenant_id, product_id) do update set payload = excluded.payload
                """,
                (product.tenant_id, product.product_id, jsonb(payload)),
            )
            conn.commit()
        return payload

    def upsert_variant(self, variant: ProductVariant) -> dict[str, Any]:
        payload = variant.to_dict()
        with self._connect(variant.tenant_id) as conn:
            conn.execute(
                """
                insert into shelfwise_product_variants
                    (tenant_id, variant_id, product_id, payload)
                values (%s, %s, %s, %s)
                on conflict (tenant_id, variant_id) do update set payload = excluded.payload
                """,
                (variant.tenant_id, variant.variant_id, variant.product_id, jsonb(payload)),
            )
            conn.commit()
        return payload

    def upsert_identifier(self, identifier: ProductIdentifier) -> dict[str, Any]:
        with self._connect(identifier.tenant_id) as conn:
            existing = conn.execute(
                """
                select variant_id from shelfwise_product_identifiers
                where tenant_id = %s and kind = %s and value = %s
                """,
                (identifier.tenant_id, identifier.kind, identifier.value),
            ).fetchone()
            if existing is not None and existing["variant_id"] != identifier.variant_id:
                raise ConflictingIdentifierError(
                    f"{identifier.kind}={identifier.value!r} is already mapped to variant "
                    f"{existing['variant_id']!r}, not {identifier.variant_id!r} - resolve the "
                    "conflict explicitly (human review) instead of silently overwriting it"
                )
            conn.execute(
                """
                insert into shelfwise_product_identifiers
                    (tenant_id, kind, value, variant_id, source_system)
                values (%s, %s, %s, %s, %s)
                on conflict (tenant_id, kind, value) do update
                set variant_id = excluded.variant_id, source_system = excluded.source_system
                """,
                (
                    identifier.tenant_id,
                    identifier.kind,
                    identifier.value,
                    identifier.variant_id,
                    identifier.source_system,
                ),
            )
            conn.commit()
        return identifier.to_dict()

    def resolve_identifier(self, *, tenant_id: str, kind: str, value: str) -> dict[str, Any] | None:
        with self._connect(tenant_id) as conn:
            row = conn.execute(
                """
                select v.payload
                from shelfwise_product_identifiers i
                join shelfwise_product_variants v
                    on v.tenant_id = i.tenant_id and v.variant_id = i.variant_id
                where i.tenant_id = %s and i.kind = %s and i.value = %s
                """,
                (tenant_id, kind, value),
            ).fetchone()
        return deepcopy(row["payload"]) if row else None

    def get_product(self, *, tenant_id: str, product_id: str) -> dict[str, Any] | None:
        with self._connect(tenant_id) as conn:
            row = conn.execute(
                "select payload from shelfwise_products where tenant_id = %s and product_id = %s",
                (tenant_id, product_id),
            ).fetchone()
        return deepcopy(row["payload"]) if row else None

    def list_products(self, *, tenant_id: str) -> list[dict[str, Any]]:
        with self._connect(tenant_id) as conn:
            rows = conn.execute(
                "select payload from shelfwise_products where tenant_id = %s order by product_id",
                (tenant_id,),
            ).fetchall()
        return [deepcopy(row["payload"]) for row in rows]

    def list_variants(
        self, *, tenant_id: str, product_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._connect(tenant_id) as conn:
            if product_id is None:
                rows = conn.execute(
                    """
                    select payload from shelfwise_product_variants
                    where tenant_id = %s order by variant_id
                    """,
                    (tenant_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    select payload from shelfwise_product_variants
                    where tenant_id = %s and product_id = %s order by variant_id
                    """,
                    (tenant_id, product_id),
                ).fetchall()
        return [deepcopy(row["payload"]) for row in rows]

    def list_identifiers(self, *, tenant_id: str, variant_id: str) -> list[dict[str, Any]]:
        with self._connect(tenant_id) as conn:
            rows = conn.execute(
                """
                select tenant_id, kind, value, variant_id
                from shelfwise_product_identifiers
                where tenant_id = %s and variant_id = %s
                order by kind, value
                """,
                (tenant_id, variant_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def clear(self) -> None:
        with self._connect(None) as conn:
            conn.execute("delete from shelfwise_product_identifiers")
            conn.execute("delete from shelfwise_product_variants")
            conn.execute("delete from shelfwise_products")
            conn.commit()

    def _ensure_schema(self) -> None:
        with self._connect(None) as conn:
            conn.execute(
                """
                create table if not exists shelfwise_products (
                    tenant_id text not null,
                    product_id text not null,
                    payload jsonb not null,
                    primary key (tenant_id, product_id)
                )
                """
            )
            conn.execute(
                """
                create table if not exists shelfwise_product_variants (
                    tenant_id text not null,
                    variant_id text not null,
                    product_id text not null,
                    payload jsonb not null,
                    primary key (tenant_id, variant_id)
                )
                """
            )
            conn.execute(
                """
                create index if not exists idx_shelfwise_product_variants_product
                on shelfwise_product_variants (tenant_id, product_id)
                """
            )
            conn.execute(
                """
                create table if not exists shelfwise_product_identifiers (
                    tenant_id text not null,
                    kind text not null,
                    value text not null,
                    variant_id text not null,
                    source_system text,
                    primary key (tenant_id, kind, value)
                )
                """
            )
            conn.execute(
                """
                create index if not exists idx_shelfwise_product_identifiers_variant
                on shelfwise_product_identifiers (tenant_id, variant_id)
                """
            )
            apply_tenant_rls(
                conn,
                (
                    "shelfwise_products",
                    "shelfwise_product_variants",
                    "shelfwise_product_identifiers",
                ),
            )
            conn.commit()

    def _connect(self, tenant_id: str | None) -> Any:
        return connect(self._database_url, tenant_id=tenant_id)


def create_product_catalog_store() -> InMemoryProductCatalogStore | PostgresProductCatalogStore:
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemoryProductCatalogStore()
    if backend == "postgres":
        return PostgresProductCatalogStore(os.getenv("DATABASE_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_STORE_BACKEND: {backend}")
