from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from shelfwise_runtime import DataDomain
from shelfwise_storage import bind_tenant_context, reset_tenant_context

from .journal import InMemoryJournal, PostgresJournal, journaled

CapabilityHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
Publish = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Capability:
    name: str
    handler: CapabilityHandler
    exposed_to: frozenset[str]
    writes: bool = False


class CapabilityRegistry:
    """Register capabilities once, expose them selectively by actor role."""

    def __init__(self) -> None:
        self._capabilities: dict[str, Capability] = {}

    def register(self, capability: Capability) -> None:
        if capability.name in self._capabilities:
            raise ValueError(f"capability already registered: {capability.name}")
        self._capabilities[capability.name] = capability

    def get(self, name: str) -> Capability | None:
        return self._capabilities.get(name)

    def exposed_for(self, role: str) -> list[str]:
        return sorted(
            name
            for name, capability in self._capabilities.items()
            if role in capability.exposed_to
        )


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1, max_length=64)
    capability: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)
    compensation: dict[str, Any] | None = None


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    data_domain: DataDomain = DataDomain.OPERATIONAL_TWIN
    actor_role: str = Field(min_length=1, max_length=64)
    steps: list[PlanStep] = Field(min_length=1, max_length=32)


@dataclass(slots=True)
class PlanResult:
    plan_id: str
    status: str
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    failed_step: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "status": self.status,
            "outputs": self.outputs,
            "failed_step": self.failed_step,
        }


class PlanRunner:
    """Run validated plan data over the existing durable step journal."""

    def __init__(
        self,
        registry: CapabilityRegistry,
        journal: InMemoryJournal | PostgresJournal,
        publish: Publish,
    ) -> None:
        self._registry = registry
        self._journal = journal
        self._publish = publish

    async def run(self, plan: Plan) -> PlanResult:
        tenant_token = bind_tenant_context(plan.tenant_id)
        try:
            return await self._run_bound(plan)
        finally:
            reset_tenant_context(tenant_token)

    async def _run_bound(self, plan: Plan) -> PlanResult:
        problems = validate_plan(plan, self._registry)
        if problems:
            return PlanResult(plan.plan_id, "failed", failed_step=f"validation: {problems[0]}")

        run_id = _plan_run_id(plan)
        self._journal.start_run(
            run_id,
            tenant_id=plan.tenant_id,
            data_domain=plan.data_domain.value,
        )
        result = PlanResult(plan.plan_id, "done")
        total = len(plan.steps)
        for index, step in enumerate(plan.steps, start=1):
            capability = self._registry.get(step.capability)
            if capability is None:
                result.status = "failed"
                result.failed_step = step.key
                break
            try:
                output = await self._run_step(run_id, step, capability)
            except Exception as exc:
                result.status = "failed"
                result.failed_step = step.key
                await self._publish(
                    "progress",
                    {
                        "plan_id": plan.plan_id,
                        "tenant_id": plan.tenant_id,
                        "data_domain": plan.data_domain.value,
                        "step": step.key,
                        "i": index,
                        "total": total,
                        "status": "failed",
                        "error": str(exc)[:200],
                    },
                )
                break
            result.outputs[step.key] = output
            await self._publish(
                "progress",
                {
                    "plan_id": plan.plan_id,
                    "tenant_id": plan.tenant_id,
                    "data_domain": plan.data_domain.value,
                    "step": step.key,
                    "i": index,
                    "total": total,
                    "status": "ok",
                },
            )
        self._journal.finish_run(run_id, status=result.status)
        return result

    async def _run_step(
        self,
        run_id: str,
        step: PlanStep,
        capability: Capability,
    ) -> dict[str, Any]:
        seen = self._journal.get(run_id, step.key)
        if seen is not None:
            return seen
        output = await capability.handler(dict(step.params))
        journaled(
            self._journal,
            run_id,
            step.key,
            lambda: output,
            compensation=step.compensation,
        )
        return output


def validate_plan(plan: Plan, registry: CapabilityRegistry) -> list[str]:
    """Validate exposure, unknown capabilities, duplicate keys, and write compensation."""
    problems: list[str] = []
    seen: set[str] = set()
    for step in plan.steps:
        if step.key in seen:
            problems.append(f"duplicate step key: {step.key}")
        seen.add(step.key)
        capability = registry.get(step.capability)
        if capability is None:
            problems.append(f"unknown capability: {step.capability}")
            continue
        if plan.actor_role not in capability.exposed_to:
            problems.append(
                f"capability not exposed to role {plan.actor_role}: {step.capability}"
            )
        if capability.writes and step.compensation is None:
            problems.append(f"write step without compensation: {step.key}")
        if capability.writes and plan.data_domain is not DataDomain.OPERATIONAL_TWIN:
            problems.append(f"write step outside operational domain: {step.key}")
    return problems


def _plan_run_id(plan: Plan) -> str:
    return f"plan:{plan.tenant_id}:{plan.data_domain.value}:{plan.plan_id}"
