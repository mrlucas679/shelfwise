from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation
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
from shelfwise_inference.client import RunRecorder
from shelfwise_inference.config import load_inference_config
from shelfwise_inference.orchestration import (
    AgentOrchestrationError,
    AgentOrchestrator,
    AgentRunResult,
    CascadeDeadlineExceeded,
    ExecutionMode,
)
from shelfwise_inference.tool_calling import (
    ToolCallingError,
    assert_conclusion_grounded_in_tool_results,
)
from shelfwise_runtime import DataDomain

from .cascade import (
    _COLD_CHAIN_SCENARIO_ID,
    _EXPIRY_SCENARIO_ID,
    _GOLDEN_SCENARIO_ID,
    _PRICE_OUTLIER_SCENARIO_ID,
    _PROCUREMENT_SCENARIO_ID,
    _SALES_SCENARIO_ID,
    EXPIRY_REVIEW_MAX_DAYS,
    PRICE_EXCEPTION_TOLERANCE,
    _cause_id,
    _decision_id,
    _monitor_action,
)
from .cascade import _event_tenant_id as _tenant_id
from .product_policies import resolve_product_policy
from .tools.mcp_surface import AuditLog, PlatformTool, build_platform_tools
from .tools.model_runtime import OpenAIModelRuntime, architecture_from_inference_config
from .world_facts import WorldFactsProvider
from .world_facts import default_facts_provider as _default_facts

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

