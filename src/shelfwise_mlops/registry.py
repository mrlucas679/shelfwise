from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from shelfwise_storage import auto_schema_enabled, connect
from shelfwise_storage.rls import apply_tenant_rls


@dataclass(frozen=True, slots=True)
class ModelRun:
    id: str
    tenant_id: str
    correlation_id: str
    agent: str
    model: str
    provider: str
    prompt_version: str
    schema_version: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    data_domain: str = "world_simulation"
    status: str = "ok"
    created_at: str = ""
    # Observability payload: what was actually sent/received, so a real prompt/response/error
    # can be inspected later without re-running the call. Capped at the client layer to bound
    # storage growth; empty string means "not applicable" (e.g. no error on a successful run).
    user_message: str = ""
    response_text: str = ""
    error_detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "correlation_id": self.correlation_id,
            "agent": self.agent,
            "model": self.model,
            "provider": self.provider,
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "data_domain": self.data_domain,
            "status": self.status,
            "created_at": self.created_at or datetime.now(UTC).isoformat(),
            "user_message": self.user_message,
            "response_text": self.response_text,
            "error_detail": self.error_detail,
        }


@dataclass(frozen=True, slots=True)
class PromptVersion:
    id: str
    tenant_id: str
    agent: str
    version: str
    sha: str
    system_prompt: str
    schema_version: str = "v1"
    created_at: str = ""

    def to_dict(self, *, include_prompt: bool = False) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "agent": self.agent,
            "version": self.version,
            "sha": self.sha,
            "schema_version": self.schema_version,
            "created_at": self.created_at or datetime.now(UTC).isoformat(),
        }
        if include_prompt:
            payload["system_prompt"] = self.system_prompt
        else:
            payload["system_prompt_preview"] = _preview(self.system_prompt)
        return payload


class InMemoryModelRunRegistry:
    def __init__(self) -> None:
        self._runs: list[ModelRun] = []

    def record(self, run: ModelRun) -> ModelRun:
        self._runs.append(run)
        return run

    def list(
        self,
        *,
        tenant_id: str | None = None,
        data_domain: str | None = None,
    ) -> list[ModelRun]:
        runs = list(self._runs)
        if tenant_id is not None:
            runs = [run for run in runs if run.tenant_id == tenant_id]
        if data_domain is not None:
            runs = [run for run in runs if run.data_domain == data_domain]
        return runs

    def clear(self) -> None:
        self._runs.clear()


class InMemoryPromptRegistry:
    def __init__(self) -> None:
        self._versions: dict[tuple[str, str], PromptVersion] = {}

    def record(self, version: PromptVersion) -> PromptVersion:
        key = (version.tenant_id, version.id)
        existing = self._versions.get(key)
        if existing is not None:
            if _prompt_identity(existing) != _prompt_identity(version):
                raise ValueError(f"Prompt version {version.id} already has different content")
            return existing
        if version.sha != prompt_sha(version.system_prompt):
            raise ValueError("Prompt version sha does not match system prompt")
        stored = version
        if not stored.created_at:
            stored = replace(stored, created_at=datetime.now(UTC).isoformat())
        self._versions[key] = stored
        return stored

    def record_prompt(
        self,
        *,
        agent: str,
        version: str,
        system_prompt: str,
        tenant_id: str = "global",
        prompt_id: str | None = None,
        schema_version: str = "v1",
    ) -> PromptVersion:
        return self.record(
            PromptVersion(
                id=prompt_id or f"{agent}:{version}",
                tenant_id=tenant_id,
                agent=agent,
                version=version,
                sha=prompt_sha(system_prompt),
                system_prompt=system_prompt,
                schema_version=schema_version,
            )
        )

    def get(self, prompt_id: str, *, tenant_id: str | None = None) -> PromptVersion | None:
        if tenant_id is not None:
            return self._versions.get((tenant_id, prompt_id))
        matches = [version for version in self._versions.values() if version.id == prompt_id]
        if not matches:
            return None
        return sorted(matches, key=lambda version: (version.tenant_id, version.id))[0]

    def list(
        self,
        *,
        tenant_id: str | None = None,
        agent: str | None = None,
    ) -> list[PromptVersion]:
        versions = list(self._versions.values())
        if tenant_id is not None:
            versions = [version for version in versions if version.tenant_id == tenant_id]
        if agent is not None:
            versions = [version for version in versions if version.agent == agent]
        return sorted(versions, key=lambda version: (version.agent, version.version, version.id))

    def clear(self) -> None:
        self._versions.clear()


