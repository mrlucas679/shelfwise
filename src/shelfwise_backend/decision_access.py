"""Tenant-scoped decision filtering and cross-tenant access control.

Pulled out of `app.py` as a cohesive, single-concern slice: every function here answers
one question - "which tenant does this decision belong to, and is the caller allowed to
see or mutate it" - and depends only on the shared `decision_store` singleton
(`state.py`) and `_auth_mode` (`deps.py`). This is the real prerequisite HANDOFF.md
flagged for a clean `/mlops/*` route extraction: those routes read `decision_store`
through `_tenant_scoped_decisions`, not directly.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from .deps import _auth_mode
from .state import decision_store
from .tenant import TenantContext


def tenant_scoped_decisions(
    ctx: TenantContext, *, data_domain: str | None = None
) -> list[dict[str, Any]]:
    """Filter decisions to the authenticated tenant when auth is actually enforced.

    In "off" mode there is exactly one (default) tenant context for the whole process,
    so filtering would be a no-op - skipped there to avoid touching the many callers
    exercised by the default local/test/demo configuration.
    """
    decisions = [
        item
        for item in decision_store.list()
        if data_domain is None or str(item.get("data_domain") or "world_simulation") == data_domain
    ]
    if _auth_mode() != "jwt":
        return decisions
    return [item for item in decisions if _owned_by(item, ctx.tenant_id)]


def decision_belongs_to_other_tenant(decision: dict[str, Any], ctx: TenantContext) -> bool:
    if _auth_mode() != "jwt":
        return False
    return not _owned_by(decision, ctx.tenant_id)


def _owned_by(decision: dict[str, Any], tenant_id: str) -> bool:
    """Fail-closed ownership check: a decision with no stamped tenant belongs to nobody.

    `decision_tenant_id`'s fallback-to-caller behavior is correct for display purposes
    (labeling an unstamped decision in a response) but wrong for an ownership check - it
    makes an untagged row match *whichever* tenant happens to be asking, which is fail-open
    for exactly the row shape that's least trustworthy (a missing tenant column). A
    genuinely tenant-less row should be visible/actionable by no one, not everyone.
    """
    stamped = str(decision.get("tenant_id") or "").strip()
    return bool(stamped) and stamped == tenant_id


def reject_cross_tenant_decision_access(decision_id: str, ctx: TenantContext) -> None:
    """404 before any mutation if the decision belongs to a different tenant.

    Checked ahead of the approve/reject store call (not just on read) so a cross-tenant
    approval can never execute even against the in-memory backend, which has no RLS
    backstop. A genuinely-missing decision is left to the normal 404 the store call
    already raises, so this only short-circuits on an actual ownership mismatch.
    """
    if _auth_mode() != "jwt":
        return
    existing = decision_store.get(decision_id)
    if existing is not None and decision_belongs_to_other_tenant(existing, ctx):
        raise HTTPException(status_code=404, detail="Decision not found")


def decision_action(decision: dict[str, Any]) -> dict[str, Any]:
    """Return the decision action payload as a task-safe dictionary."""
    action = decision.get("action")
    return action if isinstance(action, dict) else {}


def decision_tenant_id(decision: dict[str, Any], fallback: str) -> str:
    """Return the persisted decision tenant, falling back to the authenticated tenant."""
    tenant_id = str(decision.get("tenant_id") or "").strip()
    return tenant_id or fallback