_EXPIRY_CRITIC_SCHEMA: dict[str, Any] = {
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

_EXPIRY_EXECUTIVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "conclusion": {"type": "string", "minLength": 1, "maxLength": 600},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "recommended_action_type": {
            "type": "string",
            "enum": ["review_expiry_markdown", "monitor"],
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


class AgenticCascadeDeadlineError(AgenticCascadeError):
    """Raised when a cascade is stopped early because it could not finish before its deadline.

    Carries enough of `CascadeDeadlineExceeded`'s evidence for the route handler to return a
    typed 503 instead of letting `enforce_request_deadline` middleware cut the connection while
    the cascade (and its GPU cost) keeps running - see orchestration.CascadeDeadlineExceeded.
    """

    def __init__(self, message: str, *, completed_model_calls: int, elapsed_ms: int) -> None:
        super().__init__(message)
        self.completed_model_calls = completed_model_calls
        self.elapsed_ms = elapsed_ms


def run_golden_cascade_via_agents(
    event: Event | None = None,
    *,
    execution_mode: ExecutionMode = ExecutionMode.LIVE_REQUIRED,
    decisions: Any,
    memory: Any,
    facts: WorldFactsProvider | None = None,
    orchestrator_factory: Any = None,
    audit: AuditLog | None = None,
    model_run_recorder: RunRecorder | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Run the golden scenario's Critic + Executive reasoning through real Gemma tool calls."""
    return _scope_result(
        asyncio.run(
            _run(
                event,
                execution_mode=execution_mode,
                decisions=decisions,
                memory=memory,
                facts=facts,
                orchestrator_factory=orchestrator_factory,
                audit=audit,
                model_run_recorder=model_run_recorder,
                deadline=deadline,
            )
        ),
        event,
    )


async def _run(
    event: Event | None,
    *,
    execution_mode: ExecutionMode,
    decisions: Any,
    memory: Any,
    facts: WorldFactsProvider | None,
    orchestrator_factory: Any,
    audit: AuditLog | None = None,
    model_run_recorder: RunRecorder | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    resolved_facts = facts or _default_facts()
    tenant_id = _tenant_id(event)
    scenario = resolved_facts.get_scenario_facts(tenant_id)
    sku = scenario.sku
    product = scenario.product_name
    correlation_id = event.correlation_id if event is not None else None

    audit = audit or AuditLog()
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
        else _default_orchestrator(
            tools=tools,
            execution_mode=execution_mode,
            recorder=_scoped_recorder(model_run_recorder, event),
        )
    )

    # The candidate markdown the agents evaluate comes from the product-policy layer,
    # exactly like the deterministic cascade - both paths must argue about the SAME
    # candidate, and neither may bury it as a literal in prompts or routing.
    product_policy = resolve_product_policy(scenario.category)
    discount_pct = product_policy.markdown_discount_pct
    discount_display = f"{int(Decimal(discount_pct) * 100)}%"

    try:
        critic_run = await orchestrator.run(
            role="critic",
            system=(
                "You are the ShelfWise Critic agent. You must call the get_stock and "
                "simulate_markdown tools to gather the real facts for this SKU before "
                "deciding. Never invent numbers - the tools are your calculator; use the "
                f"exact figures they return. A {discount_display} markdown is only sound "
                "if the simulated incremental profit is positive and the stock/expiry "
                "facts support it. Your conclusion must explain the math: state the "
                "specific numbers you computed (e.g. units on hand, incremental profit) "
                "and how they lead to your verdict, not just the verdict itself."
            ),
            user=(
                f"Evaluate whether a {discount_display} markdown is justified for SKU "
                f"{sku} ({product}). Call get_stock, then call simulate_markdown with "
                f"discount_pct={discount_pct}, then return your verdict, citing the "
                "exact numbers from those tool results."
            ),
            final_schema=_CRITIC_SCHEMA,
            final_schema_name="critic_verdict",
            correlation_id=correlation_id,
            tenant_id=tenant_id,
            temperature=0.0,
            max_tokens=_EXECUTIVE_VERDICT_MAX_TOKENS,
            require_tool_call_first=True,
            deadline=deadline,
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
                f"conclusion={_bounded_conclusion(str(critic_answer['conclusion']))!r}. "
                "Decide the routing action."
            ),
            final_schema=_EXECUTIVE_SCHEMA,
            final_schema_name="executive_verdict",
            correlation_id=critic_run.correlation_id,
            tenant_id=tenant_id,
            temperature=0.0,
            max_tokens=_EXECUTIVE_VERDICT_MAX_TOKENS,
            deadline=deadline,
        )
        executive_answer = executive_run.answer
    except (AgentOrchestrationError, ToolCallingError) as exc:
        raise AgenticCascadeError(f"live agentic golden cascade failed: {exc}") from exc
    except CascadeDeadlineExceeded as exc:
        raise AgenticCascadeDeadlineError(
            f"live agentic golden cascade could not finish inside the response deadline: {exc}",
            completed_model_calls=exc.completed_model_calls,
            elapsed_ms=exc.elapsed_ms,
        ) from exc

    return _build_result(
        event=event,
        scenario_sku=sku,
        product=product,
        critic_run=critic_run,
        critic_answer=critic_answer,
        executive_run=executive_run,
        executive_answer=executive_answer,
        product_policy=product_policy,
    )


def _default_orchestrator(
    *,
    tools: list[PlatformTool],
    execution_mode: ExecutionMode,
    recorder: RunRecorder | None = None,
) -> AgentOrchestrator:
    config = load_inference_config()
    architecture = architecture_from_inference_config(config)
    runtime = OpenAIModelRuntime(
        architecture=architecture,
        execution_mode=execution_mode,
        recorder=recorder,
    )
    return AgentOrchestrator(tools=tools, model_runtime=runtime)


def _event_data_domain(event: Event | None) -> str:
    """Keep manual/demo runs explicit while preserving an operational event's boundary."""
    return (
        event.data_domain.value
        if event is not None
        else DataDomain.WORLD_SIMULATION.value
    )


def _scoped_recorder(
    recorder: RunRecorder | None,
    event: Event | None,
) -> RunRecorder | None:
    """Attach the cascade domain before a model receipt reaches the shared registry."""
    if recorder is None:
        return None
    data_domain = _event_data_domain(event)

    def record(payload: dict[str, Any]) -> None:
        recorder({**payload, "data_domain": data_domain})

    return record


_EXECUTIVE_VERDICT_MAX_TOKENS = 400
"""Cap on every critic/executive final-verdict call (audit-measured ~19 effective tok/s
across both tiers made the prior default of 800 arithmetically unable to finish inside the
30s hackathon ceiling for the largest-evidence cascades - a decision plus cited figures does
not need an 800-token budget)."""

_EXECUTIVE_CONCLUSION_CHAR_BUDGET = 200


def _bounded_conclusion(conclusion: str, *, limit: int = _EXECUTIVE_CONCLUSION_CHAR_BUDGET) -> str:
    """Cap the critic conclusion text forwarded into the executive's strong-tier prompt.

    The critic schema allows up to 600 characters of prose. Quoting the full string into the
    executive's user message duplicates that prose into a second strong-tier prompt for no
    decision-relevant gain - the executive needs enough of the critic's reasoning to decide,
    not the whole essay (2026-07-14 forensic audit, SLO-fit finding).
    """
    if len(conclusion) <= limit:
        return conclusion
    return f"{conclusion[: limit - 1].rstrip()}…"


def _tool_result(tool_calls: tuple[Any, ...], name: str) -> dict[str, Any]:
    """Return the result payload of the first execution of a named tool, or `{}`.

    `Decision` objects built by these agentic cascades never carried `expected_outcome`,
    so `shelfwise_memory.record_approved_decision` always computed zero exposure and the
    harness's own `_learning_target` silently returned `None` for every one of them -
    every approved agentic decision produced zero learning movement, invisibly, because
    nothing ever checked a metric that was never populated (found 2026-07-15 by
    distrusting a run that reported 0 failures). The fix pulls the real number the tool
    already computed - never a fabricated or re-derived one - into `expected_outcome`.
    """
    for execution in tool_calls:
        if execution.name == name and isinstance(execution.result, dict):
            return execution.result
    return {}


def _decimal_from_tool_field(payload: dict[str, Any], key: str) -> Decimal | None:
    value = payload.get(key)
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _token_budget(*runs: AgentRunResult) -> dict[str, int]:
    """Summarize prompt/completion token cost across every model call in a cascade run.

    A `token_budget` receipt on every agentic response makes SLO regressions visible in every
    future run's evidence, not just the ones that time out.
    """
    calls = [call for run in runs for call in run.model_calls]
    return {
        "prompt_tokens": sum(call.input_tokens for call in calls),
        "completion_tokens": sum(call.output_tokens for call in calls),
        "calls": len(calls),
    }


def _enforce_critic_verdict(
    *,
    critic_passed: bool,
    executive_action: RecommendedAction,
    safe_action: RecommendedAction,
) -> tuple[RecommendedAction, bool]:
    """Make the critic's verdict binding on the executive's routing, not advisory.

    The critic's verdict reaches the executive only as prose inside a prompt, and prose
    is not enforcement: a hallucinating executive can answer with the escalating action
    even though the critic failed the work. The two guardrail cascades (catalog-price,
    expiry) already fail closed on such a downgrade; the four routing cascades silently
    trusted the executive. This gate closes that gap in the deterministic layer where a
    model cannot argue with it: a failed critic verdict always routes the safe action,
    and choosing the safe action is always allowed regardless of the critic (an
    executive may be more conservative than the critic, never less).

    Returns (final_action, override_applied) so callers can put the override on the
    decision record for auditability instead of hiding the disagreement.
    """
    if critic_passed or executive_action.type == safe_action.type:
        return executive_action, False
    return safe_action, True


def _critic_gate_receipt(
    *,
    critic_passed: bool,
    executive_action_type: str,
    override_applied: bool,
) -> dict[str, Any]:
    """One auditable record of what each agent said and what the gate did about it."""
    return {
        "critic_passed": critic_passed,
        "executive_action_type": executive_action_type,
        "override_applied": override_applied,
    }


def _scope_result(
    result: dict[str, Any] | None,
    event: Event | None,
) -> dict[str, Any] | None:
    """Stamp agent output and its decision before any store or trace can observe it."""
    if result is None:
        return None
    tenant_id = _tenant_id(event)
    data_domain = _event_data_domain(event)
    result["tenant_id"] = tenant_id
    result["data_domain"] = data_domain
    decision = result.get("decision")
    if isinstance(decision, dict):
        decision["tenant_id"] = tenant_id
        decision["data_domain"] = data_domain
    return result


def _build_result(
    *,
    event: Event | None,
    scenario_sku: str,
    product: str,
    critic_run: AgentRunResult,
    critic_answer: dict[str, Any],
    executive_run: AgentRunResult,
    executive_answer: dict[str, Any],
    product_policy: Any,
) -> dict[str, Any]:
    correlation_id = (
        event.correlation_id if event is not None else critic_run.correlation_id
    )
    tenant_id = _tenant_id(event)
    critic_passed = bool(critic_answer["critic_passed"])
    action_type = executive_answer["recommended_action_type"]
    markdown = RecommendedAction(
        "apply_markdown",
        {
            "sku": scenario_sku,
            "discount_pct": product_policy.markdown_discount_pct,
            "duration_hours": product_policy.markdown_duration_hours,
        },
        RiskTier.HIGH,
    )
    monitor = _monitor_action(scenario_sku)
    routed_action, gate_override = _enforce_critic_verdict(
        critic_passed=critic_passed,
        executive_action=markdown if action_type == "apply_markdown" else monitor,
        safe_action=monitor,
    )

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
    decision_payload["critic_gate"] = _critic_gate_receipt(
        critic_passed=critic_passed,
        executive_action_type=str(action_type),
        override_applied=gate_override,
    )
    markdown_sim = _tool_result(critic_run.tool_calls, "simulate_markdown")
    predicted_units = _decimal_from_tool_field(markdown_sim, "markdown_units_sold")
    if predicted_units is not None:
        expected_outcome: dict[str, Any] = {"predicted_sell_through_units": int(predicted_units)}
        incremental_profit = markdown_sim.get("incremental_profit")
        if isinstance(incremental_profit, dict) and "minor_units" in incremental_profit:
            # `/mlops`'s decision-economics dashboard (`_attach_decision_governance`) only
            # ever reads `incremental_profit_minor_units` - a decision missing it always
            # displayed "R0.00 recovered" for a real, successful markdown, independent of
            # (and undetected by) the learning-store fix above, which reads a different key.
            expected_outcome["incremental_profit_minor_units"] = int(
                incremental_profit["minor_units"]
            )
        decision_payload["expected_outcome"] = expected_outcome

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
        "token_budget": _token_budget(critic_run, executive_run),
    }


def run_procurement_cascade_via_agents(
    event: Event | None = None,
    *,
    execution_mode: ExecutionMode = ExecutionMode.LIVE_REQUIRED,
    decisions: Any,
    memory: Any,
    facts: WorldFactsProvider | None = None,
    orchestrator_factory: Any = None,
    audit: AuditLog | None = None,
    model_run_recorder: RunRecorder | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Run the procurement reorder/supplier decision through real Gemma tool calls."""
    return _scope_result(
        asyncio.run(
            _run_procurement(
                event,
                execution_mode=execution_mode,
                decisions=decisions,
                memory=memory,
                facts=facts,
                orchestrator_factory=orchestrator_factory,
                audit=audit,
                model_run_recorder=model_run_recorder,
                deadline=deadline,
            )
        ),
        event,
    )


async def _run_procurement(
    event: Event | None,
    *,
    execution_mode: ExecutionMode,
    decisions: Any,
    memory: Any,
    facts: WorldFactsProvider | None,
    orchestrator_factory: Any,
    audit: AuditLog | None = None,
    model_run_recorder: RunRecorder | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    resolved_facts = facts or _default_facts()
    tenant_id = _tenant_id(event)
    scenario = resolved_facts.get_scenario_facts(tenant_id)
    sku = scenario.sku
    product = scenario.product_name
    correlation_id = event.correlation_id if event is not None else None

    audit = audit or AuditLog()
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
        else _default_orchestrator(
            tools=tools,
            execution_mode=execution_mode,
            recorder=_scoped_recorder(model_run_recorder, event),
        )
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
            max_tokens=_EXECUTIVE_VERDICT_MAX_TOKENS,
            require_tool_call_first=True,
            deadline=deadline,
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
                f"conclusion={_bounded_conclusion(str(critic_answer['conclusion']))!r}. "
                "Decide the routing action."
            ),
            final_schema=_PROCUREMENT_EXECUTIVE_SCHEMA,
            final_schema_name="procurement_executive_verdict",
            correlation_id=critic_run.correlation_id,
            tenant_id=tenant_id,
            temperature=0.0,
            max_tokens=_EXECUTIVE_VERDICT_MAX_TOKENS,
            deadline=deadline,
        )
        executive_answer = executive_run.answer
    except (AgentOrchestrationError, ToolCallingError) as exc:
        raise AgenticCascadeError(f"live agentic procurement cascade failed: {exc}") from exc
    except CascadeDeadlineExceeded as exc:
        raise AgenticCascadeDeadlineError(
            f"live agentic procurement cascade could not finish inside the response "
            f"deadline: {exc}",
            completed_model_calls=exc.completed_model_calls,
            elapsed_ms=exc.elapsed_ms,
        ) from exc

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
    monitor = _monitor_action(scenario_sku)
    routed_action, gate_override = _enforce_critic_verdict(
        critic_passed=critic_passed,
        executive_action=reorder if action_type == "reorder" else monitor,
        safe_action=monitor,
    )

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
    decision_payload["critic_gate"] = _critic_gate_receipt(
        critic_passed=critic_passed,
        executive_action_type=str(action_type),
        override_applied=gate_override,
    )
    reorder_policy = _tool_result(critic_run.tool_calls, "get_reorder_policy")
    stockout_exposure = _decimal_from_tool_field(reorder_policy, "stockout_exposure_minor_units")
    if stockout_exposure is not None:
        # Procurement decisions had no learning-metric route at all (not degraded, entirely
        # absent) - approving a reorder never built organizational memory or showed real
        # recovered value on the `/mlops` economics dashboard, so the app could never
        # legitimately claim continuous learning for procurement, one of the store
        # positions the product is required to cover for real, not as a demo slice.
        exposure_minor_units = int(stockout_exposure)
        decision_payload["expected_outcome"] = {
            "stockout_exposure_minor_units": exposure_minor_units,
            "incremental_profit_minor_units": exposure_minor_units,
        }

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
        "token_budget": _token_budget(critic_run, executive_run),
    }


def run_sales_cascade_via_agents(
    event: Event | None = None,
    *,
    execution_mode: ExecutionMode = ExecutionMode.LIVE_REQUIRED,
    decisions: Any,
    memory: Any,
    facts: WorldFactsProvider | None = None,
    orchestrator_factory: Any = None,
    audit: AuditLog | None = None,
    model_run_recorder: RunRecorder | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Run the POS price-integrity verdict through real Gemma tool calls."""
    return _scope_result(
        asyncio.run(
            _run_sales(
                event,
                execution_mode=execution_mode,
                decisions=decisions,
                memory=memory,
                facts=facts,
                orchestrator_factory=orchestrator_factory,
                audit=audit,
                model_run_recorder=model_run_recorder,
                deadline=deadline,
            )
        ),
        event,
    )


async def _run_sales(
    event: Event | None,
    *,
    execution_mode: ExecutionMode,
    decisions: Any,
    memory: Any,
    facts: WorldFactsProvider | None,
    orchestrator_factory: Any,
    audit: AuditLog | None = None,
    model_run_recorder: RunRecorder | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    resolved_facts = facts or _default_facts()
    tenant_id = _tenant_id(event)
    scenario = resolved_facts.get_scenario_facts(tenant_id)
    sku = scenario.sku
    product = scenario.product_name
    correlation_id = event.correlation_id if event is not None else None
    # A deliberately mismatched till price - the deterministic cascade's own tolerance band
    # is +/-15%, so a 20% deviation is a genuine, non-borderline price exception worth an
    # agent catching, not a rounding/promotion variance that should pass silently.
    observed_unit_price = float(scenario.unit_price.amount) * 1.2

    audit = audit or AuditLog()
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
        else _default_orchestrator(
            tools=tools,
            execution_mode=execution_mode,
            recorder=_scoped_recorder(model_run_recorder, event),
        )
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
            max_tokens=_EXECUTIVE_VERDICT_MAX_TOKENS,
            require_tool_call_first=True,
            deadline=deadline,
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
                f"conclusion={_bounded_conclusion(str(critic_answer['conclusion']))!r}. "
                "Decide the routing action."
            ),
            final_schema=_SALES_EXECUTIVE_SCHEMA,
            final_schema_name="sales_executive_verdict",
            correlation_id=critic_run.correlation_id,
            tenant_id=tenant_id,
            temperature=0.0,
            max_tokens=_EXECUTIVE_VERDICT_MAX_TOKENS,
            deadline=deadline,
        )
        executive_answer = executive_run.answer
    except (AgentOrchestrationError, ToolCallingError) as exc:
        raise AgenticCascadeError(f"live agentic sales cascade failed: {exc}") from exc
    except CascadeDeadlineExceeded as exc:
        raise AgenticCascadeDeadlineError(
            f"live agentic sales cascade could not finish inside the response deadline: {exc}",
            completed_model_calls=exc.completed_model_calls,
            elapsed_ms=exc.elapsed_ms,
        ) from exc

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
    # Safe direction here is escalation to a human: a failed price-integrity critic must
    # never be waved through as a clean recorded sale by the executive.
    routed_action, gate_override = _enforce_critic_verdict(
        critic_passed=critic_passed,
        executive_action=record_sale if action_type == "record_sale" else review_exception,
        safe_action=review_exception,
    )

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
    decision_payload["critic_gate"] = _critic_gate_receipt(
        critic_passed=critic_passed,
        executive_action_type=str(action_type),
        override_applied=gate_override,
    )
    price_check = _tool_result(critic_run.tool_calls, "check_price_integrity")
    price_delta = _decimal_from_tool_field(price_check, "price_delta")
    # Key economics off the FINAL routed action, not the executive's raw answer - the
    # critic gate can turn an executive "record_sale" into a review_price_exception, and
    # that overridden decision still carries real revenue exposure.
    if price_delta is not None and routed_action.type != "record_sale":
        exposure_minor_units = int((abs(price_delta) * 100).to_integral_value())
        # `/mlops`'s decision-economics dashboard reads only `incremental_profit_minor_units`
        # - without it every agentic price exception displayed "R0.00 recovered" regardless
        # of the learning-store fix above, which reads a different key.
        decision_payload["expected_outcome"] = {
            "revenue_exposure_minor_units": exposure_minor_units,
            "incremental_profit_minor_units": exposure_minor_units,
        }

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
        "token_budget": _token_budget(critic_run, executive_run),
    }