class PostgresModelRunRegistry:
    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresModelRunRegistry")
        self._database_url = database_url
        if auto_schema_enabled():
            self._ensure_schema()

    def record(self, run: ModelRun) -> ModelRun:
        stored = run if run.created_at else replace(run, created_at=datetime.now(UTC).isoformat())
        with self._connect() as conn:
            conn.execute(
                """
                insert into shelfwise_model_runs
                    (
                        id, tenant_id, correlation_id, agent, model, provider,
                        prompt_version, schema_version, input_tokens, output_tokens,
                        latency_ms, data_domain, status, created_at,
                        user_message, response_text, error_detail
                    )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (id) do update
                set status = excluded.status,
                    output_tokens = excluded.output_tokens,
                    latency_ms = excluded.latency_ms,
                    data_domain = excluded.data_domain,
                    response_text = excluded.response_text,
                    error_detail = excluded.error_detail
                """,
                (
                    stored.id,
                    stored.tenant_id,
                    stored.correlation_id,
                    stored.agent,
                    stored.model,
                    stored.provider,
                    stored.prompt_version,
                    stored.schema_version,
                    stored.input_tokens,
                    stored.output_tokens,
                    stored.latency_ms,
                    stored.data_domain,
                    stored.status,
                    stored.created_at,
                    stored.user_message,
                    stored.response_text,
                    stored.error_detail,
                ),
            )
            conn.commit()
        return stored

    def list(
        self,
        *,
        tenant_id: str | None = None,
        data_domain: str | None = None,
    ) -> list[ModelRun]:
        clauses: list[str] = []
        params: list[object] = []
        if tenant_id is not None:
            clauses.append("tenant_id = %s")
            params.append(tenant_id)
        if data_domain is not None:
            clauses.append("data_domain = %s")
            params.append(data_domain)
        where = f"where {' and '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select id, tenant_id, correlation_id, agent, model, provider,
                       prompt_version, schema_version, input_tokens, output_tokens,
                       latency_ms, data_domain, status, created_at,
                       user_message, response_text, error_detail
                from shelfwise_model_runs
                {where}
                order by created_at asc, id asc
                """,
                tuple(params),
            ).fetchall()
        return [_model_run_from_row(row) for row in rows]

    def clear(self) -> None:
        with self._connect() as conn:
            conn.execute("delete from shelfwise_model_runs")
            conn.commit()

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(_MODEL_RUN_SCHEMA_SQL)
            conn.execute(
                """
                alter table shelfwise_model_runs
                add column if not exists data_domain text not null default 'world_simulation'
                """
            )
            conn.execute(
                """
                alter table shelfwise_model_runs
                add column if not exists user_message text not null default ''
                """
            )
            conn.execute(
                """
                alter table shelfwise_model_runs
                add column if not exists response_text text not null default ''
                """
            )
            conn.execute(
                """
                alter table shelfwise_model_runs
                add column if not exists error_detail text not null default ''
                """
            )
            apply_tenant_rls(conn, ("shelfwise_model_runs",))
            conn.commit()

    def _connect(self) -> Any:
        return connect(self._database_url)


class PostgresPromptRegistry:
    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresPromptRegistry")
        self._database_url = database_url
        if auto_schema_enabled():
            self._ensure_schema()

    def record(self, version: PromptVersion) -> PromptVersion:
        existing = self.get(version.id, tenant_id=version.tenant_id)
        if existing is not None:
            if _prompt_identity(existing) != _prompt_identity(version):
                raise ValueError(f"Prompt version {version.id} already has different content")
            return existing
        if version.sha != prompt_sha(version.system_prompt):
            raise ValueError("Prompt version sha does not match system prompt")
        stored = (
            version
            if version.created_at
            else replace(version, created_at=datetime.now(UTC).isoformat())
        )
        with self._connect() as conn:
            conn.execute(
                """
                insert into shelfwise_prompt_versions
                    (
                        id, tenant_id, agent, version, sha, system_prompt,
                        schema_version, created_at
                    )
                values (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    stored.id,
                    stored.tenant_id,
                    stored.agent,
                    stored.version,
                    stored.sha,
                    stored.system_prompt,
                    stored.schema_version,
                    stored.created_at,
                ),
            )
            conn.commit()
        return stored

    def record_prompt(
        self,
        *,
        agent: str,
        version: str,
        system_prompt: str,
        tenant_id: str = "global",
        prompt_id: str | None = None,
        schema_version: str = "v1",
    ) -> PromptVersion:
        return self.record(
            PromptVersion(
                id=prompt_id or f"{agent}:{version}",
                tenant_id=tenant_id,
                agent=agent,
                version=version,
                sha=prompt_sha(system_prompt),
                system_prompt=system_prompt,
                schema_version=schema_version,
            )
        )

    def get(self, prompt_id: str, *, tenant_id: str | None = None) -> PromptVersion | None:
        clauses = ["id = %s"]
        params: list[str] = [prompt_id]
        if tenant_id is not None:
            clauses.append("tenant_id = %s")
            params.append(tenant_id)
        where = " and ".join(clauses)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                select id, tenant_id, agent, version, sha, system_prompt,
                       schema_version, created_at
                from shelfwise_prompt_versions
                where {where}
                order by tenant_id asc, id asc
                limit 1
                """,
                tuple(params),
            ).fetchone()
        return _prompt_from_row(row) if row else None

    def list(
        self,
        *,
        tenant_id: str | None = None,
        agent: str | None = None,
    ) -> list[PromptVersion]:
        clauses: list[str] = []
        params: list[str] = []
        if tenant_id is not None:
            clauses.append("tenant_id = %s")
            params.append(tenant_id)
        if agent is not None:
            clauses.append("agent = %s")
            params.append(agent)
        where = "where " + " and ".join(clauses) if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select id, tenant_id, agent, version, sha, system_prompt,
                       schema_version, created_at
                from shelfwise_prompt_versions
                {where}
                order by agent asc, version asc, id asc
                """,
                tuple(params),
            ).fetchall()
        return [_prompt_from_row(row) for row in rows]

    def clear(self) -> None:
        with self._connect() as conn:
            conn.execute("delete from shelfwise_prompt_versions")
            conn.commit()

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(_PROMPT_VERSION_SCHEMA_SQL)
            apply_tenant_rls(conn, ("shelfwise_prompt_versions",))
            conn.commit()

    def _connect(self) -> Any:
        return connect(self._database_url)


