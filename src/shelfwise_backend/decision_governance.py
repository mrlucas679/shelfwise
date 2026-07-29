"""Decision economics and governance metadata attachment.

Pulled out of `app.py` as the first, safest slice of its ~12-function ingest-adjacent
core (see `HANDOFF.md`'s 2026-07-23 entry for the full call graph and the reasoning
behind extracting it in this order). Deliberately self-contained: every function here is
a pure computation over an already-built result dict - no store I/O, no event durability,
no cascade dispatch. That is what makes this piece safe to move first, ahead of
`_project_twin_event` and the `_record_pipeline_event` ingest core itself.
"""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from typing import Any

from shelfwise_contracts import Money
from shelfwise_mlops import decision_economics


def attach_decision_governance(result: dict[str, Any]) -> None:
    decision = result.get("decision")
    if not isinstance(decision, dict):
        return
    expected = (
        decision.get("expected_outcome")
        if isinstance(decision.get("expected_outcome"), dict)
        else {}
    )
    recovered_cents = int(expected.get("incremental_profit_minor_units") or 0)
    measured_tokens = measured_model_call_tokens(result)
    economics_method = "estimated_deterministic_cascade_tokens"
    if measured_tokens is not None:
        total_tokens = measured_tokens
        economics_method = "measured_model_call_usage"
    else:
        total_tokens = estimate_cascade_tokens(result)
    rate = inference_rate()
    decision["economics"] = decision_economics(
        rand_recovered=Money(minor_units=recovered_cents, currency="ZAR"),
        total_tokens=total_tokens,
        rate_zar_per_1k=rate,
    )
    inference = result.get("inference") if isinstance(result.get("inference"), dict) else {}
    decision["governance"] = {
        "correlation_id": result.get("correlation_id"),
        "schema_version": "v1",
        "prompt_version": "deterministic-cascade:v1",
        "models": {
            "routine": inference.get("routine_model", "offline-routine"),
            "strong": inference.get("strong_model", "offline-strong"),
        },
        "provider": inference.get("provider", "offline"),
        "evidence_count": len(
            result.get("evidence") if isinstance(result.get("evidence"), list) else []
        ),
        "economics_method": economics_method,
    }


def measured_model_call_tokens(result: dict[str, Any]) -> int | None:
    """Sum real per-call token usage when a result carries genuine model call traces."""
    model_calls = result.get("model_calls")
    if not isinstance(model_calls, list) or not model_calls:
        return None
    total = 0
    for call in model_calls:
        if not isinstance(call, dict):
            return None
        usage = call.get("usage")
        if not isinstance(usage, dict):
            return None
        total += int(usage.get("total_tokens") or 0)
    return max(1, total)


def estimate_cascade_tokens(result: dict[str, Any]) -> int:
    text = " ".join(
        [
            str(result.get("scenario") or ""),
            str(result.get("decision") or ""),
            str(result.get("evidence") or ""),
            str(result.get("trace") or ""),
        ]
    )
    return max(1, (len(text) + 3) // 4)


def inference_rate() -> Decimal:
    raw = os.getenv("INFERENCE_ZAR_PER_1K", "0.004")
    try:
        rate = Decimal(raw)
    except (TypeError, ValueError, InvalidOperation):
        return Decimal("0.004")
    return max(rate, Decimal("0"))
