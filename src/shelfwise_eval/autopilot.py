"""Deterministic reviewer that stands in for a human during unattended harness runs.

The application's HITL gate is deliberately human. During an unattended soak run there
is no human, so the harness supplies one: a fixed, documented, auditable policy that
approves or rejects every pending decision through the same public endpoints a person
would use. Every verdict carries its reason so the run's trail shows not just what was
decided, but why.
"""

from __future__ import annotations

from typing import Any

APPROVE = "approve"
REJECT = "reject"
SKIP = "skip"

# R1,000.00 in minor units: small enough that a wrong call is cheap, large enough that
# routine markdowns and facility checks sail through.
DEFAULT_EXPOSURE_LIMIT_MINOR_UNITS = 100_000

_EXPOSURE_KEYS = (
    "revenue_exposure_minor_units",
    "stock_at_risk_minor_units",
    "stockout_exposure_minor_units",
    "incremental_profit_minor_units",
)

_EXPECTED_TERMINAL_STATUS = {
    APPROVE: "approved",
    REJECT: "rejected",
}


def review_decision(
    decision: dict[str, Any],
    *,
    exposure_limit_minor_units: int = DEFAULT_EXPOSURE_LIMIT_MINOR_UNITS,
    force_dissent: bool = False,
) -> dict[str, Any]:
    """Return the autopilot verdict for one decision.

    Policy, in order:
    - not pending -> SKIP (never re-decide a resolved decision)
    - critic verdict "approved" -> APPROVE (the critic already validated the evidence)
    - critic verdict "review_required" -> APPROVE when the money at stake is at or under
      the exposure limit, REJECT when it is larger (a human should have seen it; absent
      one, the safe unattended default is to hold the action back)
    - anything else (critic rejected, missing verdict) -> REJECT
    """
    status = str(decision.get("status") or "")
    exposure = _exposure_minor_units(decision)
    if status != "pending":
        return _verdict(SKIP, f"status is {status or 'unknown'}, not pending", exposure)

    verdict = str(decision.get("critic_verdict") or "")
    if verdict == "approved":
        if force_dissent:
            return _verdict(
                REJECT,
                "deterministic dissent sample: independently rejected critic-approved action",
                exposure,
            )
        return _verdict(APPROVE, "critic approved the evidence chain", exposure)
    if verdict == "review_required":
        if exposure <= exposure_limit_minor_units:
            if force_dissent:
                return _verdict(
                    REJECT,
                    "deterministic dissent sample: held an otherwise approvable action",
                    exposure,
                )
            return _verdict(
                APPROVE,
                f"exposure {exposure} minor units within limit {exposure_limit_minor_units}",
                exposure,
            )
        return _verdict(
            REJECT,
            f"exposure {exposure} minor units exceeds limit {exposure_limit_minor_units}",
            exposure,
        )
    reason = f"unattended default for critic verdict '{verdict or 'missing'}'"
    return _verdict(REJECT, reason, exposure)


def resolution_receipt(
    *,
    decision_id: str,
    verdict: dict[str, Any],
    status_code: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Audit a HITL endpoint response against the exact request that was sent."""
    action = str(verdict.get("action") or "")
    expected_status = _EXPECTED_TERMINAL_STATUS.get(action)
    result = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
    returned_id = str(result.get("id") or "")
    returned_status = str(result.get("status") or "")
    mismatches: list[str] = []
    if status_code != 200:
        mismatches.append(f"http_status={status_code}")
    if returned_id != decision_id:
        mismatches.append(f"decision_id={returned_id or 'missing'}")
    if expected_status is None:
        mismatches.append(f"unsupported_action={action or 'missing'}")
    elif returned_status != expected_status:
        mismatches.append(
            f"returned_status={returned_status or 'missing'} expected={expected_status}"
        )
    return {
        "requested_decision_id": decision_id,
        "requested_action": action,
        "expected_status": expected_status,
        "status_code": status_code,
        "returned_decision_id": returned_id,
        "returned_status": returned_status,
        "matched": not mismatches,
        "mismatches": mismatches,
    }


def _exposure_minor_units(decision: dict[str, Any]) -> int:
    expected = decision.get("expected_outcome")
    if not isinstance(expected, dict):
        return 0
    for key in _EXPOSURE_KEYS:
        value = expected.get(key)
        if value is not None:
            try:
                return abs(int(value))
            except (TypeError, ValueError):
                continue
    return 0


def _verdict(action: str, reason: str, exposure: int) -> dict[str, Any]:
    return {
        "action": action,
        "reason": reason,
        "exposure_minor_units": exposure,
        "reviewer": "autopilot",
    }