def run_catalog_price_check_via_agents(
    event: Event,
    *,
    execution_mode: ExecutionMode = ExecutionMode.LIVE_REQUIRED,
    decisions: Any,
    memory: Any,
    facts: WorldFactsProvider | None = None,
    orchestrator_factory: Any = None,
    audit: AuditLog | None = None,
    model_run_recorder: RunRecorder | None = None,
    deadline: float | None = None,
) -> dict[str, Any] | None:
    """Run a POS price-outlier event through real Gemma tool calls."""
    return _scope_result(
        asyncio.run(
            _run_catalog_price_check(
                event,
                execution_mode=execution_mode,
                decisions=decisions,
                memory=memory,
                facts=facts,
                orchestrator_factory=orchestrator_factory,
                audit=audit,
                model_run_recorder=model_run_recorder,
                deadline=deadline,
            )
        ),
        event,
    )


async def _run_catalog_price_check(
    event: Event,
    *,
    execution_mode: ExecutionMode,
    decisions: Any,
    memory: Any,
    facts: WorldFactsProvider | None,
    orchestrator_factory: Any,
    audit: AuditLog | None = None,
    model_run_recorder: RunRecorder | None = None,
    deadline: float | None = None,
) -> dict[str, Any] | None:
    payload = event.payload
    try:
        unit_price_cents = int(payload["unit_price_cents"])
        catalog_price_cents = int(payload["catalog_price_cents"])
    except (KeyError, TypeError, ValueError):
        return None
    if catalog_price_cents <= 0 or unit_price_cents < 0:
        return None

    delta_pct = (
        Decimal(unit_price_cents) - Decimal(catalog_price_cents)
    ) / Decimal(catalog_price_cents)
    if abs(delta_pct) <= PRICE_EXCEPTION_TOLERANCE:
        return None

    resolved_facts = facts or _default_facts()
    tenant_id = _tenant_id(event)
    sku = str(payload.get("sku") or resolved_facts.get_hero_sku(tenant_id))
    scenario = resolved_facts.get_scenario_facts(tenant_id, sku)
    observed = Decimal(unit_price_cents) / Decimal("100")
    catalog = Decimal(catalog_price_cents) / Decimal("100")
    units = max(1, int(payload.get("units") or 1))

    audit = audit or AuditLog()
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
        else _default_orchestrator(
            tools=tools,
            execution_mode=execution_mode,
            recorder=_scoped_recorder(model_run_recorder, event),
        )
    )

    try:
        critic_run = await orchestrator.run(
            role="critic",
            system=(
                "You are the ShelfWise POS Price Critic agent. You must call "
                "check_price_integrity to gather the real catalogue comparison before "
                "deciding. Never invent numbers - the tool is your calculator. A sale "
                "outside the configured variance band must remain pending for manager "
                "review, not be recorded automatically. Cite the exact observed price, "
                "catalogue price, or computed delta from the tool result."
            ),
            user=(
                f"Review event {event.id} for SKU {sku} ({scenario.product_name}). "
                f"The POS observed unit price is {observed}. Call check_price_integrity "
                f"with sku={sku!r} and observed_unit_price={float(observed):.2f}, then "
                "return a verdict citing the exact tool numbers."
            ),
            final_schema=_SALES_CRITIC_SCHEMA,
            final_schema_name="catalog_price_critic_verdict",
            correlation_id=event.correlation_id,
            tenant_id=tenant_id,
            temperature=0.0,
            max_tokens=_EXECUTIVE_VERDICT_MAX_TOKENS,
            require_tool_call_first=True,
            deadline=deadline,
        )
        critic_answer = critic_run.answer
        assert_conclusion_grounded_in_tool_results(
            str(critic_answer["conclusion"]), critic_run.tool_calls
        )
        if not bool(critic_answer["requires_human_review"]):
            raise AgenticCascadeError("catalog price outlier was not routed for human review")

        executive_run = await orchestrator.run(
            role="executive",
            system=(
                "You are the ShelfWise Executive agent. The POS Price Critic found a "
                "catalogue-price exception with real tool evidence. Route it to "
                "review_price_exception. Do not downgrade an out-of-band till price to an "
                "automatic sale."
            ),
            user=(
                f"SKU {sku} ({scenario.product_name}). Critic verdict: "
                f"passed={critic_answer['critic_passed']}, "
                f"conclusion={_bounded_conclusion(str(critic_answer['conclusion']))!r}. "
                "Route the guardrail action."
            ),
            final_schema=_SALES_EXECUTIVE_SCHEMA,
            final_schema_name="catalog_price_executive_verdict",
            correlation_id=critic_run.correlation_id,
            tenant_id=tenant_id,
            temperature=0.0,
            max_tokens=_EXECUTIVE_VERDICT_MAX_TOKENS,
            deadline=deadline,
        )
        executive_answer = executive_run.answer
        if executive_answer["recommended_action_type"] != "review_price_exception":
            raise AgenticCascadeError("catalog price outlier was downgraded by executive agent")
    except CascadeDeadlineExceeded as exc:
        raise AgenticCascadeDeadlineError(
            f"live agentic catalog-price cascade could not finish before the deadline: {exc}",
            completed_model_calls=exc.completed_model_calls,
            elapsed_ms=exc.elapsed_ms,
        ) from exc
    except (AgentOrchestrationError, ToolCallingError) as exc:
        raise AgenticCascadeError(f"live agentic catalog-price guardrail failed: {exc}") from exc

    return _build_catalog_price_check_result(
        event=event,
        scenario_sku=sku,
        product=scenario.product_name,
        observed=observed,
        catalog=catalog,
        units=units,
        critic_run=critic_run,
        critic_answer=critic_answer,
        executive_run=executive_run,
        executive_answer=executive_answer,
    )


