from __future__ import annotations

from fastapi.testclient import TestClient

from shelfwise_backend import (
    run_cold_chain_cascade,
    run_critic_rejection_cascade,
    run_golden_cascade,
    run_procurement_cascade,
    run_sales_cascade,
)
from shelfwise_backend.app import app, model_run_registry, prompt_registry
from shelfwise_contracts import Event, EventSource, EventType
from shelfwise_inference import OpenAICompatibleInferenceClient
from shelfwise_mlops import ModelRun


def test_golden_cascade_returns_all_demo_agents() -> None:
    result = run_golden_cascade()
    agents = [item["agent"] for item in result["evidence"]]

    assert agents == [
        "inventory",
        "demand",
        "expiry",
        "opportunity",
        "simulation",
        "critic",
        "executive",
    ]
    assert result["decision"]["status"] == "pending"
    assert result["decision"]["action"]["type"] == "apply_markdown"
    assert result["decision"]["action"]["risk_tier"] == "high"


def test_golden_cascade_is_math_backed_and_traceable() -> None:
    result = run_golden_cascade()
    trace_names = {span["name"] for span in result["trace"]}
    support = [
        fact
        for evidence in result["evidence"]
        for fact in evidence["supporting_data"]
    ]

    assert "decision_science.forecast_demand" in trace_names
    assert "decision_science.score_expiry_risk" in trace_names
    assert "decision_science.simulate_markdown" in trace_names
    profit_fact = next(fact for fact in support if fact["fact"] == "incremental_profit")
    critic_fact = next(fact for fact in support if fact["fact"] == "critic_passed")
    assert profit_fact["value"] != "ZAR 0.00"
    assert critic_fact["value"] == "True"
    assert all(evidence["sources"] for evidence in result["evidence"])
    assert result["seed_data"]["recent_daily_units"] == ["28", "31", "29", "34", "30"]
    assert result["seed_data"]["product_name"] == "Amasi 2L"


def test_critic_rejection_cascade_downgrades_unsupported_action() -> None:
    result = run_critic_rejection_cascade()
    support = [
        fact
        for evidence in result["evidence"]
        for fact in evidence["supporting_data"]
    ]
    critic = next(evidence for evidence in result["evidence"] if evidence["agent"] == "critic")
    opportunity = next(
        evidence for evidence in result["evidence"] if evidence["agent"] == "opportunity"
    )

    assert result["scenario"] == "critic_rejects_unsupported_supplier_switch"
    assert opportunity["recommended_action"]["type"] == "supplier_switch"
    assert result["decision"]["status"] == "rejected"
    assert result["decision"]["action"]["type"] == "monitor"
    assert result["decision"]["critic_verdict"] == "rejected"
    assert result["decision"]["rejected_action"]["type"] == "supplier_switch"
    assert "Critic rejected" in critic["conclusion"]
    assert any(fact["fact"] == "critic_passed" and fact["value"] == "False" for fact in support)


def test_procurement_cascade_uses_reorder_policy_and_supplier_ranking() -> None:
    result = run_procurement_cascade()
    agents = [item["agent"] for item in result["evidence"]]
    support = [
        fact
        for evidence in result["evidence"]
        for fact in evidence["supporting_data"]
    ]

    assert result["scenario"] == "procurement_reorder_supplier_cover"
    assert "procurement" in agents
    assert result["decision"]["status"] == "pending"
    assert result["decision"]["role"] == "procurement_manager"
    assert result["decision"]["action"]["type"] == "reorder"
    assert result["decision"]["action"]["params"]["supplier_id"] == (
        result["supplier_ranking"]["ranked"][0]["supplier_id"]
    )
    assert result["reorder_policy"]["method"] == (
        "safety_stock_reorder_point_normal_lead_time_demand"
    )
    assert any(fact["fact"] == "suggested_order_units" for fact in support)
    assert any(fact["fact"] == "supplier_coverage" for fact in support)


