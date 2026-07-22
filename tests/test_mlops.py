from __future__ import annotations

import asyncio
import json
from decimal import Decimal

from fastapi.testclient import TestClient

from shelfwise_backend.app import app, decision_store, learning_store, tenant_fact_store
from shelfwise_contracts import Money
from shelfwise_mlops import (
    InMemoryModelRunRegistry,
    InMemoryPromptRegistry,
    InMemoryTenantFactStore,
    ModelRun,
    OutcomeRecord,
    PromptVersion,
    SkillStats,
    TokenUsage,
    activate,
    build_accountability_report,
    choose_model_route,
    consolidate_outcomes,
    decision_economics,
    draft_skills,
    estimate_cost,
    export_preference_jsonl,
    export_sft_jsonl,
    inference_cost,
    prompt_sha,
    release_gate,
    scorecard_release_gate,
    to_plan,
    tombstone_skill,
)


def test_model_run_registry_records_runs_per_tenant() -> None:
    registry = InMemoryModelRunRegistry()
    run = ModelRun(
        id="run_1",
        tenant_id="tenant_1",
        correlation_id="cor_1",
        agent="critic",
        model="strong",
        provider="offline",
        prompt_version="v1",
        schema_version="v1",
        input_tokens=100,
        output_tokens=50,
        latency_ms=12,
    )

    registry.record(run)

    assert registry.list(tenant_id="tenant_1") == [run]
    assert registry.list(tenant_id="other") == []
    assert registry.list(data_domain="world_simulation") == [run]
    assert registry.list(data_domain="operational_twin") == []
    assert registry.list()[0].to_dict()["created_at"]


def test_prompt_registry_records_hash_backed_versions_per_agent() -> None:
    registry = InMemoryPromptRegistry()
    system_prompt = "You are the ShelfWise critic. Reply briefly."

    prompt = registry.record_prompt(
        agent="critic",
        version="v1",
        system_prompt=system_prompt,
        tenant_id="tenant_1",
        prompt_id="smoke:v1",
    )
    duplicate = registry.record(
        PromptVersion(
            id="smoke:v1",
            tenant_id="tenant_1",
            agent="critic",
            version="v1",
            sha=prompt_sha(system_prompt),
            system_prompt=system_prompt,
        )
    )

    assert duplicate == prompt
    assert prompt.sha == prompt_sha(system_prompt)
    assert registry.get("smoke:v1", tenant_id="tenant_1") == prompt
    assert registry.list(agent="critic") == [prompt]
    assert registry.list(agent="executive") == []
    assert "system_prompt" not in prompt.to_dict()
    assert prompt.to_dict(include_prompt=True)["system_prompt"] == system_prompt


def test_prompt_registry_rejects_mutated_prompt_for_existing_version() -> None:
    registry = InMemoryPromptRegistry()
    registry.record_prompt(agent="critic", version="v1", system_prompt="original")

    try:
        registry.record_prompt(agent="critic", version="v1", system_prompt="changed")
    except ValueError as exc:
        assert "already has different content" in str(exc)
    else:
        raise AssertionError("mutated prompt content should not reuse a version id")


def test_prompt_registry_allows_same_prompt_id_per_tenant() -> None:
    registry = InMemoryPromptRegistry()

    tenant_1 = registry.record_prompt(
        agent="critic",
        version="v1",
        system_prompt="tenant one prompt",
        tenant_id="tenant_1",
        prompt_id="critic:v1",
    )
    tenant_2 = registry.record_prompt(
        agent="critic",
        version="v1",
        system_prompt="tenant two prompt",
        tenant_id="tenant_2",
        prompt_id="critic:v1",
    )

    assert registry.get("critic:v1", tenant_id="tenant_1") == tenant_1
    assert registry.get("critic:v1", tenant_id="tenant_2") == tenant_2
    assert registry.list(tenant_id="tenant_1") == [tenant_1]


