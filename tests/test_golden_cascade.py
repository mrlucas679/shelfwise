from __future__ import annotations

from fastapi.testclient import TestClient

from shelfwise_backend import run_golden_cascade
from shelfwise_backend.app import app
from shelfwise_inference import OpenAICompatibleInferenceClient


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

    assert approved["status"] == "approved"
    assert approved["review"]["status"] == "approved"


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


def test_readiness_endpoint_reports_backend_ready() -> None:
    client = TestClient(app)
    response = client.get("/readiness")

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["checks"]["golden_cascade"] == "ok"
    assert body["checks"]["hitl"] == "ok"
    assert body["checks"]["seed_data"] == "ok"


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