def prompt_sha(system_prompt: str) -> str:
    return hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()


def create_model_run_registry() -> InMemoryModelRunRegistry | PostgresModelRunRegistry:
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemoryModelRunRegistry()
    if backend == "postgres":
        return PostgresModelRunRegistry(os.getenv("DATABASE_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_STORE_BACKEND: {backend}")


def create_prompt_registry() -> InMemoryPromptRegistry | PostgresPromptRegistry:
    backend = os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemoryPromptRegistry()
    if backend == "postgres":
        return PostgresPromptRegistry(os.getenv("DATABASE_URL", ""))
    raise ValueError(f"unsupported SHELFWISE_STORE_BACKEND: {backend}")


def release_gate(
    candidate_scores: dict[str, Decimal],
    baseline_scores: dict[str, Decimal],
    *,
    max_regression: Decimal = Decimal("0.02"),
) -> dict[str, object]:
    regressions: dict[str, str] = {}
    for metric, baseline in baseline_scores.items():
        candidate = candidate_scores.get(metric)
        if candidate is None:
            regressions[metric] = "missing"
            continue
        if candidate + max_regression < baseline:
            regressions[metric] = f"{candidate} < {baseline} - {max_regression}"
    return {
        "passed": not regressions,
        "regressions": regressions,
        "metrics_checked": sorted(baseline_scores),
    }


def _prompt_identity(version: PromptVersion) -> tuple[str, str, str, str, str, str]:
    return (
        version.tenant_id,
        version.agent,
        version.version,
        version.sha,
        version.system_prompt,
        version.schema_version,
    )


def _preview(value: str) -> str:
    compact = " ".join(value.split())
    if len(compact) <= 120:
        return compact
    return f"{compact[:117]}..."


def _model_run_from_row(row: dict[str, Any]) -> ModelRun:
    return ModelRun(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        correlation_id=str(row["correlation_id"]),
        agent=str(row["agent"]),
        model=str(row["model"]),
        provider=str(row["provider"]),
        prompt_version=str(row["prompt_version"]),
        schema_version=str(row["schema_version"]),
        input_tokens=int(row["input_tokens"]),
        output_tokens=int(row["output_tokens"]),
        latency_ms=int(row["latency_ms"]),
        data_domain=str(row.get("data_domain") or "world_simulation"),
        status=str(row["status"]),
        created_at=_iso(row["created_at"]),
        user_message=str(row.get("user_message") or ""),
        response_text=str(row.get("response_text") or ""),
        error_detail=str(row.get("error_detail") or ""),
    )


def _prompt_from_row(row: dict[str, Any]) -> PromptVersion:
    return PromptVersion(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        agent=str(row["agent"]),
        version=str(row["version"]),
        sha=str(row["sha"]),
        system_prompt=str(row["system_prompt"]),
        schema_version=str(row["schema_version"]),
        created_at=_iso(row["created_at"]),
    )


def _iso(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


_MODEL_RUN_SCHEMA_SQL = """
create table if not exists shelfwise_model_runs (
    id text primary key,
    tenant_id text not null,
    correlation_id text not null,
    agent text not null,
    model text not null,
    provider text not null,
    prompt_version text not null,
    schema_version text not null,
    input_tokens integer not null,
    output_tokens integer not null,
    latency_ms integer not null,
    data_domain text not null default 'world_simulation',
    status text not null default 'ok',
    created_at timestamptz not null,
    user_message text not null default '',
    response_text text not null default '',
    error_detail text not null default ''
);
create index if not exists idx_shelfwise_model_runs_tenant_created
on shelfwise_model_runs (tenant_id, created_at desc);
create index if not exists idx_shelfwise_model_runs_tenant_domain_created
on shelfwise_model_runs (tenant_id, data_domain, created_at desc);
"""

_PROMPT_VERSION_SCHEMA_SQL = """
create table if not exists shelfwise_prompt_versions (
    tenant_id text not null,
    id text not null,
    agent text not null,
    version text not null,
    sha text not null,
    system_prompt text not null,
    schema_version text not null default 'v1',
    created_at timestamptz not null,
    primary key (tenant_id, id)
);
create index if not exists idx_shelfwise_prompt_versions_tenant_agent
on shelfwise_prompt_versions (tenant_id, agent, version);
"""
