"""Tenant-scoped confirmation records for built-in product policy templates."""

from __future__ import annotations

import os
from copy import deepcopy
from threading import Lock

from .postgres import auto_schema_enabled, connect
from .rls import apply_tenant_rls
from .time_utils import now_iso


class InMemoryPolicyConfirmationStore:
    """Process-local policy confirmations used by local and test deployments."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._records: dict[tuple[str, str], dict[str, str]] = {}

    def confirm(
        self,
        *,
        tenant_id: str,
        category: str,
        policy_id: str,
        confirmed_by: str,
    ) -> dict[str, str]:
        """Upsert one tenant's confirmation of the current registered template."""
        record = _confirmation_record(
            tenant_id=tenant_id,
            category=category,
            policy_id=policy_id,
            confirmed_by=confirmed_by,
        )
        with self._lock:
            self._records[(record["tenant_id"], record["category"])] = record
        return deepcopy(record)

    def list(self, tenant_id: str) -> list[dict[str, str]]:
        """List only the requested tenant's confirmations."""
        tenant = _simple_identifier(tenant_id, field="tenant_id", max_length=128)
        with self._lock:
            records = [
                deepcopy(record)
                for (record_tenant, _), record in self._records.items()
                if record_tenant == tenant
            ]
        return sorted(records, key=lambda item: item["category"])

    def clear(self) -> None:
        """Clear process-local records between tests."""
        with self._lock:
            self._records.clear()


class PostgresPolicyConfirmationStore:
    """Durable confirmations protected by Postgres tenant row-level security."""

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresPolicyConfirmationStore")
        self._database_url = database_url
        if auto_schema_enabled():
            self._ensure_schema()

    def confirm(
        self,
        *,
        tenant_id: str,
        category: str,
        policy_id: str,
        confirmed_by: str,
    ) -> dict[str, str]:
        record = _confirmation_record(
            tenant_id=tenant_id,
            category=category,
            policy_id=policy_id,
            confirmed_by=confirmed_by,
        )
        with connect(self._database_url, tenant_id=record["tenant_id"]) as conn:
            conn.execute(
                """
                insert into shelfwise_policy_confirmations (
                    tenant_id, category, policy_id, confirmed_by, confirmed_at
                )
                values (%s, %s, %s, %s, %s)
                on conflict (tenant_id, category) do update
                set policy_id = excluded.policy_id,
                    confirmed_by = excluded.confirmed_by,
                    confirmed_at = excluded.confirmed_at
                """,
                (
                    record["tenant_id"],
                    record["category"],
                    record["policy_id"],
                    record["confirmed_by"],
                    record["confirmed_at"],
                ),
            )
            conn.commit()
        return record

    def list(self, tenant_id: str) -> list[dict[str, str]]:
        tenant = _simple_identifier(tenant_id, field="tenant_id", max_length=128)
        with connect(self._database_url, tenant_id=tenant) as conn:
            rows = conn.execute(
                """
                select tenant_id, category, policy_id, confirmed_by, confirmed_at
                from shelfwise_policy_confirmations
                where tenant_id = %s
                order by category
                """,
                (tenant,),
            ).fetchall()
        return [
            {
                "tenant_id": str(row["tenant_id"]),
                "category": str(row["category"]),
                "policy_id": str(row["policy_id"]),
                "confirmed_by": str(row["confirmed_by"]),
                "confirmed_at": row["confirmed_at"].isoformat(),
            }
            for row in rows
        ]

    def clear(self) -> None:
        with connect(self._database_url) as conn:
            conn.execute("delete from shelfwise_policy_confirmations")
            conn.commit()

    def _ensure_schema(self) -> None:
        with connect(self._database_url) as conn:
            conn.execute(_POLICY_CONFIRMATION_SCHEMA_SQL)
            apply_tenant_rls(conn, ("shelfwise_policy_confirmations",))
            conn.commit()


def create_policy_confirmation_store(
) -> InMemoryPolicyConfirmationStore | PostgresPolicyConfirmationStore:
    """Create the configured durable or local policy-confirmation store."""
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemoryPolicyConfirmationStore()
    if backend == "postgres":
        return PostgresPolicyConfirmationStore(os.getenv("DATABASE_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_STORE_BACKEND: {backend}")


def _confirmation_record(
    *,
    tenant_id: str,
    category: str,
    policy_id: str,
    confirmed_by: str,
) -> dict[str, str]:
    return {
        "tenant_id": _simple_identifier(tenant_id, field="tenant_id", max_length=128),
        "category": _simple_identifier(category.lower(), field="category", max_length=40),
        "policy_id": _simple_identifier(policy_id, field="policy_id", max_length=96),
        "confirmed_by": _simple_identifier(
            confirmed_by, field="confirmed_by", max_length=128
        ),
        "confirmed_at": now_iso(),
    }


def _simple_identifier(value: str, *, field: str, max_length: int) -> str:
    cleaned = value.strip()
    if (
        not cleaned
        or len(cleaned) > max_length
        or not cleaned.replace("_", "").replace("-", "").isalnum()
    ):
        raise ValueError(f"{field} must be a simple identifier")
    return cleaned


_POLICY_CONFIRMATION_SCHEMA_SQL = """
create table if not exists shelfwise_policy_confirmations (
    tenant_id text not null,
    category text not null,
    policy_id text not null,
    confirmed_by text not null,
    confirmed_at timestamptz not null,
    primary key (tenant_id, category)
);
"""
