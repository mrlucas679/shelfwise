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
from shelfwise_inference.config import load_inference_config
from shelfwise_inference.orchestration import (
    AgentOrchestrationError,
    AgentOrchestrator,
    AgentRunResult,
    ExecutionMode,
)
from shelfwise_inference.tool_calling import (
    ToolCallingError,
    assert_conclusion_grounded_in_tool_results,
)
from shelfwise_worldgen import create_world_snapshot_store

from .cascade import (
    _COLD_CHAIN_SCENARIO_ID,
    _GOLDEN_SCENARIO_ID,
    _PROCUREMENT_SCENARIO_ID,
    _SALES_SCENARIO_ID,
    _cause_id,
    _decision_id,
)
from .tenant import default_tenant_context
from .tools.mcp_surface import AuditLog, PlatformTool, build_platform_tools
from .tools.model_runtime import OpenAIModelRuntime, architecture_from_inference_config
from .world_facts import WorldFactsProvider

_default_facts_store: Any = None


def _default_facts() -> WorldFactsProvider:
    """Lazily-shared facts provider for callers that don't inject their own."""
    global _default_facts_store
    if _default_facts_store is None:
        _default_facts_store = create_world_snapshot_store()
    return WorldFactsProvider(_default_facts_store)


def _tenant_id(event: Event | None) -> str:
    return event.tenant_id if event is not None else default_tenant_context().tenant_id

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

_PROCUREMENT_CRITIC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "conclusion": {"type": "string", "minLength": 1, "maxLength": 600},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "critic_passed": {"type": "boolean"},
        "supplier_id": {"type": "string", "minLength": 1, "maxLength": 80},
        "requires_human_review": {"type": "boolean"},
    },
    "required": [
        "conclusion",
        "confidence",
        "critic_passed",
        "supplier_id",
        "requires_human_review",
    ],
    "additionalProperties": False,
}

_PROCUREMENT_EXECUTIVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "conclusion": {"type": "string", "minLength": 1, "maxLength": 600},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "recommended_action_type": {"type": "string", "enum": ["reorder", "monitor"]},
    },
    "required": ["conclusion", "confidence", "recommended_action_type"],
    "additionalProperties": False,
}

_SALES_CRITIC_SCHEMA: dict[str, Any] = {
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

_SALES_EXECUTIVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "conclusion": {"type": "string", "minLength": 1, "maxLength": 600},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "recommended_action_type": {
            "type": "string",
            "enum": ["record_sale", "review_price_exception"],
        },
    },
    "required": ["conclusion", "confidence", "recommended_action_type"],
    "additionalProperties": False,
}

_COLD_CHAIN_CRITIC_SCHEMA: dict[str, Any] = {
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

_COLD_CHAIN_EXECUTIVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "conclusion": {"type": "string", "minLength": 1, "maxLength": 600},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "recommended_action_type": {
            "type": "string",
            "enum": ["dispatch_facilities_check", "monitor_cold_chain"],
        },
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
    facts: WorldFactsProvider | None = None,
    orchestrator_factory: Any = None,
) -> dict[str, Any]:
    """Run the golden scenario's Critic + Executive reasoning through real Gemma tool calls."""
    return asyncio.run(
        _run(
            event,
            execution_mode=execution_mode,
            decisions=decisions,
            memory=memory,
            facts=facts,
            orchestrator_factory=orchestrator_factory,
        )
    )


