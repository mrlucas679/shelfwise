from __future__ import annotations

import hashlib
import os
from copy import deepcopy
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from shelfwise_storage import auto_schema_enabled, connect, jsonb
from shelfwise_storage.rls import apply_tenant_rls

from .provenance import InboundRecord


class InMemoryInboundRecordStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._records: dict[tuple[str, str, str], dict[str, Any]] = {}

    def record(
        self,
        record: InboundRecord,
        *,
        event_id: str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        key = _record_key(record)
        payload = _stored_payload(record, event_id=event_id)
        with self._lock:
            existing = self._records.get(key)
            if existing is not None:
                return False, _public_payload(existing)
            self._records[key] = payload
            return True, _public_payload(payload)

    def list(
        self,
        *,
        tenant_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        _validate_limit(limit)
        with self._lock:
            rows = list(self._records.values())
        if tenant_id is not None:
            rows = [row for row in rows if row["tenant_id"] == tenant_id]
        rows = sorted(rows, key=lambda row: str(row["ingested_at"]), reverse=True)
        return [_public_payload(row) for row in rows[:limit]]

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


class PostgresInboundRecordStore:
    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresInboundRecordStore")
        self._database_url = database_url
        if auto_schema_enabled():
            self._ensure_schema()

    def record(
        self,
        record: InboundRecord,
        *,
        event_id: str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        payload = _stored_payload(record, event_id=event_id)
        with self._connect(record.tenant_id) as conn:
            row = conn.execute(
                """
                insert into shelfwise_inbound_records
                    (
                        id,
                        tenant_id,
                        source_system,
                        source_object_type,
                        source_object_id,
                        raw_payload_hash,
                        event_id,
                        payload,
                        ingested_at,
                        event_time
                    )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (tenant_id, source_system, raw_payload_hash, source_object_id)
                    do nothing
                returning payload
                """,
                (
                    payload["id"],
                    record.tenant_id,
                    record.source_system.value,
                    record.source_object_type,
                    record.source_object_id,
                    record.payload_hash,
                    event_id,
                    jsonb(payload),
                    payload["ingested_at"],
                    record.event_time.isoformat(),
                ),
            ).fetchone()
            if row is not None:
                conn.commit()
                return True, _public_payload(row["payload"])

            row = conn.execute(
                """
                select payload
                from shelfwise_inbound_records
                where tenant_id = %s
                    and source_system = %s
                    and raw_payload_hash = %s
                    and source_object_id = %s
                """,
                (
                    record.tenant_id,
                    record.source_system.value,
                    record.payload_hash,
                    record.source_object_id,
                ),
            ).fetchone()
            conn.commit()
        if row is None:
            raise RuntimeError("inbound record insert failed without existing row")
        return False, _public_payload(row["payload"])

    def list(
        self,
        *,
        tenant_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        _validate_limit(limit)
        with self._connect(tenant_id) as conn:
            if tenant_id is None:
                rows = conn.execute(
                    """
                    select payload
                    from shelfwise_inbound_records
                    order by ingested_at desc, id
                    limit %s
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    select payload
                    from shelfwise_inbound_records
                    where tenant_id = %s
                    order by ingested_at desc, id
                    limit %s
                    """,
                    (tenant_id, limit),
                ).fetchall()
        return [_public_payload(row["payload"]) for row in rows]

    def clear(self) -> None:
        with self._connect(None) as conn:
            conn.execute("delete from shelfwise_inbound_records")
            conn.commit()

    def _ensure_schema(self) -> None:
        with self._connect(None) as conn:
            conn.execute(
                """
                create table if not exists shelfwise_inbound_records (
                    id text primary key,
                    tenant_id text not null,
                    source_system text not null,
                    source_object_type text not null,
                    source_object_id text not null,
                    raw_payload_hash text not null,
                    event_id text,
                    payload jsonb not null,
                    ingested_at timestamptz not null,
                    event_time timestamptz not null
                )
                """
            )
            conn.execute(
                """
                do $$
                declare
                    old_constraint text;
                begin
                    -- Pre-migration shape keyed only on (tenant_id, source_system,
                    -- raw_payload_hash): every line/count derived from one raw webhook
                    -- payload shares that hash, so it silently kept only the first. Widen
                    -- it to include source_object_id so distinct lines/counts from a single
                    -- payload can all persist, while a resent payload (same object ids)
                    -- still dedups.
                    select con.conname into old_constraint
                    from pg_constraint con
                    join pg_class rel on rel.oid = con.conrelid
                    where rel.relname = 'shelfwise_inbound_records'
                      and con.contype = 'u'
                      and pg_get_constraintdef(con.oid)
                          = 'UNIQUE (tenant_id, source_system, raw_payload_hash)';

                    if old_constraint is not null then
                        execute format(
                            'alter table shelfwise_inbound_records drop constraint %I',
                            old_constraint
                        );
                    end if;

                    if not exists (
                        select 1 from pg_constraint
                        where conname = 'shelfwise_inbound_records_dedup_key'
                    ) then
                        alter table shelfwise_inbound_records
                        add constraint shelfwise_inbound_records_dedup_key
                        unique (tenant_id, source_system, raw_payload_hash, source_object_id);
                    end if;
                end $$
                """
            )
            conn.execute(
                """
                create index if not exists idx_shelfwise_inbound_records_tenant_ingested
                on shelfwise_inbound_records (tenant_id, ingested_at desc)
                """
            )
            conn.execute(
                """
                create index if not exists idx_shelfwise_inbound_records_tenant_source_object
                on shelfwise_inbound_records (tenant_id, source_system, source_object_id)
                """
            )
            apply_tenant_rls(conn, ("shelfwise_inbound_records",))
            conn.commit()

    def _connect(self, tenant_id: str | None) -> Any:
        return connect(self._database_url, tenant_id=tenant_id)


def create_inbound_record_store() -> InMemoryInboundRecordStore | PostgresInboundRecordStore:
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemoryInboundRecordStore()
    if backend == "postgres":
        return PostgresInboundRecordStore(os.getenv("DATABASE_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_STORE_BACKEND: {backend}")


def _stored_payload(record: InboundRecord, *, event_id: str | None) -> dict[str, Any]:
    payload = record.to_dict()
    payload.update(
        {
            "id": _record_id(record),
            "event_id": event_id,
            "ingested_at": datetime.now(UTC).isoformat(),
            "raw_payload": deepcopy(record.raw_payload),
            "has_raw_payload": True,
        }
    )
    return payload


def _public_payload(payload: dict[str, Any]) -> dict[str, Any]:
    public = deepcopy(payload)
    public.pop("raw_payload", None)
    public["has_raw_payload"] = bool(payload.get("raw_payload"))
    return public


def _record_key(record: InboundRecord) -> tuple[str, str, str, str]:
    # Keying on payload_hash alone would collide every line/count derived from ONE raw
    # webhook payload (they all share the same raw_payload, hence the same hash) - only the
    # first would ever be stored. Including source_object_id keeps retry-idempotency (a
    # resent payload reproduces the same object ids, so it still dedups) while letting
    # multiple distinct lines/counts from a single payload each persist.
    return (
        record.tenant_id,
        record.source_system.value,
        record.payload_hash,
        record.source_object_id,
    )


def _record_id(record: InboundRecord) -> str:
    basis = f"{record.tenant_id}:{record.source_system.value}:{record.payload_hash}"
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    return f"inbound_{digest[:16]}"


def _validate_limit(limit: int) -> None:
    if limit <= 0 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
