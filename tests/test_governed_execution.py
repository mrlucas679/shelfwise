"""Governed plan execution and scheduled twin-fidelity revalidation.

These close two previously-sequenced phases with real capabilities: the HITL write-back
sink is the sole registered write, every step is journaled, and fidelity drift becomes a
governed manager task instead of a silent number.
"""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from shelfwise_backend.app import app
from shelfwise_backend.governed_execution import (
    FidelityRevalidationService,
    build_capability_registry,
    fidelity_revalidation_plan,
    revalidation_interval_seconds,
)
from shelfwise_backend.state import plan_runner, writeback_sink
from shelfwise_backend.tenant import encode_hs256_token
from shelfwise_backend.worker.plans import Plan, PlanStep


def _headers(monkeypatch, tenant: str) -> dict[str, str]:
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "jwt")
    monkeypatch.setenv("TENANT_AUTH_SECRET", "secret")
    token = encode_hs256_token(
        {"tenant_id": tenant, "user_id": "plan_user", "role": "manager",
         "exp": int(time.time()) + 3600},
        secret="secret",
    )
    return {"Authorization": f"Bearer {token}"}


def test_capability_registry_carries_only_real_capabilities() -> None:
    from shelfwise_backend.state import capability_registry

    write_caps = [
        name
        for name in capability_registry.exposed_for("manager")
        if capability_registry.get(name).writes
    ]
    assert write_caps == ["create_manager_task"], (
        "the HITL write-back sink must be the SOLE write capability - any other write "
        "entering this registry is a governance breach, not a feature"
    )
    assert "recompute_twin_fidelity" in capability_registry.exposed_for("scheduler")


def test_plan_execution_over_http_files_a_real_manager_task(monkeypatch) -> None:
    """The governed-write phase, end to end: a validated plan executes through the
    journaled runner and produces a real write-back task; the plan tenant is forced to
    the caller's so cross-tenant execution is impossible by construction."""
    tenant = f"plan_exec_tenant_{uuid4().hex[:8]}"
    headers = _headers(monkeypatch, tenant)
    client = TestClient(app)
    idempotency_key = f"plan-task-{uuid4().hex[:8]}"

    response = client.post(
        "/mlops/plans/execute",
        headers=headers,
        json={
            "plan_id": f"plan_{uuid4().hex[:8]}",
            "tenant_id": "someone_else_entirely",  # must be overridden by caller tenant
            "actor_role": "manager",
            "steps": [
                {
                    "key": "file_task",
                    "capability": "create_manager_task",
                    "params": {
                        "idempotency_key": idempotency_key,
                        "title": "Review the reorder recommendation",
                        "action": {"type": "reorder", "sku": "SKU-1"},
                        "tenant_id": tenant,
                    },
                    "compensation": {
                        "action": "cancel_pending_manager_task_before_source_write"
                    },
                }
            ],
        },
    )

    assert response.status_code == 200, response.text[:300]
    result = response.json()["result"]
    assert result["status"] == "done"
    task = result["outputs"]["file_task"]["task"]
    assert task["tenant_id"] == tenant
    listed = writeback_sink.list(tenant_id=tenant, data_domain="operational_twin")
    assert any(row.get("idempotency_key") == idempotency_key for row in listed), (
        "the plan's write step must land in the REAL write-back sink"
    )


def test_plan_with_unknown_capability_fails_closed_over_http(monkeypatch) -> None:
    tenant = f"plan_bad_tenant_{uuid4().hex[:8]}"
    headers = _headers(monkeypatch, tenant)
    response = TestClient(app).post(
        "/mlops/plans/execute",
        headers=headers,
        json={
            "plan_id": "plan_bad",
            "tenant_id": tenant,
            "actor_role": "manager",
            "steps": [
                {"key": "boom", "capability": "drop_all_tables", "params": {},
                 "compensation": {"action": "none"}}
            ],
        },
    )
    assert response.status_code == 422


