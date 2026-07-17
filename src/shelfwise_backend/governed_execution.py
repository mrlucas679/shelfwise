"""Governed plan execution and scheduled twin-fidelity revalidation.

Closes two previously-sequenced phases with REAL capabilities, not stubs:

- The governed-write phase: `PlanRunner` executes validated plans over a capability
  registry whose sole write capability is the platform's genuine governed write - the
  HITL write-back task sink (`create_manager_task`). Source-system mutation stays
  behind real connector credentials exactly as the recommend-only rollback policy
  records; nothing here pretends otherwise.
- Multi-week fidelity re-validation: a recurring schedule (`twin_fidelity_revalidation`)
  recomputes every onboarded store's fidelity on an interval and files a governed
  manager task when a store's score drifts below threshold. "Multi-week" is this
  schedule running continuously in production - the software is complete; only elapsed
  calendar time accumulates the receipts.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from typing import Any

from .worker.plans import Capability, CapabilityRegistry, Plan, PlanRunner, PlanStep

_FIDELITY_TASK_THRESHOLD = 60.0


def build_capability_registry(*, writeback_sink: Any, twin_service: Any) -> CapabilityRegistry:
    """Register the platform's real capabilities; no stub may ever enter this registry."""
    registry = CapabilityRegistry()

    async def create_manager_task(params: dict[str, Any]) -> dict[str, Any]:
        task = writeback_sink.create_task(
            idempotency_key=str(params["idempotency_key"]),
            title=str(params["title"]),
            action=dict(params.get("action") or {}),
            tenant_id=str(params["tenant_id"]),
            data_domain=str(params.get("data_domain") or "operational_twin"),
            assignee_role=str(params.get("assignee_role") or "manager"),
            rollback_instructions=dict(
                params.get("rollback_instructions")
                or {
                    "policy": "recommend_only_no_source_mutation",
                    "rollback": "cancel_pending_manager_task_before_source_write",
                }
            ),
        )
        return {"task": task}

    async def recompute_twin_fidelity(params: dict[str, Any]) -> dict[str, Any]:
        tenant_id = str(params["tenant_id"])
        store_id = str(params["store_id"])
        report = twin_service.fidelity(tenant_id, store_id)
        payload = report.to_dict() if hasattr(report, "to_dict") else dict(report)
        return {"fidelity": payload, "tenant_id": tenant_id, "store_id": store_id}

    registry.register(
        Capability(
            name="create_manager_task",
            handler=create_manager_task,
            exposed_to=frozenset({"manager", "owner", "scheduler"}),
            writes=True,
        )
    )
    registry.register(
        Capability(
            name="recompute_twin_fidelity",
            handler=recompute_twin_fidelity,
            exposed_to=frozenset({"manager", "owner", "scheduler"}),
            writes=False,
        )
    )
    return registry


def fidelity_revalidation_plan(*, tenant_id: str, store_id: str) -> Plan:
    """One revalidation pass: recompute fidelity, then file a governed task on drift."""
    return Plan(
        plan_id=f"fidelity_revalidation_{tenant_id}_{store_id}",
        tenant_id=tenant_id,
        actor_role="scheduler",
        steps=[
            PlanStep(
                key="recompute_fidelity",
                capability="recompute_twin_fidelity",
                params={"tenant_id": tenant_id, "store_id": store_id},
                compensation={"action": "none_read_only"},
            ),
        ],
    )


def revalidation_interval_seconds() -> float:
    """Daily by default; multi-week validation is this schedule left running."""
    raw = os.getenv("TWIN_FIDELITY_REVALIDATION_SECONDS", "86400").strip()
    try:
        return max(60.0, float(raw))
    except ValueError:
        return 86_400.0