def test_sales_cascade_records_clean_pos_sale() -> None:
    result = run_sales_cascade()
    support = [
        fact
        for evidence in result["evidence"]
        for fact in evidence["supporting_data"]
    ]

    assert result["scenario"] == "pos_sale_price_integrity"
    assert result["decision"]["status"] == "approved"
    assert result["decision"]["role"] == "sales_manager"
    assert result["decision"]["action"]["type"] == "record_sale"
    assert result["decision"]["expected_outcome"]["line_revenue_minor_units"] == 90000
    assert any(fact["fact"] == "price_delta" and fact["value"] == "0.00" for fact in support)


def test_sales_cascade_routes_price_exception_to_review() -> None:
    event = Event(
        id="evt_sale_exception",
        type=EventType.SALE,
        ts="2026-07-06T10:14:00Z",
        actor="store_12",
        source=EventSource.POS_CSV,
        tenant_id="sa_retail_demo",
        payload={"sku": "4011", "location": "store_12", "quantity": 2, "unit_price": "20.00"},
    )

    result = run_sales_cascade(event)

    assert result["correlation_id"] == "evt_sale_exception"
    assert result["decision"]["status"] == "pending"
    assert result["decision"]["action"]["type"] == "review_price_exception"
    assert result["decision"]["expected_outcome"]["price_delta"] == "-10.00"


def test_cold_chain_cascade_routes_facilities_review() -> None:
    event = Event(
        id="evt_cold_chain_fridge_dairy_1",
        type=EventType.COLD_CHAIN_ALERT,
        ts="2026-07-06T10:14:00Z",
        actor="store_12",
        source=EventSource.API,
        tenant_id="sa_retail_demo",
        payload={
            "site_id": "store_12",
            "asset_id": "fridge_dairy_1",
            "category": "dairy",
            "diagnosis": "generator_failed",
            "severity": 2,
            "predicted_minutes_to_unsafe": "18",
            "measured_outage_hours": "4",
            "temp_c": "8.2",
            "stock_at_risk": {"minor_units": 643500, "currency": "ZAR"},
        },
    )

    result = run_cold_chain_cascade(event)
    agents = [item["agent"] for item in result["evidence"]]

    assert agents == ["cold_chain", "expiry", "critic", "executive"]
    assert result["scenario"] == "cold_chain_generator_failure_facilities_review"
    assert result["decision"]["status"] == "pending"
    assert result["decision"]["role"] == "facilities_manager"
    assert result["decision"]["action"]["type"] == "dispatch_facilities_check"
    assert result["decision"]["action"]["params"]["asset_id"] == "fridge_dairy_1"
    assert result["decision"]["expected_outcome"]["stock_at_risk_minor_units"] == 643500
    assert result["decision"]["critic_verdict"] == "approved"


def test_inference_routing_keeps_strong_model_for_critic_and_executive() -> None:
    result = run_golden_cascade()
    routing = result["inference"]["routing"]

    assert "critic" in routing["strong_agents"]
    assert "executive" in routing["strong_agents"]
    assert "inventory" in routing["routine_agents"]