async def _run(
    event: Event | None,
    *,
    execution_mode: ExecutionMode,
    decisions: Any,
    memory: Any,
    facts: WorldFactsProvider | None,
    orchestrator_factory: Any,
) -> dict[str, Any]:
    resolved_facts = facts or _default_facts()
    tenant_id = event.tenant_id if event is not None else default_tenant_context().tenant_id
    scenario = resolved_facts.get_scenario_facts(tenant_id)
    sku = scenario.sku
    product = scenario.product_name
    correlation_id = event.correlation_id if event is not None else None

    audit = AuditLog()
    tools: list[PlatformTool] = build_platform_tools(
        decisions=decisions,
        memory=memory,
        audit=audit,
        facts=resolved_facts,
        tenant_id=tenant_id,
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
                "deciding. Never invent numbers - the tools are your calculator; use the "
                "exact figures they return. A 20% markdown is only sound if the simulated "
                "incremental profit is positive and the stock/expiry facts support it. Your "
                "conclusion must explain the math: state the specific numbers you computed "
                "(e.g. units on hand, incremental profit) and how they lead to your verdict, "
                "not just the verdict itself."
            ),
            user=(
                f"Evaluate whether a 20% markdown is justified for SKU {sku} ({product}). "
                "Call get_stock, then call simulate_markdown with discount_pct=0.2, then "
                "return your verdict, citing the exact numbers from those tool results."
            ),
            final_schema=_CRITIC_SCHEMA,
            final_schema_name="critic_verdict",
            correlation_id=correlation_id,
            tenant_id=tenant_id,
            temperature=0.0,
            require_tool_call_first=True,
        )
        critic_answer = critic_run.answer
        assert_conclusion_grounded_in_tool_results(
            str(critic_answer["conclusion"]), critic_run.tool_calls
        )
        executive_run = await orchestrator.run(
            role="executive",
            system=(
                "You are the ShelfWise Executive agent. The Critic has already evaluated "
                "the markdown recommendation for this SKU, citing real computed figures. "
                "Decide whether to route the action forward for manager approval "
                "(apply_markdown) or hold (monitor). Reference the Critic's specific "
                "numbers in your own conclusion rather than restating a generic summary."
            ),
            user=(
                f"SKU {sku} ({product}). Critic verdict: passed={critic_answer['critic_passed']}, "
                f"conclusion={critic_answer['conclusion']!r}. Decide the routing action."
            ),
            final_schema=_EXECUTIVE_SCHEMA,
            final_schema_name="executive_verdict",
            correlation_id=critic_run.correlation_id,
            tenant_id=tenant_id,
            temperature=0.0,
        )
        executive_answer = executive_run.answer
    except (AgentOrchestrationError, ToolCallingError) as exc:
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
    tenant_id = _tenant_id(event)
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
    decision_payload["tenant_id"] = tenant_id
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


def run_procurement_cascade_via_agents(
    event: Event | None = None,
    *,
    execution_mode: ExecutionMode = ExecutionMode.LIVE_REQUIRED,
    decisions: Any,
    memory: Any,
    facts: WorldFactsProvider | None = None,
    orchestrator_factory: Any = None,
) -> dict[str, Any]:
    """Run the procurement reorder/supplier decision through real Gemma tool calls."""
    return asyncio.run(
        _run_procurement(
            event,
            execution_mode=execution_mode,
            decisions=decisions,
            memory=memory,
            facts=facts,
            orchestrator_factory=orchestrator_factory,
        )
    )


async def _run_procurement(
    event: Event | None,
    *,
    execution_mode: ExecutionMode,
    decisions: Any,
    memory: Any,
    facts: WorldFactsProvider | None,
    orchestrator_factory: Any,
) -> dict[str, Any]:
    resolved_facts = facts or _default_facts()
    tenant_id = event.tenant_id if event is not None else default_tenant_context().tenant_id
    scenario = resolved_facts.get_scenario_facts(tenant_id)
    sku = scenario.sku
    product = scenario.product_name
    correlation_id = event.correlation_id if event is not None else None

    audit = AuditLog()
    tools: list[PlatformTool] = build_platform_tools(
        decisions=decisions,
        memory=memory,
        audit=audit,
        facts=resolved_facts,
        tenant_id=tenant_id,
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
                "You are the ShelfWise Procurement Critic agent. You must call "
                "get_reorder_policy and get_supplier_ranking to gather the real facts for "
                "this SKU before deciding. Never invent numbers - the tools are your "
                "calculator. A reorder is only justified if the reorder policy says a "
                "reorder is needed and a supplier with a real measured profile is available. "
                "Your conclusion must cite the specific figures you computed."
            ),
            user=(
                f"Decide whether to reorder SKU {sku} ({product}) and from which supplier. "
                "Call get_reorder_policy, then call get_supplier_ranking, then return your "
                "verdict citing the exact numbers from those tool results."
            ),
            final_schema=_PROCUREMENT_CRITIC_SCHEMA,
            final_schema_name="procurement_critic_verdict",
            correlation_id=correlation_id,
            tenant_id=tenant_id,
            temperature=0.0,
            require_tool_call_first=True,
        )
        critic_answer = critic_run.answer
        assert_conclusion_grounded_in_tool_results(
            str(critic_answer["conclusion"]), critic_run.tool_calls
        )
        executive_run = await orchestrator.run(
            role="executive",
            system=(
                "You are the ShelfWise Executive agent. The Procurement Critic has already "
                "evaluated whether to reorder this SKU, citing real computed figures. Decide "
                "whether to route the action forward for manager approval (reorder) or hold "
                "(monitor). Reference the Critic's specific numbers in your own conclusion "
                "rather than restating a generic summary."
            ),
            user=(
                f"SKU {sku} ({product}). Critic verdict: passed={critic_answer['critic_passed']}, "
                f"supplier={critic_answer['supplier_id']!r}, "
                f"conclusion={critic_answer['conclusion']!r}. Decide the routing action."
            ),
            final_schema=_PROCUREMENT_EXECUTIVE_SCHEMA,
            final_schema_name="procurement_executive_verdict",
            correlation_id=critic_run.correlation_id,
            tenant_id=tenant_id,
            temperature=0.0,
        )
        executive_answer = executive_run.answer
    except (AgentOrchestrationError, ToolCallingError) as exc:
        raise AgenticCascadeError(f"live agentic procurement cascade failed: {exc}") from exc

    return _build_procurement_result(
        event=event,
        scenario_sku=sku,
        product=product,
        critic_run=critic_run,
        critic_answer=critic_answer,
        executive_run=executive_run,
        executive_answer=executive_answer,
    )