def test_cost_estimate_uses_token_rate_card() -> None:
    estimate = estimate_cost(
        provider="vllm",
        model="qwen",
        usage=TokenUsage(input_tokens=1000, output_tokens=500),
        input_zar_per_1k=Decimal("0.10"),
        output_zar_per_1k=Decimal("0.20"),
    )

    assert estimate.zar.amount == Decimal("0.20")
    assert estimate.to_dict()["usage"]["total_tokens"] == 1500


def test_decision_economics_reports_recovered_per_inference_cost() -> None:
    assert inference_cost(10_000, rate_zar_per_1k=Decimal("0.004")).minor_units == 4

    economics = decision_economics(
        rand_recovered=Money.zar(378),
        total_tokens=10_000,
        rate_zar_per_1k=Decimal("0.004"),
    )

    assert economics["cost"]["minor_units"] == 4
    assert economics["recovered"]["minor_units"] == 37800
    assert economics["recovered_per_cost"] == "9450.0"


def test_decision_economics_reports_a_real_zero_ratio_not_none() -> None:
    """`Decimal("0")` is falsy in Python - a decision that cost real tokens and
    recovered nothing must report the meaningful ratio "0.0", not be conflated with
    the only genuinely undefined case (zero cost, division impossible).
    """
    zero_recovered = decision_economics(
        rand_recovered=Money.zar(0),
        total_tokens=10_000,
        rate_zar_per_1k=Decimal("0.004"),
    )
    zero_cost = decision_economics(
        rand_recovered=None,
        total_tokens=0,
        rate_zar_per_1k=Decimal("0.004"),
    )

    assert zero_recovered["cost"]["minor_units"] > 0
    assert zero_recovered["recovered_per_cost"] == "0.0", (
        "real cost with zero recovery must report the ratio, not hide it as null"
    )
    assert zero_cost["cost"]["minor_units"] == 0
    assert zero_cost["recovered_per_cost"] is None, (
        "zero cost is the only case where the ratio is genuinely undefined"
    )


def test_routing_keeps_critic_and_high_risk_actions_on_strong_model() -> None:
    critic = choose_model_route(agent="critic", routine_model="small", strong_model="strong")
    inventory = choose_model_route(
        agent="inventory",
        routine_model="small",
        strong_model="strong",
        risk_tier="high",
    )
    demand = choose_model_route(agent="demand", routine_model="small", strong_model="strong")

    assert critic.model == "strong"
    assert inventory.model == "strong"
    assert demand.model == "small"


def test_release_gate_blocks_missing_or_regressed_metrics() -> None:
    passed = release_gate(
        {"golden_pass_rate": Decimal("0.98")},
        {"golden_pass_rate": Decimal("0.99")},
    )
    failed = release_gate(
        {"golden_pass_rate": Decimal("0.90")},
        {"golden_pass_rate": Decimal("0.99"), "critic_rejection_rate": Decimal("1.0")},
    )

    assert passed["passed"] is True
    assert failed["passed"] is False
    assert "golden_pass_rate" in failed["regressions"]
    assert failed["regressions"]["critic_rejection_rate"] == "missing"


def test_scorecard_release_gate_blocks_category_regression() -> None:
    failed, reasons = asyncio.run(
        scorecard_release_gate(
            {
                "pass_rate": Decimal("0.97"),
                "by_category": {"expiry": Decimal("0.95"), "stockout": Decimal("0.85")},
            }
        )
    )
    passed, passed_reasons = asyncio.run(
        scorecard_release_gate(
            {
                "pass_rate": Decimal("0.98"),
                "by_category": {"expiry": Decimal("0.97"), "stockout": Decimal("0.96")},
            }
        )
    )

    assert failed is False
    assert any("stockout" in reason for reason in reasons)
    assert passed is True
    assert passed_reasons == []


