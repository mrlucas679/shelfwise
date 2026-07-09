from __future__ import annotations

from pathlib import Path

import pytest

from shelfwise_backend import run_golden_cascade
from shelfwise_storage import (
    TENANT_SCOPED_TABLES,
    all_tenant_rls_sql,
    apply_tenant_rls,
    bind_tenant_context,
    current_tenant_id,
    reset_tenant_context,
    set_tenant_sql,
    tenant_rls_sql,
)

ROOT = Path(__file__).resolve().parents[1]


def test_runnable_decisions_carry_tenant_id_for_persistence() -> None:
    result = run_golden_cascade()

    assert result["decision"]["tenant_id"] == "sa_retail_demo"


def test_tenant_rls_sql_is_fail_closed_and_covers_current_business_tables() -> None:
    statements = all_tenant_rls_sql()
    joined = "\n".join(statements)

    assert "shelfwise_decisions" in TENANT_SCOPED_TABLES
    assert "shelfwise_events" in TENANT_SCOPED_TABLES
    assert "shelfwise_inbound_records" in TENANT_SCOPED_TABLES
    assert "cascade_steps" in TENANT_SCOPED_TABLES
    assert "shelfwise_writeback_tasks" in TENANT_SCOPED_TABLES
    assert "shelfwise_worldgen_runs" in TENANT_SCOPED_TABLES
    assert "shelfwise_product_state" in TENANT_SCOPED_TABLES
    assert "shelfwise_learned_patterns" in TENANT_SCOPED_TABLES
    assert "shelfwise_business_profile" in TENANT_SCOPED_TABLES
    assert "force row level security" in joined
    assert "current_setting('app.tenant_id', true)" in joined
    assert "with check" in joined


def test_tenant_rls_sql_rejects_non_identifier_names() -> None:
    with pytest.raises(ValueError, match="table name"):
        tenant_rls_sql("shelfwise_decisions; drop table users")
    with pytest.raises(ValueError, match="tenant column"):
        tenant_rls_sql("shelfwise_decisions", tenant_column="tenant_id; drop")


def test_set_tenant_sql_is_parameterized() -> None:
    sql, params = set_tenant_sql("tenant_1")

    assert sql == "select set_config('app.tenant_id', %s, true)"
    assert params == ("tenant_1",)
    with pytest.raises(ValueError, match="tenant_id is required"):
        set_tenant_sql("")


def test_apply_tenant_rls_executes_fail_closed_policy_statements() -> None:
    class FakeConn:
        def __init__(self) -> None:
            self.statements: list[str] = []

        def execute(self, statement: str) -> None:
            self.statements.append(statement)

    conn = FakeConn()

    apply_tenant_rls(conn, ("shelfwise_events",))

    joined = "\n".join(conn.statements)
    assert "alter table shelfwise_events force row level security" in joined
    assert "with check (tenant_id = current_setting('app.tenant_id', true))" in joined
    with pytest.raises(ValueError, match="unknown tenant scoped table"):
        apply_tenant_rls(conn, ("not_registered",))


def test_storage_tenant_context_defaults_and_resets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SHELFWISE_TENANT_ID", raising=False)
    monkeypatch.delenv("TENANT_ID", raising=False)

    assert current_tenant_id() == "sa_retail_demo"
    token = bind_tenant_context("tenant_2")
    try:
        assert current_tenant_id() == "tenant_2"
    finally:
        reset_tenant_context(token)
    assert current_tenant_id() == "sa_retail_demo"


def test_compose_init_schema_matches_tenant_scoped_tables() -> None:
    schema = (ROOT / "src" / "shelfwise_storage" / "schema.sql").read_text(
        encoding="utf-8"
    )

    assert "create extension if not exists vector" in schema
    for table in TENANT_SCOPED_TABLES:
        assert f"create table if not exists {table}" in schema
        assert f"alter table {table} force row level security" in schema
        assert f"create policy {table}_tenant_isolation" in schema