def _build_procurement_result(
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
    tenant_id = _tenant_id(event)
    critic_passed = bool(critic_answer["critic_passed"])
    supplier_id = str(critic_answer["supplier_id"])
    action_type = executive_answer["recommended_action_type"]
    reorder = RecommendedAction(
        "reorder",
        {"sku": scenario_sku, "supplier_id": supplier_id},
        RiskTier.MEDIUM,
    )
    monitor = RecommendedAction("monitor", {"sku": scenario_sku}, RiskTier.LOW)
    routed_action = reorder if action_type == "reorder" else monitor

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
            recommended_action=reorder if critic_passed else monitor,
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
            f"Live Gemma agentic procurement verdict for SKU {scenario_sku}: "
            f"{executive_answer['conclusion']}"
        ),
    )
    decision_payload = decision.to_dict()
    decision_payload["tenant_id"] = tenant_id
    decision_payload["scenario_id"] = _PROCUREMENT_SCENARIO_ID
    decision_payload["role"] = "procurement_manager"
    decision_payload["critic_verdict"] = "approved" if critic_passed else "rejected"

    return {
        "correlation_id": correlation_id,
        "scenario": _PROCUREMENT_SCENARIO_ID,
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


def run_sales_cascade_via_agents(
    event: Event | None = None,
    *,
    execution_mode: ExecutionMode = ExecutionMode.LIVE_REQUIRED,
    decisions: Any,
    memory: Any,
    facts: WorldFactsProvider | None = None,
    orchestrator_factory: Any = None,
) -> dict[str, Any]:
    """Run the POS price-integrity verdict through real Gemma tool calls."""
    return asyncio.run(
        _run_sales(
            event,
            execution_mode=execution_mode,
            decisions=decisions,
            memory=memory,
            facts=facts,
            orchestrator_factory=orchestrator_factory,
        )
    )


async def _run_sales(
    event: Event | None,
    *,
    execution_mode: ExecutionMode,
    decisions: Any,
    memory: Any,
    facts: WorldFactsProvider | None,
    orchestrator_factory: Any,
) -> dict[str, Any]:
    resolved_facts = facts or _default_facts()
    tenant_id = event.tenant_id if event is not None else default_tenant_context().tenant_id
    scenario = resolved_facts.get_scenario_facts(tenant_id)
    sku = scenario.sku
    product = scenario.product_name
    correlation_id = event.correlation_id if event is not None else None
    # A deliberately mismatched till price - the deterministic cascade's own tolerance band
    # is +/-15%, so a 20% deviation is a genuine, non-borderline price exception worth an
    # agent catching, not a rounding/promotion variance that should pass silently.
    observed_unit_price = float(scenario.unit_price.amount) * 1.2

    audit = AuditLog()
    tools: list[PlatformTool] = build_platform_tools(
        decisions=decisions,
        memory=memory,
        audit=audit,
        facts=resolved_facts,
        tenant_id=tenant_id,
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
                "You are the ShelfWise Sales Critic agent. You must call "
                "check_price_integrity to gather the real facts for this SKU before "
                "deciding. Never invent numbers - the tools are your calculator. The sale "
                "can be recorded automatically only if the observed price matches the "
                "catalogue price; otherwise it must be flagged for manager review. Your "
                "conclusion must cite the specific figures you computed."
            ),
            user=(
                f"Check whether SKU {sku} ({product})'s till price integrity holds. Call "
                f"check_price_integrity with observed_unit_price={observed_unit_price:.2f}, "
                "then return your verdict citing the exact numbers from that tool result."
            ),
            final_schema=_SALES_CRITIC_SCHEMA,
            final_schema_name="sales_critic_verdict",
            correlation_id=correlation_id,
            tenant_id=tenant_id,
            temperature=0.0,
            require_tool_call_first=True,
        )
        critic_answer = critic_run.answer
        assert_conclusion_grounded_in_tool_results(
            str(critic_answer["conclusion"]), critic_run.tool_calls
        )
        executive_run = await orchestrator.run(
            role="executive",
            system=(
                "You are the ShelfWise Executive agent. The Sales Critic has already "
                "evaluated this till sale's price integrity, citing real computed figures. "
                "Decide whether to record the sale automatically (record_sale) or route it "
                "for manager review (review_price_exception). Reference the Critic's "
                "specific numbers in your own conclusion."
            ),
            user=(
                f"SKU {sku} ({product}). Critic verdict: passed={critic_answer['critic_passed']}, "
                f"conclusion={critic_answer['conclusion']!r}. Decide the routing action."
            ),
            final_schema=_SALES_EXECUTIVE_SCHEMA,
            final_schema_name="sales_executive_verdict",
            correlation_id=critic_run.correlation_id,
            tenant_id=tenant_id,
            temperature=0.0,
        )
        executive_answer = executive_run.answer
    except (AgentOrchestrationError, ToolCallingError) as exc:
        raise AgenticCascadeError(f"live agentic sales cascade failed: {exc}") from exc

    return _build_sales_result(
        event=event,
        scenario_sku=sku,
        product=product,
        critic_run=critic_run,
        critic_answer=critic_answer,
        executive_run=executive_run,
        executive_answer=executive_answer,
    )