def test_export_sft_and_preference_jsonl_are_labeled_synthetic(tmp_path) -> None:
    sft_path = tmp_path / "sft.jsonl"
    preference_path = tmp_path / "preferences.jsonl"

    sft_count = export_sft_jsonl(
        [{"input": [{"role": "user", "content": "x"}], "ideal": {"agent": "expiry"}}],
        sft_path,
    )
    preference_count = export_preference_jsonl(
        [
            {
                "messages": [{"role": "user", "content": "x"}],
                "chosen": {"agent": "critic", "verdict": "reject"},
                "rejected": {"agent": "critic", "verdict": "approve"},
            }
        ],
        preference_path,
    )

    sft_line = json.loads(sft_path.read_text(encoding="utf-8").splitlines()[0])
    preference_line = json.loads(preference_path.read_text(encoding="utf-8").splitlines()[0])
    assert sft_count == 1
    assert preference_count == 1
    assert sft_line["synthetic"] is True
    assert sft_line["output"]["agent"] == "expiry"
    assert preference_line["synthetic"] is True
    assert preference_line["chosen"]["verdict"] == "reject"


def test_memory_consolidation_only_promotes_repeated_successful_outcomes() -> None:
    facts = consolidate_outcomes(
        [
            OutcomeRecord("tenant_1", "4011", "markdown", Decimal("0.85"), ("dec_1",)),
            OutcomeRecord("tenant_1", "4011", "markdown", Decimal("0.80"), ("dec_2",)),
            OutcomeRecord("tenant_1", "4011", "transfer", Decimal("0.90"), ("dec_3",)),
            OutcomeRecord("tenant_1", "4011", "markdown", Decimal("0.30"), ("dec_4",)),
        ]
    )

    assert len(facts) == 1
    assert facts[0].action == "markdown"
    assert facts[0].support_count == 2
    assert facts[0].confidence == Decimal("0.82")
    assert facts[0].evidence_refs == ("dec_1", "dec_2")


def test_memory_consolidation_never_combines_live_and_simulation_outcomes() -> None:
    facts = consolidate_outcomes(
        [
            OutcomeRecord(
                "tenant_1",
                "4011",
                "markdown",
                Decimal("0.85"),
                ("dec_sim_1",),
                "world_simulation",
            ),
            OutcomeRecord(
                "tenant_1",
                "4011",
                "markdown",
                Decimal("0.80"),
                ("dec_sim_2",),
                "world_simulation",
            ),
            OutcomeRecord(
                "tenant_1",
                "4011",
                "markdown",
                Decimal("0.90"),
                ("dec_live_1",),
                "operational_twin",
            ),
            OutcomeRecord(
                "tenant_1",
                "4011",
                "markdown",
                Decimal("0.88"),
                ("dec_live_2",),
                "operational_twin",
            ),
        ]
    )
    store = InMemoryTenantFactStore()
    store.record_many(facts)

    assert {fact.data_domain for fact in facts} == {
        "world_simulation",
        "operational_twin",
    }
    assert len({fact.id for fact in facts}) == 2
    assert len(store.list(tenant_id="tenant_1", data_domain="world_simulation")) == 1
    assert len(store.list(tenant_id="tenant_1", data_domain="operational_twin")) == 1


def test_tenant_fact_store_filters_and_tombstones_governed_memory() -> None:
    store = InMemoryTenantFactStore()
    facts = consolidate_outcomes(
        [
            OutcomeRecord("tenant_1", "4011", "apply_markdown", Decimal("0.85"), ("dec_1",)),
            OutcomeRecord("tenant_1", "4011", "apply_markdown", Decimal("0.80"), ("dec_2",)),
            OutcomeRecord("tenant_2", "4011", "reorder", Decimal("0.90"), ("dec_3",)),
            OutcomeRecord("tenant_2", "4011", "reorder", Decimal("0.88"), ("dec_4",)),
        ]
    )

    persisted = store.record_many(facts)
    tombstoned = store.tombstone(
        persisted[0]["id"],
        tenant_id="tenant_1",
        reason="contradicted_by_later_outcome",
    )

    assert len(persisted) == 2
    assert store.list(tenant_id="tenant_1") == []
    assert store.list(tenant_id="tenant_2")[0]["action"] == "reorder"
    assert tombstoned is not None
    assert tombstoned["active"] is False
    assert tombstoned["tombstone_reason"] == "contradicted_by_later_outcome"
    assert store.list(tenant_id="tenant_1", active_only=False)[0]["active"] is False