def _build_catalog_price_check_result(
    *,
    event: Event,
    scenario_sku: str,
    product: str,
    observed: Decimal,
    catalog: Decimal,
    units: int,
    critic_run: AgentRunResult,
    critic_answer: dict[str, Any],
    executive_run: AgentRunResult,
    executive_answer: dict[str, Any],
) -> dict[str, Any]:
    price_delta_pct = ((observed - catalog) / catalog * Decimal("100")).quantize(
        Decimal("0.1")
    )
    action = RecommendedAction(
        "review_price_exception",
        {
            "sku": scenario_sku,
            "observed_unit_price": str(observed),
            "catalog_unit_price": str(catalog),
            "units": str(units),
            "price_delta_pct": f"{price_delta_pct}%",
        },
        RiskTier.MEDIUM,
    )
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
            recommended_action=action,
            sources=tool_sources,
            requires_human_review=True,
        ),
        EvidenceObject(
            agent=AgentName.EXECUTIVE,
            conclusion=str(executive_answer["conclusion"]),
            supporting_data=[
                {
                    "fact": "guardrail_route",
                    "value": executive_answer["recommended_action_type"],
                    "source": "agent:sales_critic",
                    "method": "agent_orchestrator",
                }
            ],
            confidence=Decimal(str(executive_answer["confidence"])),
            recommended_action=action,
            sources=(SourceRef.tool("agent:sales_critic"),),
            requires_human_review=True,
        ),
    ]
    decision = Decision(
        id=_decision_id(event),
        status=DecisionStatus.PENDING,
        action=action,
        caused_by=(event.id,),
        summary=(
            f"Live Gemma agentic price exception for SKU {scenario_sku} ({product}): "
            f"{executive_answer['conclusion']}"
        ),
    )
    decision_payload = decision.to_dict()
    decision_payload["tenant_id"] = _tenant_id(event)
    decision_payload["scenario_id"] = _PRICE_OUTLIER_SCENARIO_ID
    decision_payload["role"] = "sales_manager"
    decision_payload["critic_verdict"] = "review_required"
    price_exposure_minor_units = int((abs(observed - catalog) * units * 100).to_integral_value())
    decision_payload["expected_outcome"] = {
        "revenue_exposure_minor_units": price_exposure_minor_units,
        # `/mlops`'s decision-economics dashboard reads only `incremental_profit_minor_units`
        # - without it this displayed "R0.00 recovered" regardless of the learning-store fix.
        "incremental_profit_minor_units": price_exposure_minor_units,
    }

    return {
        "correlation_id": event.correlation_id,
        "scenario": _PRICE_OUTLIER_SCENARIO_ID,
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
        "token_budget": _token_budget(critic_run, executive_run),
    }


