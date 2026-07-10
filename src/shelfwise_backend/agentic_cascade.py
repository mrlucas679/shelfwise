from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

from shelfwise_contracts import (
    AgentName,
    Decision,
    DecisionStatus,
    Event,
    EvidenceObject,
    RecommendedAction,
    RiskTier,
    SourceRef,
)
from shelfwise_data import load_seeded_scenario
from shelfwise_inference.config import load_inference_config
from shelfwise_inference.orchestration import (
    AgentOrchestrationError,
    AgentOrchestrator,
    AgentRunResult,
    ExecutionMode,
)

from .cascade import _GOLDEN_SCENARIO_ID, _cause_id, _decision_id
from .tools.mcp_surface import AuditLog, PlatformTool, build_platform_tools
from .tools.model_runtime import OpenAIModelRuntime, architecture_from_inference_config

# Unlike run_golden_cascade (deterministic Python math plus hand-authored EvidenceObject
# literals), this path hands the same seeded facts to Gemma as read-only tools and requires
# a genuine model call + tool-calling loop for the Critic and Executive verdicts - proving
# simulated workload -> tool call -> Gemma inference -> agent decision connectivity end to
# end, rather than a deterministic cascade that merely reports inference config alongside it.

_CRITIC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "conclusion": {"type": "string", "minLength": 1, "maxLength": 600},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "critic_passed": {"type": "boolean"},
        "requires_human_review": {"type": "boolean"},
    },
    "required": ["conclusion", "confidence", "critic_passed", "requires_human_review"],
    "additionalProperties": False,
}

_EXECUTIVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "conclusion": {"type": "string", "minLength": 1, "maxLength": 600},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "recommended_action_type": {"type": "string", "enum": ["apply_markdown", "monitor"]},
    },
    "required": ["conclusion", "confidence", "recommended_action_type"],
    "additionalProperties": False,
}


class AgenticCascadeError(RuntimeError):
    """Raised when the live agentic golden cascade cannot produce a valid decision."""


def run_golden_cascade_via_agents(
    event: Event | None = None,
    *,
    execution_mode: ExecutionMode = ExecutionMode.LIVE_REQUIRED,
    decisions: Any,
    memory: Any,
    orchestrator_factory: Any = None,
) -> dict[str, Any]:
    """Run the golden scenario's Critic + Executive reasoning through real Gemma tool calls."""
    return asyncio.run(
        _run(
            event,
            execution_mode=execution_mode,
            decisions=decisions,
            memory=memory,
            orchestrator_factory=orchestrator_factory,
        )
    )


async def _run(
    event: Event | None,
    *,
    execution_mode: ExecutionMode,
    decisions: Any,
    memory: Any,
    orchestrator_factory: Any,
) -> dict[str, Any]:
    scenario = load_seeded_scenario()
    sku = scenario.sku
    product = scenario.product_name
    correlation_id = event.correlation_id if event is not None else None

    audit = AuditLog()
    tools: list[PlatformTool] = build_platform_tools(
        decisions=decisions, memory=memory, audit=audit
    )
    orchestrator = (
        orchestrator_factory()
        if orchestrator_factory is not None
        else _default_orchestrator(tools=tools, execution_mode=execution_mode)
    )

    try:
        critic_run = await orchestrator.run(
            role="critic",
            system=(
                "You are the ShelfWise Critic agent. You must call the get_stock and "
                "simulate_markdown tools to gather the real facts for this SKU before "
                "deciding. Never invent numbers. A 20% markdown is only sound if the "
                "simulated incremental profit is positive and the stock/expiry facts "
                "support it."
            ),
            user=(
                f"Evaluate whether a 20% markdown is justified for SKU {sku} ({product}). "
                "Call get_stock, then call simulate_markdown with discount_pct=0.2, then "
                "return your verdict."
            ),
            final_schema=_CRITIC_SCHEMA,
            final_schema_name="critic_verdict",
            correlation_id=correlation_id,
            tenant_id=event.tenant_id if event is not None else "sa_retail_demo",
            temperature=0.0,
            require_tool_call_first=True,
        )
        critic_answer = critic_run.answer
        executive_run = await orchestrator.run(
            role="executive",
            system=(
                "You are the ShelfWise Executive agent. The Critic has already evaluated "
                "the markdown recommendation for this SKU. Decide whether to route the "
                "action forward for manager approval (apply_markdown) or hold (monitor)."
            ),
            user=(
                f"SKU {sku} ({product}). Critic verdict: passed={critic_answer['critic_passed']}, "
                f"conclusion={critic_answer['conclusion']!r}. Decide the routing action."
            ),
            final_schema=_EXECUTIVE_SCHEMA,
            final_schema_name="executive_verdict",
            correlation_id=critic_run.correlation_id,
            tenant_id=event.tenant_id if event is not None else "sa_retail_demo",
            temperature=0.0,
        )
        executive_answer = executive_run.answer
    except AgentOrchestrationError as exc:
        raise AgenticCascadeError(f"live agentic golden cascade failed: {exc}") from exc

    return _build_result(
        event=event,
        scenario_sku=sku,
        product=product,
        critic_run=critic_run,
        critic_answer=critic_answer,
        executive_run=executive_run,
        executive_answer=executive_answer,
    )


