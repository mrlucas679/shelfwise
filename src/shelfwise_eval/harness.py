from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from shelfwise_backend.app import app

EXPECTED_AGENTS = [
    "inventory",
    "demand",
    "expiry",
    "opportunity",
    "simulation",
    "critic",
    "executive",
]


def run_eval() -> dict[str, Any]:
    client = TestClient(app)
    checks: list[dict[str, Any]] = []

    def check(name: str, condition: bool, detail: str) -> None:
        checks.append(
            {
                "name": name,
                "passed": condition,
                "detail": detail,
            }
        )

    readiness = client.get("/readiness")
    check("readiness", readiness.status_code == 200, f"status_code={readiness.status_code}")

    golden = client.get("/demo/golden")
    golden_body = golden.json()
    check("golden_http", golden.status_code == 200, f"status_code={golden.status_code}")
    agents = [item["agent"] for item in golden_body["evidence"]]
    check(
        "golden_agents",
        agents == EXPECTED_AGENTS,
        f"agents={agents}",
    )
    decision = golden_body["decision"]
    check(
        "golden_pending_hitl",
        decision["status"] == "pending" and decision["action"]["risk_tier"] == "high",
        f"status={decision['status']} action={decision['action']['type']}",
    )
    check(
        "grounded_evidence",
        len(golden_body["evidence"]) >= 7,
        f"evidence_count={len(golden_body['evidence'])}",
    )
    token_total = sum(
        int(item["supporting_data"][0].get("tokens", 0))
        for item in golden_body["evidence"]
        if item.get("supporting_data")
    )
    check("token_ceiling", token_total <= 24_000, f"total_tokens={token_total} ceiling=24000")
    spans = [item["name"] for item in golden_body["trace"]]
    check(
        "trace_chain",
        "decision_science.forecast_demand" in spans
        and "decision_science.simulate_markdown" in spans,
        f"spans={spans}",
    )

    approved = client.post(f"/decisions/{decision['id']}/approve")
    approved_body = approved.json()
    check("hitl_approve_http", approved.status_code == 200, f"status_code={approved.status_code}")
    approved_decision = approved_body["decision"]
    check(
        "pending_to_approved",
        approved_decision["status"] == "approved"
        and approved_decision["write_back"]["status"] == "mocked_success",
        (
            f"status={approved_decision['status']} "
            f"write_back={approved_decision['write_back']['status']}"
        ),
    )
    learning_event = approved_body["learning_event"]
    check(
        "learning_moment",
        learning_event["updated_threshold"] >= learning_event["previous_threshold"],
        f"threshold={learning_event['previous_threshold']}->{learning_event['updated_threshold']}",
    )

    rejection = client.get("/demo/critic-rejection")
    rejection_body = rejection.json()
    rejection_decision = rejection_body["decision"]
    check(
        "critic_rejection_http",
        rejection.status_code == 200,
        f"status_code={rejection.status_code}",
    )
    check(
        "critic_rejection_downgrades",
        rejection_decision["status"] == "rejected"
        and rejection_decision["action"]["type"] == "monitor"
        and rejection_decision["critic_verdict"] == "rejected",
        (
            f"status={rejection_decision['status']} "
            f"verdict={rejection_decision.get('critic_verdict')} "
            f"action={rejection_decision['action']['type']}"
        ),
    )

    attention = client.get("/products/attention")
    attention_body = attention.json()
    check(
        "product_attention_http",
        attention.status_code == 200,
        f"status_code={attention.status_code}",
    )
    check(
        "product_attention_bounded",
        len(attention_body["items"]) <= attention_body["limit"]
        and len(attention_body["sell_first"]) >= 1,
        (
            f"items={len(attention_body['items'])} "
            f"sell_first_products={len(attention_body['sell_first'])}"
        ),
    )

    product_search = client.get("/products/search", params={"q": "amasi", "limit": 3})
    product_body = product_search.json()
    first = product_body["products"][0]
    check(
        "product_search_http",
        product_search.status_code == 200,
        f"status_code={product_search.status_code}",
    )
    check(
        "product_search_attention_ranked",
        first["name"] == "Amasi 2L"
        and first["sell_first_units"] == 10
        and first["lot_count"] == 2
        and first["fefo_batches"][0]["lot"] == "AMASI-OLD-0707",
        (
            f"first={first['name']} "
            f"sell_first_units={first['sell_first_units']} "
            f"lot_count={first['lot_count']} "
            f"first_lot={first['fefo_batches'][0]['lot']}"
        ),
    )
    source_counts = product_body["source_counts"]
    check(
        "product_search_scan_bounded",
        source_counts["synthetic_scanned"] <= source_counts["synthetic_scan_budget"],
        (
            f"scanned={source_counts['synthetic_scanned']} "
            f"budget={source_counts['synthetic_scan_budget']}"
        ),
    )

    fefo = client.post(
        "/intelligence/stock/fefo-split",
        json={
            "sku": "milk_2l",
            "as_of": "2026-07-06",
            "batches": [
                {
                    "sku": "milk_2l",
                    "lot": "MILK-OLD-0707",
                    "units": 10,
                    "expiry_date": "2026-07-07",
                    "received_date": "2026-07-03",
                    "location": "fridge_a",
                },
                {
                    "sku": "milk_2l",
                    "lot": "MILK-NEW-0713",
                    "units": 20,
                    "expiry_date": "2026-07-13",
                    "received_date": "2026-07-06",
                    "location": "fridge_a",
                },
            ],
        },
    )
    fefo_body = fefo.json()["batch_split"]
    check("fefo_split_http", fefo.status_code == 200, f"status_code={fefo.status_code}")
    check(
        "fefo_split_batch_math",
        fefo_body["priority_sell_units"] == 10 and fefo_body["normal_units"] == 20,
        f"priority={fefo_body['priority_sell_units']} normal={fefo_body['normal_units']}",
    )

    inference_readiness = client.get("/inference/readiness")
    inference_body = inference_readiness.json()
    check(
        "inference_readiness_http",
        inference_readiness.status_code == 200,
        f"status_code={inference_readiness.status_code}",
    )
    check(
        "inference_timeout_submission_safe",
        inference_body["inference"]["timeout_seconds"] < 30,
        f"timeout={inference_body['inference']['timeout_seconds']}",
    )

    submission = client.get("/submission/readiness")
    submission_body = submission.json()
    check(
        "submission_readiness_http",
        submission.status_code == 200,
        f"status_code={submission.status_code}",
    )
    check(
        "submission_track_three",
        submission_body["track"] == "Track 3: Unicorn"
        and submission_body["checks"]["docker_image_required"] == "no",
        (
            f"track={submission_body['track']} "
            f"docker={submission_body['checks']['docker_image_required']}"
        ),
    )

    passed = sum(1 for item in checks if item["passed"])
    failed = len(checks) - passed
    return {"passed": passed, "failed": failed, "checks": checks}


def format_report(result: dict[str, Any]) -> str:
    status = "PASS" if result["failed"] == 0 else "FAIL"
    lines = [
        f"SHELFWISE EVAL {status}: "
        f"{result['passed']}/{len(result['checks'])} checks passed"
    ]
    for item in result["checks"]:
        status = "PASS" if item["passed"] else "FAIL"
        lines.append(f"{status} {item['name']}: {item['detail']}")
    return "\n".join(lines)