def run_expiry_risk_check_via_agents(
    event: Event,
    *,
    execution_mode: ExecutionMode = ExecutionMode.LIVE_REQUIRED,
    decisions: Any,
    memory: Any,
    facts: WorldFactsProvider | None = None,
    orchestrator_factory: Any = None,
    audit: AuditLog | None = None,
    model_run_recorder: RunRecorder | None = None,
    deadline: float | None = None,
) -> dict[str, Any] | None:
    """Run an imminent-expiry event through real Gemma tool calls."""
    return _scope_result(
        asyncio.run(
            _run_expiry_risk_check(
                event,
                execution_mode=execution_mode,
                decisions=decisions,
                memory=memory,
                facts=facts,
                orchestrator_factory=orchestrator_factory,
                audit=audit,
                model_run_recorder=model_run_recorder,
                deadline=deadline,
            )
        ),
        event,
    )


async def _run_expiry_risk_check(
    event: Event,
    *,
    execution_mode: ExecutionMode,
    decisions: Any,
    memory: Any,
    facts: WorldFactsProvider | None,
    orchestrator_factory: Any,
    audit: AuditLog | None = None,
    model_run_recorder: RunRecorder | None = None,
    deadline: float | None = None,
) -> dict[str, Any] | None:
    payload = event.payload
    try:
        days_to_expiry = int(payload["days_to_expiry"])
    except (KeyError, TypeError, ValueError):
        return None
    if days_to_expiry > EXPIRY_REVIEW_MAX_DAYS:
        return None

    resolved_facts = facts or _default_facts()
    tenant_id = _tenant_id(event)
    sku = str(payload.get("sku") or resolved_facts.get_hero_sku(tenant_id))
    batch_id = str(payload.get("batch_id") or f"BATCH-{sku}")
    scenario = resolved_facts.get_scenario_facts(tenant_id, sku)

    audit = audit or AuditLog()
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
        else _default_orchestrator(
            tools=tools,
            execution_mode=execution_mode,
            recorder=_scoped_recorder(model_run_recorder, event),
        )
    )

    try:
        critic_run = await orchestrator.run(
            role="critic",
            system=(
                "You are the ShelfWise Expiry Critic agent. You must call "
                "get_expiry_risk to gather the real expiry and waste-risk facts before "
                "deciding. Never invent numbers - the tool is your calculator. A batch "
                "inside the markdown review window must remain pending for inventory "
                "manager review. Cite the exact expiry, waste, risk, or ZAR-at-risk "
                "figures from the tool result."
            ),
            user=(
                f"Review batch {batch_id} for SKU {sku} ({scenario.product_name}) with "
                f"{days_to_expiry} day(s) to expiry. Call get_expiry_risk with sku={sku!r} "
                f"and days_to_expiry={days_to_expiry}, then return a verdict citing the "
                "exact tool numbers."
            ),
            final_schema=_EXPIRY_CRITIC_SCHEMA,
            final_schema_name="expiry_critic_verdict",
            correlation_id=event.correlation_id,
            tenant_id=tenant_id,
            temperature=0.0,
            max_tokens=_EXECUTIVE_VERDICT_MAX_TOKENS,
            require_tool_call_first=True,
            required_tool_names=("get_expiry_risk",),
            deadline=deadline,
        )
        critic_answer = critic_run.answer
        assert_conclusion_grounded_in_tool_results(
            str(critic_answer["conclusion"]), critic_run.tool_calls
        )
        if not bool(critic_answer["requires_human_review"]):
            raise AgenticCascadeError("expiry risk event was not routed for human review")

        executive_run = await orchestrator.run(
            role="executive",
            system=(
                "You are the ShelfWise Executive agent. The Expiry Critic found an "
                "imminent-expiry batch with real tool evidence. Route it to "
                "review_expiry_markdown. Do not downgrade an in-window expiry event to "
                "monitoring."
            ),
            user=(
                f"Batch {batch_id}, SKU {sku}. Critic verdict: "
                f"passed={critic_answer['critic_passed']}, "
                f"conclusion={_bounded_conclusion(str(critic_answer['conclusion']))!r}. "
                "Route the guardrail action."
            ),
            final_schema=_EXPIRY_EXECUTIVE_SCHEMA,
            final_schema_name="expiry_executive_verdict",
            correlation_id=critic_run.correlation_id,
            tenant_id=tenant_id,
            temperature=0.0,
            max_tokens=_EXECUTIVE_VERDICT_MAX_TOKENS,
            deadline=deadline,
        )
        executive_answer = executive_run.answer
        if executive_answer["recommended_action_type"] != "review_expiry_markdown":
            raise AgenticCascadeError("expiry risk event was downgraded by executive agent")
    except CascadeDeadlineExceeded as exc:
        raise AgenticCascadeDeadlineError(
            f"live agentic expiry cascade could not finish inside the response deadline: {exc}",
            completed_model_calls=exc.completed_model_calls,
            elapsed_ms=exc.elapsed_ms,
        ) from exc
    except (AgentOrchestrationError, ToolCallingError) as exc:
        raise AgenticCascadeError(f"live agentic expiry guardrail failed: {exc}") from exc

    return _build_expiry_risk_check_result(
        event=event,
        scenario_sku=sku,
        product=scenario.product_name,
        batch_id=batch_id,
        days_to_expiry=days_to_expiry,
        critic_run=critic_run,
        critic_answer=critic_answer,
        executive_run=executive_run,
        executive_answer=executive_answer,
    )


