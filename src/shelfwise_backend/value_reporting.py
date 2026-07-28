"""Honest month-bounded value reporting from explicit completion receipts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

_ESTIMATE_FIELDS = (
    "incremental_profit_minor_units",
    "markdown_margin_minor_units",
    "expected_revenue_minor_units",
    "line_revenue_minor_units",
    "stock_at_risk_minor_units",
    "revenue_exposure_minor_units",
    "stockout_exposure_minor_units",
)


def monthly_value_statement(
    decisions: list[dict[str, Any]],
    *,
    month: str,
) -> dict[str, Any]:
    """Separate verified recovered value from still-unverified estimates."""
    start, end = month_bounds(month)
    verified_total = 0
    estimated_total = 0
    verified_receipts: list[dict[str, Any]] = []
    unverified_decisions: list[dict[str, Any]] = []

    for decision in decisions:
        verified = _verified_receipt(decision)
        verified_at = _timestamp(verified.get("verified_at")) if verified else None
        if verified and verified_at and start <= verified_at < end:
            amount = _non_negative_int(verified.get("minor_units"))
            if amount is not None:
                verified_total += amount
                verified_receipts.append(
                    {
                        "decision_id": str(decision.get("id") or ""),
                        "task_id": str(verified.get("task_id") or ""),
                        "source_reference": str(
                            verified.get("source_reference") or ""
                        ),
                        "verified_at": verified_at.isoformat(),
                        "verified_by": str(verified.get("verified_by") or ""),
                        "minor_units": amount,
                        "currency": "ZAR",
                    }
                )
            continue

        decision_at = _timestamp(
            decision.get("updated_at") or decision.get("created_at")
        )
        if (
            str(decision.get("status") or "") != "approved"
            or decision_at is None
            or not (start <= decision_at < end)
        ):
            continue
        estimate = _decision_estimate(decision)
        if estimate is not None:
            estimated_total += estimate["minor_units"]
        unverified_decisions.append(
            {
                "decision_id": str(decision.get("id") or ""),
                "estimate": estimate,
            }
        )

    verified_receipts.sort(key=lambda item: (item["verified_at"], item["decision_id"]))
    return {
        "month": month,
        "currency": "ZAR",
        "verified_recovered_minor_units": verified_total,
        "verified_receipt_count": len(verified_receipts),
        "verified_receipts": verified_receipts,
        "estimated_opportunity_minor_units": estimated_total,
        "unverified_decision_count": len(unverified_decisions),
        "unverified_decisions": unverified_decisions,
        "method": (
            "Verified recovered value includes only explicit task-completion amounts "
            "with a source reference; model outcomes and simulations are excluded."
        ),
    }


def month_bounds(month: str) -> tuple[datetime, datetime]:
    """Return an inclusive UTC month start and exclusive next-month boundary."""
    try:
        start = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError("month must use YYYY-MM format") from exc
    if start.strftime("%Y-%m") != month:
        raise ValueError("month must use YYYY-MM format")
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


def current_month() -> str:
    """Return the current UTC reporting month."""
    return datetime.now(UTC).strftime("%Y-%m")


def _verified_receipt(decision: dict[str, Any]) -> dict[str, Any]:
    value = decision.get("verified_outcome")
    return value if isinstance(value, dict) else {}


def _decision_estimate(decision: dict[str, Any]) -> dict[str, Any] | None:
    outcome = decision.get("expected_outcome")
    if not isinstance(outcome, dict):
        return None
    for field in _ESTIMATE_FIELDS:
        amount = _non_negative_int(outcome.get(field))
        if amount is not None:
            return {"field": field, "minor_units": amount, "currency": "ZAR"}
    return None


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)