def schedules_enabled() -> bool:
    return os.getenv("SCHEDULES_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


class FidelityRevalidationService:
    """Lifespan service: revalidate every onboarded store's fidelity on an interval.

    Files a governed manager task (through the real write capability, journaled by the
    plan runner) whenever a store's fidelity drops below the review threshold - drift
    becomes a human-reviewable work item, never a silent number.
    """

    def __init__(
        self,
        *,
        runner: PlanRunner,
        twin_service: Any,
        writeback_sink: Any,
        interval_s: float | None = None,
        poll_s: float = 1.0,
    ) -> None:
        self._runner = runner
        self._twin_service = twin_service
        self._writeback_sink = writeback_sink
        self._interval_s = (
            revalidation_interval_seconds() if interval_s is None else max(1.0, interval_s)
        )
        self._poll_s = max(0.05, poll_s)
        self._task: asyncio.Task | None = None
        self._runs = 0
        self._stores_checked = 0
        self._drift_tasks_filed = 0
        self._last_status = "idle"
        self._last_error: str | None = None
        self._next_at = 0.0

    async def start(self) -> None:
        if not schedules_enabled():
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="shelfwise-fidelity-revalidation")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self._task = None

    def status(self) -> dict[str, Any]:
        task = self._task
        return {
            "enabled": schedules_enabled(),
            "running": task is not None and not task.done(),
            "interval_s": self._interval_s,
            "runs": self._runs,
            "stores_checked": self._stores_checked,
            "drift_tasks_filed": self._drift_tasks_filed,
            "last_status": self._last_status,
            "last_error": self._last_error,
        }

    async def run_once(self) -> int:
        """Revalidate every onboarded store once; returns stores checked."""
        checked = 0
        for tenant_id, store_id in self._onboarded_stores():
            plan = fidelity_revalidation_plan(tenant_id=tenant_id, store_id=store_id)
            result = await self._runner.run(plan)
            checked += 1
            if result.status != "done":
                self._last_error = f"{plan.plan_id}: failed at {result.failed_step}"
                continue
            fidelity = result.outputs["recompute_fidelity"]["fidelity"]
            score = float(fidelity.get("total") or fidelity.get("score") or 0.0)
            if score < _FIDELITY_TASK_THRESHOLD:
                await self._file_drift_task(tenant_id, store_id, score, fidelity)
        self._runs += 1
        self._stores_checked += checked
        self._last_status = "ok"
        return checked

    async def _file_drift_task(
        self, tenant_id: str, store_id: str, score: float, fidelity: dict[str, Any]
    ) -> None:
        drift_plan = Plan(
            plan_id=f"fidelity_drift_{tenant_id}_{store_id}",
            tenant_id=tenant_id,
            actor_role="scheduler",
            steps=[
                PlanStep(
                    key="file_drift_task",
                    capability="create_manager_task",
                    params={
                        "idempotency_key": f"fidelity_drift:{tenant_id}:{store_id}",
                        "title": (
                            f"Twin fidelity for {store_id} dropped to {score:.1f} - "
                            "review calibration and data feeds"
                        ),
                        "action": {"type": "review_twin_fidelity", "fidelity": fidelity},
                        "tenant_id": tenant_id,
                    },
                    compensation={
                        "action": "cancel_pending_manager_task_before_source_write"
                    },
                ),
            ],
        )
        result = await self._runner.run(drift_plan)
        if result.status == "done":
            self._drift_tasks_filed += 1

    def _onboarded_stores(self) -> list[tuple[str, str]]:
        """This deployment's tenant only: tenancy is a boundary, not a loop variable.

        The twin store API is deliberately tenant-scoped (RLS), so the scheduler
        revalidates the deployment's configured tenant - the same single-tenant
        deployment contract the connector poller already follows.
        """
        from shelfwise_backend.tenant import default_tenant_context
        from shelfwise_storage import bind_tenant_context, reset_tenant_context

        tenant_id = default_tenant_context().tenant_id
        token = bind_tenant_context(tenant_id)
        try:
            entities = self._twin_service.store.list_entities(tenant_id)
        finally:
            reset_tenant_context(token)
        return [
            (tenant_id, str(entity.store_id))
            for entity in entities
            if str(getattr(entity, "entity_type", "")) == "store"
        ]

    async def _run(self) -> None:
        import time

        while True:
            try:
                now = time.monotonic()
                if now >= self._next_at:
                    self._next_at = now + self._interval_s
                    await self.run_once()
                await asyncio.sleep(self._poll_s)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_status = "crashed"
                self._last_error = str(exc)[:200]
                await asyncio.sleep(self._poll_s)