def _build_expiry_risk_check_result(
    *,
    event: Event,
    scenario_sku: str,
    product: str,
    batch_id: str,
    days_to_expiry: int,
    critic_run: AgentRunResult,
    critic_answer: dict[str, Any],
    executive_run: AgentRunResult,
    executive_answer: dict[str, Any],
) -> dict[str, Any]:
    action = RecommendedAction(
        "review_expiry_markdown",
        {"sku": scenario_sku, "batch_id": batch_id, "days_to_expiry": str(days_to_expiry)},
        RiskTier.MEDIUM,
    )
    tool_sources = tuple(
        SourceRef.tool(execution.name)
        for execution in (*critic_run.tool_calls, *executive_run.tool_calls)
    ) or (SourceRef.tool("gemma_agent_loop"),)
    evidence = [
        EvidenceObject(
            agent=AgentName.INVENTORY,
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
            recommended_action=action,
            sources=tool_sources,
            requires_human_review=True,
        ),
        EvidenceObject(
            agent=AgentName.EXECUTIVE,
            conclusion=str(executive_answer["conclusion"]),
            supporting_data=[
                {
                    "fact": "guardrail_route",
                    "value": executive_answer["recommended_action_type"],
                    "source": "agent:expiry_critic",
                    "method": "agent_orchestrator",
                }
            ],
            confidence=Decimal(str(executive_answer["confidence"])),
            recommended_action=action,
            sources=(SourceRef.tool("agent:expiry_critic"),),
            requires_human_review=True,
        ),
    ]
    decision = Decision(
        id=_decision_id(event),
        status=DecisionStatus.PENDING,
        action=action,
        caused_by=(event.id,),
        summary=(
            f"Live Gemma agentic expiry review for SKU {scenario_sku} ({product}): "
            f"{executive_answer['conclusion']}"
        ),
    )
    decision_payload = decision.to_dict()
    decision_payload["tenant_id"] = _tenant_id(event)
    decision_payload["scenario_id"] = _EXPIRY_SCENARIO_ID
    decision_payload["role"] = "inventory_manager"
    decision_payload["critic_verdict"] = "review_required"
    expected_outcome: dict[str, Any] = {
        "days_to_expiry": days_to_expiry,
        "batch_id": batch_id,
    }
    expiry_risk = _tool_result(critic_run.tool_calls, "get_expiry_risk")
    zar_at_risk = expiry_risk.get("zar_at_risk")
    if isinstance(zar_at_risk, dict) and "minor_units" in zar_at_risk:
        # `/mlops`'s decision-economics dashboard reads only `incremental_profit_minor_units`
        # - without it every agentic expiry review displayed "R0.00 recovered".
        expected_outcome["incremental_profit_minor_units"] = int(zar_at_risk["minor_units"])
    decision_payload["expected_outcome"] = expected_outcome

    return {
        "correlation_id": event.correlation_id,
        "scenario": _EXPIRY_SCENARIO_ID,
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
        "token_budget": _token_budget(critic_run, executive_run),
    }