def _default_orchestrator(
    *, tools: list[PlatformTool], execution_mode: ExecutionMode
) -> AgentOrchestrator:
    config = load_inference_config()
    architecture = architecture_from_inference_config(config)
    runtime = OpenAIModelRuntime(architecture=architecture, execution_mode=execution_mode)
    return AgentOrchestrator(tools=tools, model_runtime=runtime)


def _build_result(
    *,
    event: Event | None,
    scenario_sku: str,
    product: str,
    critic_run: AgentRunResult,
    critic_answer: dict[str, Any],
    executive_run: AgentRunResult,
    executive_answer: dict[str, Any],
) -> dict[str, Any]:
    correlation_id = (
        event.correlation_id if event is not None else critic_run.correlation_id
    )
    critic_passed = bool(critic_answer["critic_passed"])
    action_type = executive_answer["recommended_action_type"]
    markdown = RecommendedAction(
        "apply_markdown",
        {"sku": scenario_sku, "discount_pct": "0.20", "duration_hours": 24},
        RiskTier.HIGH,
    )
    monitor = RecommendedAction("monitor", {"sku": scenario_sku}, RiskTier.LOW)
    routed_action = markdown if action_type == "apply_markdown" else monitor

    tool_sources = tuple(
        SourceRef.tool(execution.name)
        for execution in (*critic_run.tool_calls, *executive_run.tool_calls)
    ) or (SourceRef.tool("gemma_agent_loop"),)

    evidence = [
        EvidenceObject(
            agent=AgentName.CRITIC,
            conclusion=str(critic_answer["conclusion"]),
            supporting_data=[
                {
                    "fact": "tool_calls",
                    "value": [call.name for call in critic_run.tool_calls],
                    "source": "gemma_tool_loop",
                    "method": "agent_orchestrator",
                }
            ],
            confidence=Decimal(str(critic_answer["confidence"])),
            recommended_action=markdown if critic_passed else monitor,
            sources=tool_sources,
            requires_human_review=bool(critic_answer["requires_human_review"]),
        ),
        EvidenceObject(
            agent=AgentName.EXECUTIVE,
            conclusion=str(executive_answer["conclusion"]),
            supporting_data=[
                {
                    "fact": "critic_passed",
                    "value": critic_passed,
                    "source": "agent:critic",
                    "method": "agent_orchestrator",
                }
            ],
            confidence=Decimal(str(executive_answer["confidence"])),
            recommended_action=routed_action,
            sources=(SourceRef.tool("agent:critic"),),
            requires_human_review=True,
        ),
    ]

    decision = Decision(
        id=_decision_id(event),
        status=DecisionStatus.PENDING,
        action=routed_action,
        caused_by=(_cause_id(event, correlation_id),),
        summary=(
            f"Live Gemma agentic verdict for SKU {scenario_sku}: "
            f"{executive_answer['conclusion']}"
        ),
    )
    decision_payload = decision.to_dict()
    decision_payload["tenant_id"] = event.tenant_id if event is not None else "sa_retail_demo"
    decision_payload["scenario_id"] = _GOLDEN_SCENARIO_ID
    decision_payload["role"] = "store_manager"
    decision_payload["critic_verdict"] = "approved" if critic_passed else "rejected"

    return {
        "correlation_id": correlation_id,
        "scenario": _GOLDEN_SCENARIO_ID,
        "agentic": True,
        "evidence": [item.to_dict() for item in evidence],
        "decision": decision_payload,
        "model_calls": [
            call.to_dict() for call in (*critic_run.model_calls, *executive_run.model_calls)
        ],
        "tool_calls": [
            call.to_tool_message()
            for call in (*critic_run.tool_calls, *executive_run.tool_calls)
        ],
        "inference": load_inference_config().to_public_dict(),
    }
