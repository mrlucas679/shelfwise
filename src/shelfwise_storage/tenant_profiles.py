from __future__ import annotations

import os
from copy import deepcopy
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from .postgres import auto_schema_enabled, connect, jsonb
from .rls import apply_tenant_rls

DEFAULT_BUDGETS = {
    "daily_request_limit": 500,
    "monthly_token_limit": 2_000_000,
    "max_cascade_tokens": 24_000,
}
DEFAULT_MODEL_LIMITS = {
    "routine_tier": "small",
    "strong_tier": "strong",
    "allow_external_models": False,
}
DEFAULT_CONNECTOR_POLICY = {
    "mode": "read_only",
    "write_back": "hitl_required",
    "allowed_systems": ["csv"],
}


class InMemoryTenantProfileStore:
    """Process-local tenant onboarding/profile store."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._profiles: dict[str, dict[str, Any]] = {}

    def get(self, tenant_id: str) -> dict[str, Any] | None:
        with self._lock:
            profile = self._profiles.get(_clean_tenant_id(tenant_id))
            return deepcopy(profile) if profile is not None else None

    def upsert(self, profile: dict[str, Any]) -> dict[str, Any]:
        record = _profile_record(profile, existing=self.get(str(profile.get("tenant_id") or "")))
        with self._lock:
            self._profiles[record["tenant_id"]] = record
            return deepcopy(record)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            profiles = list(self._profiles.values())
        profiles.sort(key=lambda item: str(item.get("tenant_id", "")))
        return [deepcopy(profile) for profile in profiles]

    def clear(self) -> None:
        with self._lock:
            self._profiles.clear()


class PostgresTenantProfileStore:
    """Postgres-backed tenant profile store using shelfwise_business_profile."""

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresTenantProfileStore")
        self._database_url = database_url
        if auto_schema_enabled():
            self._ensure_schema()

    def get(self, tenant_id: str) -> dict[str, Any] | None:
        tenant = _clean_tenant_id(tenant_id)
        with self._connect(tenant) as conn:
            row = conn.execute(
                """
                select payload
                from shelfwise_business_profile
                where tenant_id = %s
                """,
                (tenant,),
            ).fetchone()
        return deepcopy(row["payload"]) if row else None

    def upsert(self, profile: dict[str, Any]) -> dict[str, Any]:
        tenant_id = _clean_tenant_id(str(profile.get("tenant_id") or ""))
        record = _profile_record(profile, existing=self.get(tenant_id))
        with self._connect(tenant_id) as conn:
            conn.execute(
                """
                insert into shelfwise_business_profile (tenant_id, payload, updated_at)
                values (%s, %s, %s)
                on conflict (tenant_id) do update
                set payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (tenant_id, jsonb(record), record["updated_at"]),
            )
            conn.commit()
        return self.get(tenant_id) or record

    def list(self) -> list[dict[str, Any]]:
        with self._connect(None) as conn:
            rows = conn.execute(
                """
                select payload
                from shelfwise_business_profile
                order by tenant_id
                """
            ).fetchall()
        return [deepcopy(row["payload"]) for row in rows]

    def clear(self) -> None:
        with self._connect(None) as conn:
            conn.execute("delete from shelfwise_business_profile")
            conn.commit()

    def _ensure_schema(self) -> None:
        with self._connect(None) as conn:
            conn.execute(_TENANT_PROFILE_SCHEMA_SQL)
            apply_tenant_rls(conn, ("shelfwise_business_profile",))
            conn.commit()

    def _connect(self, tenant_id: str | None) -> Any:
        return connect(self._database_url, tenant_id=tenant_id)


def create_tenant_profile_store() -> InMemoryTenantProfileStore | PostgresTenantProfileStore:
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemoryTenantProfileStore()
    if backend == "postgres":
        return PostgresTenantProfileStore(os.getenv("DATABASE_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_STORE_BACKEND: {backend}")


def default_tenant_profile(tenant_id: str, *, name: str | None = None) -> dict[str, Any]:
    now = _now()
    tenant = _clean_tenant_id(tenant_id)
    return {
        "tenant_id": tenant,
        "name": name or tenant,
        "country": "ZA",
        "currency": "ZAR",
        "timezone": "Africa/Johannesburg",
        "status": "active",
        "budgets": deepcopy(DEFAULT_BUDGETS),
        "model_limits": deepcopy(DEFAULT_MODEL_LIMITS),
        "connector_policy": deepcopy(DEFAULT_CONNECTOR_POLICY),
        "created_at": now,
        "updated_at": now,
    }


def _profile_record(
    profile: dict[str, Any],
    *,
    existing: dict[str, Any] | None,
) -> dict[str, Any]:
    tenant_id = _clean_tenant_id(str(profile.get("tenant_id") or ""))
    base = default_tenant_profile(tenant_id)
    if existing is not None:
        base.update(deepcopy(existing))
    base.update(deepcopy(profile))
    base["tenant_id"] = tenant_id
    base["budgets"] = _positive_int_map(base.get("budgets"), defaults=DEFAULT_BUDGETS)
    base["model_limits"] = _mapping(base.get("model_limits"), defaults=DEFAULT_MODEL_LIMITS)
    base["connector_policy"] = _mapping(
        base.get("connector_policy"),
        defaults=DEFAULT_CONNECTOR_POLICY,
    )
    base["created_at"] = str(base.get("created_at") or _now())
    base["updated_at"] = _now()
    return base


def _positive_int_map(value: object, *, defaults: dict[str, int]) -> dict[str, int]:
    merged = dict(defaults)
    if value is None:
        return merged
    if not isinstance(value, dict):
        raise ValueError("budgets must be an object")
    for key, raw in value.items():
        amount = int(raw)
        if amount < 0:
            raise ValueError(f"budget must be non-negative: {key}")
        merged[str(key)] = amount
    return merged


def _mapping(value: object, *, defaults: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(defaults)
    if value is None:
        return merged
    if not isinstance(value, dict):
        raise ValueError("profile section must be an object")
    merged.update(deepcopy(value))
    return merged


def _clean_tenant_id(value: str) -> str:
    tenant_id = value.strip()
    if not tenant_id:
        raise ValueError("tenant_id is required")
    if len(tenant_id) > 128 or not tenant_id.replace("_", "").replace("-", "").isalnum():
        raise ValueError("tenant_id must be a simple identifier")
    return tenant_id


def _now() -> str:
    return datetime.now(UTC).isoformat()


_TENANT_PROFILE_SCHEMA_SQL = """
create extension if not exists vector;
create table if not exists shelfwise_business_profile (
    tenant_id text primary key,
    payload jsonb not null,
    embedding vector(768),
    updated_at timestamptz not null
);
"""
