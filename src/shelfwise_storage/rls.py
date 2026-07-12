from __future__ import annotations

from typing import Any

TENANT_SCOPED_TABLES = {
    "shelfwise_decisions": "tenant_id",
    "shelfwise_events": "tenant_id",
    "shelfwise_inbound_records": "tenant_id",
    "shelfwise_learning_thresholds": "tenant_id",
    "shelfwise_learning_events": "tenant_id",
    "cascade_runs": "tenant_id",
    "cascade_steps": "tenant_id",
    "shelfwise_model_runs": "tenant_id",
    "shelfwise_prompt_versions": "tenant_id",
    "shelfwise_writeback_tasks": "tenant_id",
    "shelfwise_worldgen_runs": "tenant_id",
    "shelfwise_product_state": "tenant_id",
    "shelfwise_learned_patterns": "tenant_id",
    "shelfwise_business_profile": "tenant_id",
    "shelfwise_products": "tenant_id",
    "shelfwise_product_variants": "tenant_id",
    "shelfwise_product_identifiers": "tenant_id",
    "shelfwise_chat_conversations": "tenant_id",
    "shelfwise_inventory_positions": "tenant_id",
}


def tenant_rls_sql(table: str, *, tenant_column: str = "tenant_id") -> list[str]:
    if not table.replace("_", "").isalnum():
        raise ValueError("table name must be a simple identifier")
    if not tenant_column.replace("_", "").isalnum():
        raise ValueError("tenant column must be a simple identifier")
    policy = f"{table}_tenant_isolation"
    return [
        f"alter table {table} enable row level security",
        f"alter table {table} force row level security",
        f"drop policy if exists {policy} on {table}",
        (
            f"create policy {policy} on {table} "
            f"using ({tenant_column} = current_setting('app.tenant_id', true)) "
            f"with check ({tenant_column} = current_setting('app.tenant_id', true))"
        ),
    ]


def all_tenant_rls_sql() -> list[str]:
    statements: list[str] = []
    for table, tenant_column in TENANT_SCOPED_TABLES.items():
        statements.extend(tenant_rls_sql(table, tenant_column=tenant_column))
    return statements


def apply_tenant_rls(conn: Any, tables: tuple[str, ...]) -> None:
    """Apply fail-closed tenant RLS for the named tenant-scoped tables."""

    for table in tables:
        tenant_column = TENANT_SCOPED_TABLES.get(table)
        if tenant_column is None:
            raise ValueError(f"unknown tenant scoped table: {table}")
        for statement in tenant_rls_sql(table, tenant_column=tenant_column):
            conn.execute(statement)


def set_tenant_sql(tenant_id: str) -> tuple[str, tuple[str]]:
    if not tenant_id.strip():
        raise ValueError("tenant_id is required")
    return "select set_config('app.tenant_id', %s, true)", (tenant_id.strip(),)