def run_cold_chain_cascade_via_agents(
    event: Event | None = None,
    *,
    execution_mode: ExecutionMode = ExecutionMode.LIVE_REQUIRED,
    decisions: Any,
    memory: Any,
    facts: WorldFactsProvider | None = None,
    orchestrator_factory: Any = None,
    audit: AuditLog | None = None,
    model_run_recorder: RunRecorder | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Run the cold-chain facilities-escalation verdict through real Gemma tool calls."""
    return _scope_result(
        asyncio.run(
            _run_cold_chain(
                event,
                execution_mode=execution_mode,
                decisions=decisions,
                memory=memory,
                facts=facts,
                orchestrator_factory=orchestrator_factory,
                audit=audit,
                model_run_recorder=model_run_recorder,
                deadline=deadline,
            )
        ),
        event,
    )


async def _run_cold_chain(
    event: Event | None,
    *,
    execution_mode: ExecutionMode,
    decisions: Any,
    memory: Any,
    facts: WorldFactsProvider | None,
    orchestrator_factory: Any,
    audit: AuditLog | None = None,
    model_run_recorder: RunRecorder | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    resolved_facts = facts or _default_facts()
    payload = event.payload if event is not None else {}
    tenant_id = _tenant_id(event)
    scenario = resolved_facts.get_scenario_facts(tenant_id)
    asset_id = str(
        payload.get("asset_id") or f"cold-chain:{scenario.location}:{scenario.category}"
    )
    outage_hours = float(payload.get("measured_outage_hours") or 4.0)
    average_temp_c = float(payload.get("temp_c") or 8.2)
    correlation_id = event.correlation_id if event is not None else None

    audit = audit or AuditLog()
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
        else _default_orchestrator(
            tools=tools,
            execution_mode=execution_mode,
            recorder=_scoped_recorder(model_run_recorder, event),
        )
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
            tenant_id=_tenant_id(event),
            temperature=0.0,
            max_tokens=_EXECUTIVE_VERDICT_MAX_TOKENS,
            require_tool_call_first=True,
            required_tool_names=("get_cold_chain_status",),
            deadline=deadline,
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
                f"conclusion={_bounded_conclusion(str(critic_answer['conclusion']))!r}. "
                "Decide the routing action."
            ),
            final_schema=_COLD_CHAIN_EXECUTIVE_SCHEMA,
            final_schema_name="cold_chain_executive_verdict",
            correlation_id=critic_run.correlation_id,
            tenant_id=_tenant_id(event),
            temperature=0.0,
            max_tokens=_EXECUTIVE_VERDICT_MAX_TOKENS,
            deadline=deadline,
        )
        executive_answer = executive_run.answer
    except CascadeDeadlineExceeded as exc:
        raise AgenticCascadeDeadlineError(
            f"live agentic cold-chain cascade could not finish inside the response deadline: {exc}",
            completed_model_calls=exc.completed_model_calls,
            elapsed_ms=exc.elapsed_ms,
        ) from exc
    except (AgentOrchestrationError, ToolCallingError) as exc:
        raise AgenticCascadeError(f"live agentic cold-chain cascade failed: {exc}") from exc

    return _build_cold_chain_result(
        event=event,
        asset_id=asset_id,
        stock_at_risk_minor_units=(scenario.unit_price * scenario.units_on_hand).minor_units,
        critic_run=critic_run,
        critic_answer=critic_answer,
        executive_run=executive_run,
        executive_answer=executive_answer,
    )


def _build_cold_chain_result(
    *,
    event: Event | None,
    asset_id: str,
    stock_at_risk_minor_units: int,
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
    routed_action, gate_override = _enforce_critic_verdict(
        critic_passed=critic_passed,
        executive_action=dispatch if action_type == "dispatch_facilities_check" else monitor,
        safe_action=monitor,
    )

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
    decision_payload["critic_gate"] = _critic_gate_receipt(
        critic_passed=critic_passed,
        executive_action_type=str(action_type),
        override_applied=gate_override,
    )
    decision_payload["expected_outcome"] = {
        "stock_at_risk_minor_units": stock_at_risk_minor_units,
        # `/mlops`'s decision-economics dashboard reads only `incremental_profit_minor_units`
        # - without it every agentic cold-chain dispatch displayed "R0.00 recovered".
        "incremental_profit_minor_units": stock_at_risk_minor_units,
    }

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
        "token_budget": _token_budget(critic_run, executive_run),
    }
