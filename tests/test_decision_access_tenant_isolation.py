from __future__ import annotations

from shelfwise_backend.decision_access import (
    decision_belongs_to_other_tenant,
    tenant_scoped_decisions,
)
from shelfwise_backend.tenant import Role, TenantContext

TENANT_A = TenantContext(tenant_id="tenant-a", user_id="user-a", role=Role.OWNER)
TENANT_B = TenantContext(tenant_id="tenant-b", user_id="user-b", role=Role.OWNER)


def test_untagged_decision_is_excluded_from_every_tenants_view(monkeypatch) -> None:
    """A decision missing `tenant_id` must be fail-closed (visible to nobody), not fail-open
    (visible to whichever tenant happens to be asking, because an empty field defaulted to
    the caller's own tenant_id and then trivially matched itself)."""
    monkeypatch.setattr("shelfwise_backend.decision_access._auth_mode", lambda: "jwt")
    untagged = {"id": "dec_1", "data_domain": "world_simulation"}
    monkeypatch.setattr(
        "shelfwise_backend.decision_access.decision_store.list", lambda: [untagged]
    )

    assert tenant_scoped_decisions(TENANT_A) == []
    assert tenant_scoped_decisions(TENANT_B) == []
    assert decision_belongs_to_other_tenant(untagged, TENANT_A) is True
    assert decision_belongs_to_other_tenant(untagged, TENANT_B) is True


def test_tagged_decision_is_visible_only_to_its_own_tenant(monkeypatch) -> None:
    monkeypatch.setattr("shelfwise_backend.decision_access._auth_mode", lambda: "jwt")
    tagged = {"id": "dec_2", "tenant_id": "tenant-a", "data_domain": "world_simulation"}
    monkeypatch.setattr(
        "shelfwise_backend.decision_access.decision_store.list", lambda: [tagged]
    )

    assert tenant_scoped_decisions(TENANT_A) == [tagged]
    assert tenant_scoped_decisions(TENANT_B) == []
    assert decision_belongs_to_other_tenant(tagged, TENANT_A) is False
    assert decision_belongs_to_other_tenant(tagged, TENANT_B) is True
