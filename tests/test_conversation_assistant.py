"""Tests for the conversational-assistant subsystem: hierarchical memory, deterministic
tier routing, token-accounted context receipts, and progressive skill discovery.

This is the layer the plan flagged as the biggest gap between the blueprint and the
running app: chat previously carried only the last few raw messages, silently losing a
long conversation's earlier context, with no skill catalogue and no auditable routing.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from shelfwise_backend.app import app
from shelfwise_backend.context_budget import (
    ContextAllocation,
    build_context_receipt,
    estimate_tokens,
)
from shelfwise_backend.conversation_memory import (
    ConversationMemoryItem,
    InMemoryConversationMemoryStore,
    MemoryKind,
    compact_conversation,
)
from shelfwise_backend.conversation_routing import (
    ConversationRouteRequest,
    ConversationTier,
    RouteReason,
    choose_conversation_route,
)
from shelfwise_backend.tenant import encode_hs256_token
from shelfwise_mlops.skill_registry import (
    InMemorySkillRegistry,
    SkillManifest,
    default_skill_manifests,
    discover,
    promote,
    retire,
)

_KNOWN_AGENTS = {
    "inventory",
    "sales",
    "cold_chain",
    "expiry",
    "demand",
    "procurement",
    "opportunity",
    "simulation",
    "critic",
    "executive",
}
_KNOWN_TOOLS = {"get_stock", "get_demand_forecast", "simulate_markdown"}


def _manifest(**overrides) -> SkillManifest:
    base = dict(
        id="stock_lookup_test",
        version="1.0.0",
        name="Stock lookup",
        description="Answer stock questions.",
        domain_owner="inventory",
        allowed_roles=("manager",),
        trigger_terms=("stock", "on hand"),
        required_entity_types=("sku",),
        required_tools=("get_stock",),
        risk_tier="low",
        read_only=True,
        max_context_tokens=800,
        critic_required=False,
        hitl_required=False,
        source_refs=("capabilities/manifest.json",),
        evaluation_ids=("tests/test_model_tool_calling.py",),
        minimum_pass_rate=0.9,
        tenant_id=None,
        status="promoted",
    )
    base.update(overrides)
    return SkillManifest(**base)


# --- deterministic routing -----------------------------------------------------------


@pytest.mark.parametrize(
    ("request_kwargs", "tier", "reason"),
    [
        (
            {"domains": ("inventory",), "risk_tier": "high", "asks_for_scenario": False,
             "has_source_conflict": False, "has_memory_conflict": False},
            ConversationTier.STRONG,
            RouteReason.STRONG_HIGH_RISK,
        ),
        (
            {"domains": ("inventory",), "risk_tier": "low", "asks_for_scenario": False,
             "has_source_conflict": True, "has_memory_conflict": False},
            ConversationTier.STRONG,
            RouteReason.STRONG_SOURCE_CONFLICT,
        ),
        (
            {"domains": ("inventory",), "risk_tier": "low", "asks_for_scenario": True,
             "has_source_conflict": False, "has_memory_conflict": False},
            ConversationTier.STRONG,
            RouteReason.STRONG_SCENARIO_REASONING,
        ),
        (
            {"domains": ("inventory", "procurement"), "risk_tier": "low",
             "asks_for_scenario": False, "has_source_conflict": False,
             "has_memory_conflict": False},
            ConversationTier.STRONG,
            RouteReason.STRONG_MULTI_DOMAIN,
        ),
        (
            {"domains": ("inventory",), "risk_tier": "low", "asks_for_scenario": False,
             "has_source_conflict": False, "has_memory_conflict": False,
             "is_simple_followup": True},
            ConversationTier.ROUTINE,
            RouteReason.ROUTINE_SIMPLE_FOLLOWUP,
        ),
        (
            {"domains": ("inventory",), "risk_tier": "low", "asks_for_scenario": False,
             "has_source_conflict": False, "has_memory_conflict": False},
            ConversationTier.ROUTINE,
            RouteReason.ROUTINE_SINGLE_DOMAIN_LOOKUP,
        ),
    ],
)
def test_routing_is_deterministic_and_auditable(request_kwargs, tier, reason) -> None:
    route = choose_conversation_route(ConversationRouteRequest(**request_kwargs))
    assert route.tier is tier
    assert route.reason is reason
    assert route.to_dict()["policy_version"] == "conversation-route-v1"


# --- hierarchical conversation memory ------------------------------------------------


def _messages(count: int) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = [
        {"id": "m0", "role": "user", "text": "Track the yoghurt stock position for store 12."}
    ]
    for index in range(1, count):
        role = "assistant" if index % 2 else "user"
        rows.append({"id": f"m{index}", "role": role, "text": f"turn {index} content"})
    return rows


def test_compaction_preserves_objective_and_survives_the_sliding_window() -> None:
    """THE core gap: context older than the recent window must remain available."""
    store = InMemoryConversationMemoryStore()
    messages = _messages(20)

    summary = compact_conversation(
        store,
        tenant_id="t1",
        user_id="u1",
        conversation_id="c1",
        messages=messages,
        recent_window=4,
    )

    assert summary is not None
    assert "yoghurt stock position for store 12" in summary.text, (
        "the conversation objective from turn 0 must survive compaction even though it "
        "fell out of the recent window long ago"
    )
    assert summary.status == "active"
    assert set(summary.source_message_ids) == {f"m{i}" for i in range(16)}, (
        "provenance must cover exactly the compacted (non-recent) messages"
    )
    active = store.active_summary(tenant_id="t1", user_id="u1", conversation_id="c1")
    assert active is not None and active.id == summary.id


def test_compaction_preserves_corrections_verbatim() -> None:
    store = InMemoryConversationMemoryStore()
    messages = _messages(10)
    messages[3] = {
        "id": "m3",
        "role": "user",
        "text": "No, actually the reorder point is 40 units, not 20.",
    }

    summary = compact_conversation(
        store, tenant_id="t1", user_id="u1", conversation_id="c1",
        messages=messages, recent_window=2,
    )

    assert summary is not None
    assert "Correction (verbatim): No, actually the reorder point is 40 units" in summary.text


def test_compaction_is_idempotent_and_a_longer_prefix_supersedes() -> None:
    store = InMemoryConversationMemoryStore()
    messages = _messages(12)

    first = compact_conversation(
        store, tenant_id="t1", user_id="u1", conversation_id="c1",
        messages=messages, recent_window=4,
    )
    repeat = compact_conversation(
        store, tenant_id="t1", user_id="u1", conversation_id="c1",
        messages=messages, recent_window=4,
    )
    assert repeat is not None and first is not None and repeat.id == first.id

    grown = compact_conversation(
        store, tenant_id="t1", user_id="u1", conversation_id="c1",
        messages=_messages(18), recent_window=4,
    )
    assert grown is not None and grown.id != first.id
    assert grown.supersedes_id == first.id
    active = store.active_summary(tenant_id="t1", user_id="u1", conversation_id="c1")
    assert active is not None and active.id == grown.id
    all_active = store.list_active(tenant_id="t1", user_id="u1", conversation_id="c1")
    assert [item.id for item in all_active] == [grown.id], (
        "the superseded summary must no longer be active"
    )


def test_memory_items_require_provenance_and_confirmed_commitments() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="source message IDs"):
        ConversationMemoryItem(
            id="mem1", tenant_id="t1", user_id="u1", conversation_id="c1",
            kind=MemoryKind.OBJECTIVE, text="x", source_message_ids=(),
            entity_ids=(), valid_from=now, valid_to=None, supersedes_id=None,
            status="active", confidence=1.0, summary_version="v1",
            created_by_model_run_id=None,
        ).assert_activatable()
    with pytest.raises(ValueError, match="explicit confirmation"):
        ConversationMemoryItem(
            id="mem2", tenant_id="t1", user_id="u1", conversation_id="c1",
            kind=MemoryKind.COMMITMENT, text="will reorder", source_message_ids=("m1",),
            entity_ids=(), valid_from=now, valid_to=None, supersedes_id=None,
            status="active", confidence=0.8, summary_version="v1",
            created_by_model_run_id=None,
        ).assert_activatable()


# --- context budget -------------------------------------------------------------------


def test_context_allocation_accounts_for_the_whole_model_window() -> None:
    ContextAllocation().validate()
    with pytest.raises(ValueError, match="!= model window"):
        ContextAllocation().validate(model_window=4_096)


def test_context_receipt_fails_closed_on_overflow_before_network_io() -> None:
    with pytest.raises(ValueError, match="context overflow"):
        build_context_receipt(sections={"question": "x" * 40_000})

    receipt = build_context_receipt(
        sections={"question": "How much stock is on hand?"},
        selected_skill_ids=("stock_lookup_test",),
    )
    assert receipt.section_tokens["question"] == estimate_tokens("How much stock is on hand?")
    assert receipt.to_dict()["selected_skill_ids"] == ["stock_lookup_test"]


# --- skill registry and lifecycle -----------------------------------------------------


def test_skill_validation_rejects_orphaned_and_permission_expanding_manifests() -> None:
    with pytest.raises(ValueError, match="unknown tool"):
        _manifest(required_tools=("drop_all_tables",)).validate(
            known_agents=_KNOWN_AGENTS, known_tools=_KNOWN_TOOLS
        )
    with pytest.raises(ValueError, match="not an existing agent"):
        _manifest(domain_owner="rogue_agent").validate(
            known_agents=_KNOWN_AGENTS, known_tools=_KNOWN_TOOLS
        )
    with pytest.raises(ValueError, match="require HITL"):
        _manifest(read_only=False, hitl_required=False).validate(
            known_agents=_KNOWN_AGENTS, known_tools=_KNOWN_TOOLS
        )
    with pytest.raises(ValueError, match="sources and evaluations"):
        _manifest(evaluation_ids=()).validate(
            known_agents=_KNOWN_AGENTS, known_tools=_KNOWN_TOOLS
        )


def test_discovery_surfaces_only_promoted_role_matched_skills_ranked_by_relevance() -> None:
    registry = InMemorySkillRegistry()
    registry.upsert(
        _manifest(id="draft_skill", status="draft"),
        known_agents=_KNOWN_AGENTS, known_tools=_KNOWN_TOOLS,
    )
    registry.upsert(
        _manifest(id="stock_skill", trigger_terms=("stock", "on hand", "units")),
        known_agents=_KNOWN_AGENTS, known_tools=_KNOWN_TOOLS,
    )
    registry.upsert(
        _manifest(
            id="forecast_skill",
            domain_owner="demand",
            trigger_terms=("stock",),
            required_tools=("get_demand_forecast",),
        ),
        known_agents=_KNOWN_AGENTS, known_tools=_KNOWN_TOOLS,
    )
    registry.upsert(
        _manifest(id="owner_only", allowed_roles=("owner",), trigger_terms=("stock",)),
        known_agents=_KNOWN_AGENTS, known_tools=_KNOWN_TOOLS,
    )

    found = discover(
        registry, question="How many units of stock are on hand?", role="manager"
    )

    ids = [manifest.id for manifest in found]
    assert "draft_skill" not in ids, "drafts must never reach a conversation"
    assert "owner_only" not in ids, "role filtering happens before the prompt"
    assert ids[0] == "stock_skill", "most trigger hits must rank first"
    assert "forecast_skill" in ids


def test_promotion_gate_requires_the_skills_own_evaluation_bar() -> None:
    registry = InMemorySkillRegistry()
    registry.upsert(
        _manifest(id="candidate_skill", status="draft", minimum_pass_rate=0.9),
        known_agents=_KNOWN_AGENTS, known_tools=_KNOWN_TOOLS,
    )

    with pytest.raises(ValueError, match="below the skill's required"):
        promote(registry, "candidate_skill", measured_pass_rate=0.8)
    assert registry.get("candidate_skill").status == "draft"

    promoted = promote(registry, "candidate_skill", measured_pass_rate=0.95)
    assert promoted.status == "promoted"

    retired = retire(registry, "candidate_skill")
    assert retired.status == "retired"
    with pytest.raises(ValueError, match="retired skill cannot be promoted"):
        promote(registry, "candidate_skill", measured_pass_rate=1.0)


def test_default_platform_catalogue_validates_against_the_real_surface() -> None:
    manifests = default_skill_manifests()
    assert len(manifests) >= 8
    assert all(manifest.read_only for manifest in manifests), (
        "the built-in catalogue is read-only by contract; write skills need HITL wiring"
    )
    assert all(manifest.status == "promoted" for manifest in manifests)


# --- end-to-end through the real chat route -------------------------------------------


def _jwt_headers(monkeypatch) -> dict[str, str]:
    monkeypatch.setenv("SHELFWISE_AUTH_MODE", "jwt")
    monkeypatch.setenv("TENANT_AUTH_SECRET", "secret")
    token = encode_hs256_token(
        {
            "tenant_id": "assistant_test_tenant",
            "user_id": "assistant_user",
            "role": "manager",
            "exp": int(time.time()) + 3600,
        },
        secret="secret",
    )
    return {"Authorization": f"Bearer {token}"}


def test_chat_carries_memory_route_and_context_receipts_end_to_end(monkeypatch) -> None:
    """Drive the real /chat route far past the recent window and prove the conversation
    does NOT lose its earlier context: the rolling summary exists, is attached to the
    answer metadata, and every answer carries an auditable route + context receipt."""
    from shelfwise_backend.state import conversation_memory_store

    headers = _jwt_headers(monkeypatch)
    client = TestClient(app)
    conversation_id = "conv_memory_proof"

    for index in range(8):
        response = client.post(
            "/chat",
            headers=headers,
            json={
                "question": (
                    "Track the yoghurt stock position for store 12."
                    if index == 0
                    else f"Follow-up number {index}: what is the stock now?"
                ),
                "conversation_id": conversation_id,
                "message_id": f"msg_{index}",
            },
        )
        assert response.status_code == 200

    conversation = client.get(
        f"/chat/conversations/{conversation_id}", headers=headers
    ).json()["conversation"]
    last_answer = conversation["messages"][-1]
    meta = last_answer["metadata"]

    assert meta["conversation_route"]["policy_version"] == "conversation-route-v1"
    assert meta["conversation_route"]["tier"] in {"routine", "strong"}
    assert meta["context_receipt"]["model_window"] == 8_192
    assert meta["context_receipt"]["estimated_input_tokens"] >= 1
    assert meta.get("skills"), "a stock question must discover the stock skill"

    summary = conversation_memory_store.active_summary(
        tenant_id="assistant_test_tenant",
        user_id="assistant_user",
        conversation_id=conversation_id,
    )
    assert summary is not None, (
        "a conversation longer than the recent window must have a rolling summary - "
        "this is the exact silent-context-loss gap the memory layer closes"
    )
    assert "yoghurt stock position for store 12" in summary.text, (
        "the objective from message 0 must remain durable after it left the window"
    )
    assert meta["conversation_summary_id"] == summary.id
