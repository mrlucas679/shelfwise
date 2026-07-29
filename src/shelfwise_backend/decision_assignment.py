"""Central work-account to operational-decision queue assignments."""

from __future__ import annotations

from typing import Any

from .tenant import Role

ALL_OPERATIONAL_DECISION_ROLES = frozenset(
    {
        "facilities_manager",
        "inventory_manager",
        "procurement_manager",
        "sales_manager",
        "store_manager",
    }
)

_ASSIGNED_DECISION_ROLES: dict[Role, frozenset[str]] = {
    Role.OWNER: ALL_OPERATIONAL_DECISION_ROLES,
    Role.EXECUTIVE: ALL_OPERATIONAL_DECISION_ROLES,
    Role.MANAGER: ALL_OPERATIONAL_DECISION_ROLES,
    Role.INVENTORY: frozenset({"inventory_manager", "procurement_manager"}),
    Role.ANALYST: frozenset(
        {"inventory_manager", "procurement_manager", "sales_manager"}
    ),
    Role.AUDITOR: ALL_OPERATIONAL_DECISION_ROLES,
}


def assigned_decision_roles(role: Role) -> tuple[str, ...]:
    """Return the stable operational queues relevant to a signed-in work role."""
    return tuple(sorted(_ASSIGNED_DECISION_ROLES[role]))


def filter_assigned_decisions(
    decisions: list[dict[str, Any]],
    *,
    role: Role,
) -> list[dict[str, Any]]:
    """Filter a tenant-scoped ledger without trusting a client-supplied role."""
    assigned_roles = _ASSIGNED_DECISION_ROLES[role]
    return [
        decision
        for decision in decisions
        if str(decision.get("role") or "store_manager") in assigned_roles
    ]