def test_consolidate_memory_endpoint_persists_tenant_facts() -> None:
    client = TestClient(app)
    for decision in [
        _approved_memory_decision("dec_memory_1"),
        _approved_memory_decision("dec_memory_2"),
    ]:
        decision_store.upsert(decision)
        learning_store.record_approved_decision(decision)

    response = client.post("/mlops/consolidate-memory")
    replay = client.post("/mlops/consolidate-memory")
    listed = client.get("/mlops/tenant-facts")
    runs = client.get("/worker/runs")

    assert response.status_code == 200
    assert replay.status_code == 200
    body = response.json()
    replay_body = replay.json()
    assert body["tenant_id"] == "sa_retail_demo"
    assert body["status"] == "done"
    assert body["run_id"].startswith("memory_consolidation_sa_retail_demo_")
    assert replay_body["run_id"] == body["run_id"]
    assert replay_body["facts"] == body["facts"]
    assert body["records_considered"] == 2
    assert body["facts_written"] == 1
    assert body["facts"][0]["tenant_id"] == "sa_retail_demo"
    assert body["facts"][0]["sku"] == "4011"
    assert body["facts"][0]["action"] == "apply_markdown"
    assert body["facts"][0]["support_count"] == 2
    assert "dec_memory_1" in body["facts"][0]["evidence_refs"]
    assert listed.status_code == 200
    assert listed.json()["facts"] == tenant_fact_store.list(tenant_id="sa_retail_demo")
    assert runs.status_code == 200
    assert any(run["run_id"] == body["run_id"] for run in runs.json()["runs"])


def test_accountability_report_filters_by_tenant_and_sums_recovered_money() -> None:
    report = build_accountability_report(
        tenant_id="tenant_1",
        decisions=[
            {
                "tenant_id": "tenant_1",
                "status": "approved",
                "outcome": {"rand_recovered": {"minor_units": 1234}},
            },
            {"tenant_id": "tenant_1", "status": "rejected"},
            {
                "tenant_id": "other",
                "status": "approved",
                "outcome": {"rand_recovered": {"minor_units": 9999}},
            },
        ],
        models_used=["strong", "small", "strong"],
        prompt_versions=["v1", "v1"],
    )

    assert report.decisions_total == 2
    assert report.approved_total == 1
    assert report.rejected_total == 1
    assert report.recovered.amount == Decimal("12.34")
    assert "Accountability Report - tenant_1" in report.to_markdown()


