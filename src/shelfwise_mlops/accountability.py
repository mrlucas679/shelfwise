from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from shelfwise_contracts import Money


@dataclass(frozen=True, slots=True)
class AccountabilityReport:
    tenant_id: str
    decisions_total: int
    approved_total: int
    rejected_total: int
    recovered: Money
    models_used: tuple[str, ...]
    prompt_versions: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "decisions_total": self.decisions_total,
            "approved_total": self.approved_total,
            "rejected_total": self.rejected_total,
            "recovered": self.recovered.to_dict(),
            "models_used": list(self.models_used),
            "prompt_versions": list(self.prompt_versions),
        }

    def to_markdown(self) -> str:
        return "\n".join(
            [
                f"# Accountability Report - {self.tenant_id}",
                f"Decisions: {self.decisions_total}",
                f"Approved: {self.approved_total}",
                f"Rejected: {self.rejected_total}",
                f"Recovered: {self.recovered}",
                f"Models: {', '.join(self.models_used) or 'none'}",
                f"Prompt versions: {', '.join(self.prompt_versions) or 'none'}",
            ]
        )


def build_accountability_report(
    *,
    tenant_id: str,
    decisions: list[dict[str, Any]],
    models_used: list[str],
    prompt_versions: list[str],
) -> AccountabilityReport:
    tenant_decisions = [
        decision
        for decision in decisions
        if str(decision.get("tenant_id") or "default") == tenant_id
    ]
    approved = [decision for decision in tenant_decisions if decision.get("status") == "approved"]
    rejected = [decision for decision in tenant_decisions if decision.get("status") == "rejected"]
    recovered_cents = Decimal("0")
    for decision in approved:
        outcome = decision.get("outcome") or {}
        money = outcome.get("rand_recovered") or {}
        recovered_cents += Decimal(str(money.get("minor_units") or 0))
    return AccountabilityReport(
        tenant_id=tenant_id,
        decisions_total=len(tenant_decisions),
        approved_total=len(approved),
        rejected_total=len(rejected),
        recovered=Money(minor_units=int(recovered_cents), currency="ZAR"),
        models_used=tuple(sorted(set(models_used))),
        prompt_versions=tuple(sorted(set(prompt_versions))),
    )