def test_hitl_approve_flow() -> None:
    client = TestClient(app)
    run_response = client.get("/demo/golden")
    assert run_response.status_code == 200

    decision = run_response.json()["decision"]
    assert decision["status"] == "pending"

    approve_response = client.post(f"/decisions/{decision['id']}/approve")
    assert approve_response.status_code == 200
    approved = approve_response.json()["decision"]
    learning_event = approve_response.json()["learning_event"]

    assert approved["status"] == "approved"
    assert approved["review"]["status"] == "approved"
    assert approved["write_back"]["status"] == "pending_external_write"
    assert approved["write_back"]["idempotency_key"] == f"writeback:{decision['id']}"
    assert approved["write_back"]["rollback_instructions"]["policy"] == (
        "recommend_only_no_source_mutation"
    )
    assert approved["outcome"]["units_cleared"] == 75
    assert approved["outcome"]["rand_recovered"]["amount"] == "109.56"
    assert approved["learning_event"]["updated_threshold"] >= 75
    assert learning_event["updated_threshold"] >= learning_event["previous_threshold"]
    assert "Threshold adjusted" in learning_event["message"]

    task_response = client.get("/writeback/tasks")
    assert task_response.status_code == 200
    tasks = task_response.json()["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["idempotency_key"] == approved["write_back"]["idempotency_key"]


def test_hitl_reject_flow() -> None:
    """The manual reject path had zero test coverage - only critic auto-rejection did."""
    client = TestClient(app)
    run_response = client.get("/demo/golden")
    decision_id = run_response.json()["decision"]["id"]

    reject_response = client.post(f"/decisions/{decision_id}/reject")

    assert reject_response.status_code == 200
    rejected = reject_response.json()["decision"]
    assert rejected["status"] == "rejected"
    assert rejected["review"]["status"] == "rejected"
    assert reject_response.json()["learning_event"] is None

    # Terminal-state guard: re-rejecting an already-rejected decision is idempotent, not an error.
    second = client.post(f"/decisions/{decision_id}/reject")
    assert second.status_code == 200
    assert second.json()["decision"]["status"] == "rejected"

    unknown = client.post("/decisions/dec_does_not_exist/reject")
    assert unknown.status_code == 404


def test_demo_decision_carries_governance_and_economics() -> None:
    client = TestClient(app)
    response = client.get("/demo/golden")

    assert response.status_code == 200
    decision = response.json()["decision"]
    economics = decision["economics"]
    governance = decision["governance"]

    assert economics["cost"]["minor_units"] >= 0
    assert economics["recovered"]["minor_units"] == decision["expected_outcome"][
        "incremental_profit_minor_units"
    ]
    assert economics["total_tokens"] > 0
    assert governance["provider"] == "offline"
    assert governance["evidence_count"] == len(response.json()["evidence"])


def test_demo_golden_exposes_store_intelligence_numbers() -> None:
    client = TestClient(app)
    response = client.get("/demo/golden")

    assert response.status_code == 200
    intelligence = response.json()["store_intelligence"]
    assert intelligence["batch_split"]["priority_sell_units"] == 10
    assert intelligence["batch_split"]["normal_units"] == 20
    assert intelligence["delivery_reconciliation"]["missing_units"] == 12
    assert intelligence["supplier_cover"]["recommended_action"] == "transfer"
    assert intelligence["learning_summary"]["sell_through_delta_units"] == 6


def test_demo_critic_rejection_endpoint_is_final_without_learning_event() -> None:
    client = TestClient(app)
    response = client.get("/demo/critic-rejection")

    assert response.status_code == 200
    decision = response.json()["decision"]
    assert decision["status"] == "rejected"
    assert decision["action"]["type"] == "monitor"

    approve_response = client.post(f"/decisions/{decision['id']}/approve")

    assert approve_response.status_code == 200
    body = approve_response.json()
    assert body["decision"]["status"] == "rejected"
    assert body["learning_event"] is None


def test_demo_procurement_endpoint_persists_pending_reorder() -> None:
    client = TestClient(app)
    response = client.get("/demo/procurement")

    assert response.status_code == 200
    body = response.json()
    decision = body["decision"]
    assert decision["action"]["type"] == "reorder"
    assert decision["status"] == "pending"
    assert decision["expected_outcome"]["stockout_exposure_minor_units"] > 0

    decisions_response = client.get("/decisions")
    assert decisions_response.status_code == 200
    assert any(item["id"] == decision["id"] for item in decisions_response.json()["decisions"])


def test_demo_sales_endpoint_persists_recorded_sale() -> None:
    client = TestClient(app)
    response = client.get("/demo/sales")

    assert response.status_code == 200
    decision = response.json()["decision"]
    assert decision["status"] == "approved"
    assert decision["action"]["type"] == "record_sale"
    assert decision["expected_outcome"]["line_revenue_minor_units"] == 90000


def test_demo_cold_chain_endpoint_persists_facilities_decision() -> None:
    client = TestClient(app)
    response = client.get("/demo/cold-chain")

    assert response.status_code == 200
    decision = response.json()["decision"]
    assert decision["status"] == "pending"
    assert decision["role"] == "facilities_manager"
    assert decision["action"]["type"] == "dispatch_facilities_check"
    assert decision["expected_outcome"]["stock_at_risk_minor_units"] > 0


def test_decisions_endpoint_lists_demo_decisions() -> None:
    client = TestClient(app)
    run_response = client.get("/demo/golden")
    assert run_response.status_code == 200
    decision = run_response.json()["decision"]

    response = client.get("/decisions")

    assert response.status_code == 200
    decisions = response.json()["decisions"]
    assert any(item["id"] == decision["id"] for item in decisions)
    listed = next(item for item in decisions if item["id"] == decision["id"])
    assert listed["status"] == "pending"
    assert listed["action"]["type"] == "apply_markdown"


def test_readiness_endpoint_reports_backend_ready() -> None:
    client = TestClient(app)
    response = client.get("/readiness")

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["checks"]["golden_cascade"] == "ok"
    assert body["checks"]["hitl"] == "ok"
    assert body["checks"]["learning"] == "ok"
    assert body["checks"]["critic_rejection"] == "ok"
    assert body["checks"]["seed_data"] == "ok"


def test_learning_endpoint_reports_threshold_events() -> None:
    client = TestClient(app)
    run_response = client.get("/demo/golden")
    decision = run_response.json()["decision"]
    approve_response = client.post(f"/decisions/{decision['id']}/approve")
    assert approve_response.status_code == 200

    response = client.get("/learning")

    assert response.status_code == 200
    body = response.json()
    assert body["thresholds"]["4011:markdown_sell_through_target_units"] >= 75
    assert any(event["decision_id"] == decision["id"] for event in body["events"])


def test_seed_summary_endpoint_returns_loaded_csv_context() -> None:
    client = TestClient(app)
    response = client.get("/data/seed/summary")

    assert response.status_code == 200
    seed = response.json()["seed_data"]
    assert seed["sku"] == "4011"
    assert seed["product_name"] == "Amasi 2L"
    assert seed["units_on_hand"] == 240


def test_inference_client_is_offline_safe() -> None:
    result = OpenAICompatibleInferenceClient().complete(
        agent="critic",
        system="system",
        user="user",
    )

    assert result.used_network is False
    assert result.provider == "offline"
    assert result.input_tokens > 0
    assert result.output_tokens > 0


def test_inference_smoke_records_model_run() -> None:
    client = TestClient(app)

    response = client.get("/inference/smoke")
    runs_response = client.get("/mlops/model-runs")
    prompts_response = client.get("/mlops/prompts")

    assert response.status_code == 200
    result = response.json()["result"]
    prompt = response.json()["prompt_version"]
    assert result["run_id"]
    assert result["usage"]["total_tokens"] > 0
    assert prompt["id"] == "smoke:v1"
    assert prompt["agent"] == "critic"
    assert prompt["sha"] == prompt_registry.get("smoke:v1").sha
    assert runs_response.status_code == 200
    runs = runs_response.json()["model_runs"]
    assert len(runs) == 1
    assert runs[0]["id"] == result["run_id"]
    assert runs[0]["agent"] == "critic"
    assert runs[0]["prompt_version"] == "smoke:v1"
    assert prompts_response.status_code == 200
    prompts = prompts_response.json()["prompt_versions"]
    assert len(prompts) == 1
    assert prompts[0]["id"] == runs[0]["prompt_version"]


def test_accountability_endpoint_joins_decisions_and_model_runs() -> None:
    client = TestClient(app)
    run_response = client.get("/demo/golden")
    decision = run_response.json()["decision"]
    approve_response = client.post(f"/decisions/{decision['id']}/approve")
    assert approve_response.status_code == 200

    OpenAICompatibleInferenceClient(
        recorder=lambda payload: model_run_registry.record(ModelRun(**payload))
    ).complete(
        agent="executive",
        system="system",
        user="user",
        tenant_id="sa_retail_demo",
        prompt_version="exec:v1",
    )
    response = client.get("/mlops/accountability?tenant_id=sa_retail_demo")

    assert response.status_code == 200
    report = response.json()["report"]
    assert report["decisions_total"] == 1
    assert report["approved_total"] == 1
    assert report["recovered"]["minor_units"] > 0
    assert report["models_used"] == ["offline-strong"]
    assert report["prompt_versions"] == ["exec:v1"]
