from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal

from shelfwise_contracts import Money

from .utils import clamp, decimal, q2, safe_div


@dataclass(frozen=True, slots=True)
class Relation:
    subject: str
    predicate: str
    object: str


class RelationStore:
    def __init__(self) -> None:
        self._out: dict[str, list[Relation]] = {}

    def add(self, relation: Relation) -> None:
        self._out.setdefault(relation.subject, []).append(relation)

    def related(self, node: str, predicate: str | None = None) -> list[str]:
        return sorted(
            {
                relation.object
                for relation in self._out.get(node, [])
                if predicate is None or relation.predicate == predicate
            }
        )

    def paths(self, source: str, target: str, *, max_hops: int = 3) -> list[list[str]]:
        found: list[list[str]] = []
        queue: deque[list[str]] = deque([[source]])
        while queue:
            path = queue.popleft()
            if len(path) - 1 >= max_hops:
                continue
            for next_node in self.related(path[-1]):
                if next_node in path:
                    continue
                if next_node == target:
                    found.append([*path, next_node])
                else:
                    queue.append([*path, next_node])
        return found


@dataclass(frozen=True, slots=True)
class SupplierProfile:
    supplier_id: str
    lead_time_days: Decimal
    fill_rate: Decimal
    unit_cost: Money


@dataclass(frozen=True, slots=True)
class SupplierScore:
    supplier_id: str
    score: Decimal
    lead_time_days: Decimal
    fill_rate: Decimal
    unit_cost: Money

    def to_dict(self) -> dict[str, object]:
        return {
            "supplier_id": self.supplier_id,
            "score": str(self.score),
            "lead_time_days": str(self.lead_time_days),
            "fill_rate": str(self.fill_rate),
            "unit_cost": self.unit_cost.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class SupplierRanking:
    sku: str
    ranked: tuple[SupplierScore, ...]
    coverage: Decimal
    method: str

    def to_dict(self) -> dict[str, object]:
        return {
            "sku": self.sku,
            "ranked": [score.to_dict() for score in self.ranked],
            "coverage": str(self.coverage),
            "method": self.method,
        }


def recommend_suppliers(
    sku: str,
    graph: RelationStore,
    profiles: dict[str, SupplierProfile],
) -> SupplierRanking:
    candidates = graph.related(f"sku:{sku}", "supplied_by")
    profiled = [profiles[candidate] for candidate in candidates if candidate in profiles]
    lead_max = max((decimal(item.lead_time_days) for item in profiled), default=Decimal("1"))
    cost_max = max((decimal(item.unit_cost.minor_units) for item in profiled), default=Decimal("1"))
    scores = [
        SupplierScore(
            supplier_id=item.supplier_id,
            score=q2(
                Decimal("0.50") * clamp(item.fill_rate)
                + Decimal("0.30")
                * (Decimal("1") - clamp(safe_div(item.lead_time_days, lead_max, Decimal("1"))))
                + Decimal("0.20")
                * (
                    Decimal("1")
                    - clamp(safe_div(item.unit_cost.minor_units, cost_max, Decimal("1")))
                )
            ),
            lead_time_days=decimal(item.lead_time_days),
            fill_rate=decimal(item.fill_rate),
            unit_cost=item.unit_cost,
        )
        for item in profiled
    ]
    ranked = sorted(scores, key=lambda item: (-item.score, item.supplier_id))
    coverage = Decimal("0") if not candidates else q2(safe_div(len(profiled), len(candidates)))
    return SupplierRanking(
        sku=sku,
        ranked=tuple(ranked),
        coverage=coverage,
        method="graph_candidates_weighted_fill_lead_cost",
    )
