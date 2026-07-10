from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True, slots=True)
class OutcomeRecord:
    tenant_id: str
    sku: str
    action: str
    success_score: Decimal
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TenantFact:
    id: str
    tenant_id: str
    sku: str
    action: str
    fact: str
    support_count: int
    confidence: Decimal
    evidence_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "sku": self.sku,
            "action": self.action,
            "fact": self.fact,
            "support_count": self.support_count,
            "confidence": str(self.confidence),
            "evidence_refs": list(self.evidence_refs),
        }


def consolidate_outcomes(
    records: list[OutcomeRecord],
    *,
    min_support: int = 2,
    min_success: Decimal = Decimal("0.70"),
) -> list[TenantFact]:
    groups: dict[tuple[str, str, str], list[OutcomeRecord]] = defaultdict(list)
    for record in records:
        if record.success_score >= min_success:
            groups[(record.tenant_id, record.sku, record.action)].append(record)

    facts: list[TenantFact] = []
    for (tenant_id, sku, action), group in sorted(groups.items()):
        if len(group) < min_support:
            continue
        avg = sum((item.success_score for item in group), Decimal("0")) / Decimal(len(group))
        refs = tuple(sorted({ref for item in group for ref in item.evidence_refs}))
        facts.append(
            TenantFact(
                id=f"fact_{tenant_id}_{sku}_{action}".replace(":", "_"),
                tenant_id=tenant_id,
                sku=sku,
                action=action,
                fact=f"{action} has repeatedly worked for SKU {sku}.",
                support_count=len(group),
                confidence=avg.quantize(Decimal("0.01")),
                evidence_refs=refs,
            )
        )
    return facts