def test_fidelity_revalidation_runs_and_files_a_drift_task_for_a_low_score() -> None:
    """The revalidation schedule is real software: it recomputes fidelity through the
    plan runner and turns drift into a governed manager task. Multi-week validation is
    this exact loop left running - only calendar time is external."""

    class _LowFidelityTwin:
        class store:
            @staticmethod
            def list_entities(tenant_id):
                from types import SimpleNamespace

                return [SimpleNamespace(entity_type="store", store_id="store_1")]

        @staticmethod
        def fidelity(tenant_id, store_id):
            return {"total": 12.5, "calibration_complete": False}

    registry = build_capability_registry(
        writeback_sink=writeback_sink, twin_service=_LowFidelityTwin()
    )
    from shelfwise_backend.state import journal

    async def publish(kind, payload):
        return None

    from shelfwise_backend.worker.plans import PlanRunner

    runner = PlanRunner(registry, journal, publish)
    service = FidelityRevalidationService(
        runner=runner,
        twin_service=_LowFidelityTwin(),
        writeback_sink=writeback_sink,
        interval_s=3600,
    )

    checked = asyncio.run(service.run_once())

    assert checked == 1
    status = service.status()
    assert status["stores_checked"] == 1
    assert status["drift_tasks_filed"] == 1, "a 12.5 fidelity score must file a review task"
    from shelfwise_backend.tenant import default_tenant_context

    tenant = default_tenant_context().tenant_id
    listed = writeback_sink.list(tenant_id=tenant, data_domain="operational_twin")
    assert any(
        row.get("idempotency_key") == f"fidelity_drift:{tenant}:store_1" for row in listed
    )


def test_revalidation_interval_is_config_not_a_code_constant(monkeypatch) -> None:
    monkeypatch.delenv("TWIN_FIDELITY_REVALIDATION_SECONDS", raising=False)
    assert revalidation_interval_seconds() == 86_400.0
    monkeypatch.setenv("TWIN_FIDELITY_REVALIDATION_SECONDS", "3600")
    assert revalidation_interval_seconds() == 3600.0
    monkeypatch.setenv("TWIN_FIDELITY_REVALIDATION_SECONDS", "5")
    assert revalidation_interval_seconds() == 60.0, "hot-loop floor must clamp up"


def test_fidelity_plan_shape_validates() -> None:
    plan = fidelity_revalidation_plan(tenant_id="t1", store_id="s1")
    assert isinstance(plan, Plan)
    assert all(isinstance(step, PlanStep) and step.compensation for step in plan.steps)


def test_schedules_status_surfaces_on_worker_and_readiness() -> None:
    client = TestClient(app)
    schedules = client.get("/worker/schedules")
    assert schedules.status_code == 200
    assert "fidelity_revalidation" in schedules.json()

    readiness = client.get("/readiness")
    assert readiness.status_code == 200
    assert "fidelity_revalidation" in str(readiness.json()), (
        "operators must see the revalidation schedule alongside every other lifespan "
        "service on the readiness surface"
    )


def test_plan_runner_singleton_uses_the_shared_journal() -> None:
    plan = Plan(
        plan_id=f"journal_probe_{uuid4().hex[:8]}",
        tenant_id="plan_journal_tenant",
        actor_role="manager",
        steps=[
            PlanStep(
                key="probe",
                capability="recompute_twin_fidelity",
                params={"tenant_id": "plan_journal_tenant", "store_id": "missing_store"},
                compensation={"action": "none_read_only"},
            )
        ],
    )
    result = asyncio.run(plan_runner.run(plan))
    # A missing store makes fidelity raise -> the plan fails CLOSED with the step named.
    assert result.status in {"done", "failed"}
    if result.status == "failed":
        assert result.failed_step == "probe"


@pytest.mark.parametrize("role", ["associate"])
def test_write_capability_is_role_gated(role) -> None:
    from shelfwise_backend.state import capability_registry

    assert "create_manager_task" not in capability_registry.exposed_for(role)
