"""Tenant-scoped product value statements."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from shelfwise_runtime.provenance import DataDomain

from .decision_access import tenant_scoped_decisions
from .deps import CURRENT_TENANT_DEP
from .tenant import TenantContext
from .value_reporting import current_month, monthly_value_statement

router = APIRouter()


@router.get("/reports/value-recovered")
def value_recovered_report(
    month: str | None = None,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Return operational receipts while excluding generated simulation outcomes."""
    reporting_month = month or current_month()
    decisions = tenant_scoped_decisions(
        ctx,
        data_domain=DataDomain.OPERATIONAL_TWIN.value,
    )
    try:
        report = monthly_value_statement(decisions, month=reporting_month)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "tenant_id": ctx.tenant_id,
        "data_domain": DataDomain.OPERATIONAL_TWIN.value,
        "report": report,
    }
