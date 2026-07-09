from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .memory_consolidation import OutcomeRecord

MIN_SKILL_SUPPORT = 2
MIN_SKILL_SUCCESS_RATE = Decimal("0.70")


@dataclass(frozen=True, slots=True)
class Skill:
    id: str
    tenant_id: str
    name: str
    trigger: str
    steps: list[dict[str, Any]]
    derived_from: tuple[str, ...]
    success_rate: Decimal
    support: int
    status: str = "draft"
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        """Return an audit-friendly representation of this governed playbook."""

        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "trigger": self.trigger,
            "steps": self.steps,
            "derived_from": list(self.derived_from),
            "success_rate": str(self.success_rate),
            "support": self.support,
            "status": self.status,
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class SkillStats:
    records: dict[tuple[str, str], list[OutcomeRecord]] = field(
        default_factory=lambda: defaultdict(list)
    )

    def reflect(self, record: OutcomeRecord, *, trigger: str) -> None:
        """Fold one resolved decision outcome into a deterministic reflection bucket."""

        if not trigger:
            raise ValueError("trigger is required")
        self.records[(trigger, record.action)].append(record)


def draft_skills(
    stats: SkillStats,
    *,
    tenant_id: str,
    step_template: dict[str, list[dict[str, Any]]],
    min_support: int = MIN_SKILL_SUPPORT,
    min_success_rate: Decimal = MIN_SKILL_SUCCESS_RATE,
) -> list[Skill]:
    """Draft playbooks only for repeated patterns with enough measured success."""

    drafts: list[Skill] = []
    for (trigger, action), group in sorted(stats.records.items()):
        tenant_group = [record for record in group if record.tenant_id == tenant_id]
        if len(tenant_group) < min_support:
            continue
        success_count = sum(
            1 for record in tenant_group if record.success_score >= min_success_rate
        )
        success_rate = Decimal(success_count) / Decimal(len(tenant_group))
        steps = step_template.get(action)
        if success_rate < min_success_rate or not steps:
            continue

        drafts.append(
            Skill(
                id=_skill_id(tenant_id, trigger, action),
                tenant_id=tenant_id,
                name=f"{action} when {trigger}",
                trigger=trigger,
                steps=[dict(step) for step in steps],
                derived_from=_evidence_refs(tenant_group),
                success_rate=success_rate.quantize(Decimal("0.01")),
                support=len(tenant_group),
            )
        )
    return drafts


def activate(skill: Skill) -> Skill:
    """Activate a reviewed draft skill without mutating the draft artifact."""

    if skill.status != "draft":
        raise ValueError(f"only draft skills can be activated (status={skill.status})")
    return replace(skill, status="active")


def tombstone_skill(skill: Skill, *, reason: str) -> Skill:
    """Create a tombstoned copy when outcomes contradict a skill."""

    if not reason:
        raise ValueError("reason is required")
    return replace(skill, status=f"tombstoned:{reason[:80]}")


def to_plan(skill: Skill, *, plan_id: str, actor_role: str) -> dict[str, Any]:
    """Compile an active skill to the plan shape consumed by the worker runner."""

    if skill.status != "active":
        raise ValueError(f"only active skills run (status={skill.status})")
    return {
        "plan_id": plan_id,
        "tenant_id": skill.tenant_id,
        "actor_role": actor_role,
        "steps": [dict(step) for step in skill.steps],
    }


def _skill_id(tenant_id: str, trigger: str, action: str) -> str:
    raw = f"skill_{tenant_id}_{trigger}_{action}".lower()
    return "".join(char if char.isalnum() else "_" for char in raw).strip("_")


def _evidence_refs(records: list[OutcomeRecord]) -> tuple[str, ...]:
    refs: list[str] = []
    seen: set[str] = set()
    for record in records:
        for ref in record.evidence_refs:
            if ref not in seen:
                seen.add(ref)
                refs.append(ref)
    return tuple(refs)