def _build_sales_result(
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
    tenant_id = _tenant_id(event)
    critic_passed = bool(critic_answer["critic_passed"])
    action_type = executive_answer["recommended_action_type"]
    record_sale = RecommendedAction("record_sale", {"sku": scenario_sku}, RiskTier.LOW)
    review_exception = RecommendedAction(
        "review_price_exception", {"sku": scenario_sku}, RiskTier.MEDIUM
    )
    routed_action = record_sale if action_type == "record_sale" else review_exception

    tool_sources = tuple(
        SourceRef.tool(execution.name)
        for execution in (*critic_run.tool_calls, *executive_run.tool_calls)
    ) or (SourceRef.tool("gemma_agent_loop"),)

    evidence = [
        EvidenceObject(
            agent=AgentName.SALES,
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
            recommended_action=record_sale if critic_passed else review_exception,
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
            f"Live Gemma agentic sales verdict for SKU {scenario_sku}: "
            f"{executive_answer['conclusion']}"
        ),
    )
    decision_payload = decision.to_dict()
    decision_payload["tenant_id"] = tenant_id
    decision_payload["scenario_id"] = _SALES_SCENARIO_ID
    decision_payload["role"] = "sales_manager"
    decision_payload["critic_verdict"] = "approved" if critic_passed else "review_required"

    return {
        "correlation_id": correlation_id,
        "scenario": _SALES_SCENARIO_ID,
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


def run_cold_chain_cascade_via_agents(
    event: Event | None = None,
    *,
    execution_mode: ExecutionMode = ExecutionMode.LIVE_REQUIRED,
    decisions: Any,
    memory: Any,
    facts: WorldFactsProvider | None = None,
    orchestrator_factory: Any = None,
) -> dict[str, Any]:
    """Run the cold-chain facilities-escalation verdict through real Gemma tool calls."""
    return asyncio.run(
        _run_cold_chain(
            event,
            execution_mode=execution_mode,
            decisions=decisions,
            memory=memory,
            facts=facts,
            orchestrator_factory=orchestrator_factory,
        )
    )


async def _run_cold_chain(
    event: Event | None,
    *,
    execution_mode: ExecutionMode,
    decisions: Any,
    memory: Any,
    facts: WorldFactsProvider | None,
    orchestrator_factory: Any,
) -> dict[str, Any]:
    resolved_facts = facts or _default_facts()
    payload = event.payload if event is not None else {}
    tenant_id = event.tenant_id if event is not None else default_tenant_context().tenant_id
    scenario = resolved_facts.get_scenario_facts(tenant_id)
    asset_id = str(
        payload.get("asset_id") or f"cold-chain:{scenario.location}:{scenario.category}"
    )
    outage_hours = float(payload.get("measured_outage_hours") or 4.0)
    average_temp_c = float(payload.get("temp_c") or 8.2)
    correlation_id = event.correlation_id if event is not None else None

    audit = AuditLog()
    tools: list[PlatformTool] = build_platform_tools(
        decisions=decisions,
        memory=memory,
        audit=audit,
        facts=resolved_facts,
        tenant_id=tenant_id,
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
                "You are the ShelfWise Cold Chain Critic agent. You must call "
                "get_cold_chain_status to gather the real measured risk for this "
                "refrigeration area before deciding. Never invent numbers - the tools are "
                "your calculator. A facilities dispatch is only justified if the measured "
                "cold-chain risk is meaningfully elevated. Your conclusion must cite the "
                "specific figures you computed."
            ),
            user=(
                f"Assess cold-chain risk for {asset_id} with a {outage_hours:.0f} hour "
                f"outage at {average_temp_c:.1f}C. Call get_cold_chain_status with "
                f"area={asset_id!r}, outage_hours={outage_hours}, "
                f"average_temp_c={average_temp_c}, then return your verdict citing the "
                "exact numbers from that tool result."
            ),
            final_schema=_COLD_CHAIN_CRITIC_SCHEMA,
            final_schema_name="cold_chain_critic_verdict",
            correlation_id=correlation_id,
            tenant_id=event.tenant_id if event is not None else default_tenant_context().tenant_id,
            temperature=0.0,
            require_tool_call_first=True,
        )
        critic_answer = critic_run.answer
        assert_conclusion_grounded_in_tool_results(
            str(critic_answer["conclusion"]), critic_run.tool_calls
        )
        executive_run = await orchestrator.run(
            role="executive",
            system=(
                "You are the ShelfWise Executive agent. The Cold Chain Critic has already "
                "evaluated this refrigeration alert, citing real computed figures. Decide "
                "whether to dispatch a facilities check (dispatch_facilities_check) or "
                "continue monitoring (monitor_cold_chain). Reference the Critic's specific "
                "numbers in your own conclusion."
            ),
            user=(
                f"{asset_id}. Critic verdict: passed={critic_answer['critic_passed']}, "
                f"conclusion={critic_answer['conclusion']!r}. Decide the routing action."
            ),
            final_schema=_COLD_CHAIN_EXECUTIVE_SCHEMA,
            final_schema_name="cold_chain_executive_verdict",
            correlation_id=critic_run.correlation_id,
            tenant_id=event.tenant_id if event is not None else default_tenant_context().tenant_id,
            temperature=0.0,
        )
        executive_answer = executive_run.answer
    except (AgentOrchestrationError, ToolCallingError) as exc:
        raise AgenticCascadeError(f"live agentic cold-chain cascade failed: {exc}") from exc

    return _build_cold_chain_result(
        event=event,
        asset_id=asset_id,
        critic_run=critic_run,
        critic_answer=critic_answer,
        executive_run=executive_run,
        executive_answer=executive_answer,
    )


def _build_cold_chain_result(
    *,
    event: Event | None,
    asset_id: str,
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
    dispatch = RecommendedAction(
        "dispatch_facilities_check", {"asset_id": asset_id}, RiskTier.HIGH
    )
    monitor = RecommendedAction("monitor_cold_chain", {"asset_id": asset_id}, RiskTier.LOW)
    routed_action = dispatch if action_type == "dispatch_facilities_check" else monitor

    tool_sources = tuple(
        SourceRef.tool(execution.name)
        for execution in (*critic_run.tool_calls, *executive_run.tool_calls)
    ) or (SourceRef.tool("gemma_agent_loop"),)

    evidence = [
        EvidenceObject(
            agent=AgentName.COLD_CHAIN,
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
            recommended_action=dispatch if critic_passed else monitor,
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
            f"Live Gemma agentic cold-chain verdict for {asset_id}: "
            f"{executive_answer['conclusion']}"
        ),
    )
    decision_payload = decision.to_dict()
    decision_payload["tenant_id"] = _tenant_id(event)
    decision_payload["scenario_id"] = _COLD_CHAIN_SCENARIO_ID
    decision_payload["role"] = "facilities_manager"
    decision_payload["critic_verdict"] = "approved" if critic_passed else "rejected"

    return {
        "correlation_id": correlation_id,
        "scenario": _COLD_CHAIN_SCENARIO_ID,
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