def test_observability_snapshot_separates_live_and_simulated_metrics() -> None:
    client = TestClient(app)
    run_response = client.post("/scenarios/golden")
    decision = run_response.json()["decision"]
    approve_response = client.post(f"/decisions/{decision['id']}/approve")
    inference_response = client.get("/inference/smoke")
    connector_response = client.post(
        "/connectors/shopify/intake",
        json={"payload": {"id": 777, "created_at": "2026-07-06T10:00:00Z"}},
    )
    ingest_response = client.post(
        "/ingest",
        json={
            "id": "evt_observability_scan",
            "type": "scan",
            "ts": "2026-07-06T10:14:00Z",
            "actor": "store_12",
            "source": "scanner",
            "tenant_id": "sa_retail_demo",
            "payload": {"sku": "4011", "location": "store_12"},
        },
    )
    worker_response = client.post("/worker/process-one")

    simulated_response = client.get(
        "/mlops/observability?data_domain=world_simulation"
    )
    operational_response = client.get(
        "/mlops/observability?data_domain=operational_twin"
    )

    assert approve_response.status_code == 200
    assert inference_response.status_code == 200
    assert connector_response.status_code == 200
    assert ingest_response.status_code == 200
    assert worker_response.status_code == 200
    assert simulated_response.status_code == 200
    assert operational_response.status_code == 200
    simulated = simulated_response.json()["snapshot"]
    operational = operational_response.json()["snapshot"]
    assert simulated["tenant_id"] == "sa_retail_demo"
    assert simulated["data_domain"] == "world_simulation"
    assert simulated["decisions"]["total"] == 1
    assert simulated["decisions"]["approved"] == 1
    assert simulated["decisions"]["recovered"]["minor_units"] > 0
    assert simulated["inference"]["model_runs"] == 1
    assert simulated["inference"]["total_tokens"] > 0
    assert simulated["inference"]["estimated_cost"]["currency"] == "ZAR"
    assert simulated["connectors"]["inbound_records"] == 0
    assert simulated["writeback"]["pending_external_write"] == 1
    assert simulated["learning"]["learning_events"] == 1
    assert operational["data_domain"] == "operational_twin"
    assert operational["decisions"]["total"] == 0
    assert operational["inference"]["model_runs"] == 0
    assert operational["connectors"]["inbound_records"] == 1
    assert operational["connectors"]["invalid_records"] == 1
    assert operational["connectors"]["by_system"] == {"shopify": 1}
    assert operational["events"]["bus"]["messages_total"] >= 1
    assert operational["worker"]["done_runs"] == 1
    assert operational["candidates"]["total"] >= 0
    assert operational["open_orders"]["total"] >= 0


def test_skills_are_earned_governed_and_compile_to_plans() -> None:
    stats = SkillStats()
    for index, score in enumerate(
        [Decimal("0.80"), Decimal("0.75"), Decimal("0.95"), Decimal("0.20")]
    ):
        stats.reflect(
            OutcomeRecord(
                "tenant_1",
                "4011",
                "apply_markdown",
                score,
                (f"dec_{index}",),
            ),
            trigger="expiry_risk:dairy",
        )
    stats.reflect(
        OutcomeRecord("tenant_1", "4011", "reorder", Decimal("0.90"), ("dec_x",)),
        trigger="expiry_risk:dairy",
    )
    template = {
        "apply_markdown": [
            {
                "key": "markdown",
                "capability": "write_price",
                "params": {"discount_pct": 30},
                "compensation": {"undo": "restore_price"},
            }
        ]
    }

    drafts = draft_skills(stats, tenant_id="tenant_1", step_template=template)

    assert [skill.name for skill in drafts] == ["apply_markdown when expiry_risk:dairy"]
    assert drafts[0].status == "draft"
    assert drafts[0].support == 4
    assert drafts[0].success_rate == Decimal("0.75")
    assert drafts[0].derived_from[0] == "dec_0"

    try:
        to_plan(drafts[0], plan_id="plan_1", actor_role="manager")
    except ValueError as exc:
        assert "only active skills run" in str(exc)
    else:
        raise AssertionError("draft skill should not compile to a runnable plan")

    active = activate(drafts[0])
    plan = to_plan(active, plan_id="plan_1", actor_role="manager")
    tombstoned = tombstone_skill(active, reason="contradicted by later outcomes")

    assert plan["tenant_id"] == "tenant_1"
    assert plan["steps"][0]["compensation"] == {"undo": "restore_price"}
    assert tombstoned.status.startswith("tombstoned:")


def _approved_memory_decision(decision_id: str) -> dict[str, object]:
    return {
        "id": decision_id,
        "tenant_id": "sa_retail_demo",
        "status": "approved",
        "role": "manager",
        "action": {
            "type": "apply_markdown",
            "params": {"sku": "4011", "discount_pct": 0.3},
        },
        "caused_by": [f"evt_{decision_id}"],
        "expected_outcome": {
            "predicted_sell_through_units": 50,
            "predicted_waste_units": 5,
            "markdown_margin_minor_units": 100,
            "incremental_profit_minor_units": 500,
        },
    }
