"""Run the Phase C concurrency race hunts (C4) against a deployed ShelfWise stack over HTTP.

Three races, each fired from N simultaneous threads against the real server:

1. HITL race: approve and reject the SAME pending decision concurrently. Exactly one terminal
   state must win, and the idempotent write-back sink must mint at most one task for it.
2. Twin observation race: submit the SAME observation_id concurrently. Exactly one submission
   may project; every other must be deduplicated, with zero 5xx.
3. Connector intake race: submit an identical Square payload concurrently. Exactly one
   "accepted"; every other must be "duplicate", with zero 5xx.

Any double-mint, double-terminal-state, or 5xx is a campaign failure (exit 1).
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

SESSION_COOKIE_NAME = "shelfwise_session"


def _authenticated_client(base_url: str) -> httpx.Client:
    """Create one session-scoped client, bridging the Secure cookie to a bearer header."""
    client = httpx.Client(base_url=base_url.rstrip("/"), timeout=30.0)
    client.post("/auth/session").raise_for_status()
    token = client.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        client.close()
        raise RuntimeError("session cookie missing")
    client.headers["authorization"] = f"Bearer {token}"
    return client


def _fire_concurrently(
    base_url: str, threads: int, request_builder
) -> list[dict[str, object]]:
    """Run `request_builder(client) -> outcome dict` from N threads behind one barrier."""
    outcomes: list[dict[str, object]] = []
    lock = threading.Lock()
    barrier = threading.Barrier(threads)

    def worker(index: int) -> None:
        try:
            client = _authenticated_client(base_url)
        except Exception as exc:
            with lock:
                outcomes.append({"thread": index, "error": type(exc).__name__})
            barrier.abort()
            return
        try:
            barrier.wait(timeout=60)
            outcome = request_builder(client, index)
        except Exception as exc:
            outcome = {"error": type(exc).__name__}
        finally:
            client.close()
        outcome["thread"] = index
        with lock:
            outcomes.append(outcome)

    workers = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(threads)]
    for item in workers:
        item.start()
    for item in workers:
        item.join(timeout=120)
    return outcomes


def _server_errors(outcomes: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        o
        for o in outcomes
        if isinstance(o.get("status_code"), int) and o["status_code"] >= 500
    ]


def _race_hitl(base_url: str, threads: int) -> dict[str, object]:
    setup = _authenticated_client(base_url)
    try:
        scenario = setup.post("/scenarios/golden")
        scenario.raise_for_status()
        decision = scenario.json().get("decision") or {}
        decision_id = str(decision.get("id") or "")
        if not decision_id:
            raise RuntimeError("golden scenario produced no decision id")

        def transition(client: httpx.Client, index: int) -> dict[str, object]:
            action = "approve" if index % 2 == 0 else "reject"
            response = client.post(f"/decisions/{decision_id}/{action}", json={})
            body = response.json() if response.status_code == 200 else {}
            resolved = body.get("decision") if isinstance(body.get("decision"), dict) else {}
            return {
                "action": action,
                "status_code": response.status_code,
                "resolved_status": str(resolved.get("status") or ""),
            }

        outcomes = _fire_concurrently(base_url, threads, transition)

        final = setup.get(f"/decisions/{decision_id}")
        final_body = final.json() if final.status_code == 200 else {}
        final_decision = (
            final_body.get("decision") if isinstance(final_body.get("decision"), dict) else {}
        )
        final_status = str(final_decision.get("status") or "")

        tasks = setup.get("/writeback/tasks")
        task_rows = tasks.json().get("tasks") if tasks.status_code == 200 else []
        matching_tasks = [
            row
            for row in (task_rows or [])
            if decision_id in json.dumps(row, default=str)
        ]
        server_errors = _server_errors(outcomes)
        terminal_claims = {
            o["resolved_status"]
            for o in outcomes
            if o.get("status_code") == 200
            and o.get("resolved_status") in {"approved", "rejected"}
        }
        expected_tasks = 1 if final_status == "approved" else 0
        passed = (
            not server_errors
            and final_status in {"approved", "rejected"}
            and len(terminal_claims) == 1
            and len(matching_tasks) == expected_tasks
        )
        return {
            "race": "hitl_approve_reject",
            "decision_id": decision_id,
            "threads": threads,
            "final_status": final_status,
            "terminal_states_claimed": sorted(terminal_claims),
            "writeback_tasks_for_decision": len(matching_tasks),
            "server_errors": len(server_errors),
            "passed": passed,
        }
    finally:
        setup.close()


def _race_twin_observations(base_url: str, threads: int) -> dict[str, object]:
    setup = _authenticated_client(base_url)
    stamp = uuid4().hex[:10]
    store_id = f"phase_c_race_store_{stamp}"
    tenant_id = "sa_retail_demo"
    try:
        onboard = setup.post(
            "/twin/onboarding",
            json={
                "tenant_id": tenant_id,
                "store_id": store_id,
                "display_name": "Phase C race store",
                "timezone": "Africa/Johannesburg",
                "entities": [
                    {
                        "local_id": "fridge_race",
                        "entity_type": "fixture",
                        "display_name": "Race probe fridge",
                        "attributes": {"zone": "dairy"},
                    }
                ],
            },
        )
        onboard.raise_for_status()
        observation_id = f"obs_phase_c_race_{stamp}"
        payload = {
            "observation_id": observation_id,
            "tenant_id": tenant_id,
            "store_id": store_id,
            "twin_id": f"urn:shelfwise:{tenant_id}:{store_id}:fixture:fridge_race",
            "property_name": "cold_chain.status",
            "lane": "reported",
            "value": "healthy",
            "observed_at": datetime.now(UTC).isoformat(),
            "source_system": "phase_c_race",
            "source_object_id": f"race-{stamp}",
            "source_quality": 1.0,
            "correlation_id": f"cor_phase_c_race_{stamp}",
            "payload_hash": "e" * 64,
        }

        def observe(client: httpx.Client, index: int) -> dict[str, object]:
            response = client.post("/twin/observations", json=payload)
            body = response.json() if response.status_code == 200 else {}
            result = body.get("result") if isinstance(body.get("result"), dict) else {}
            return {
                "status_code": response.status_code,
                "result_status": str(result.get("status") or ""),
            }

        outcomes = _fire_concurrently(base_url, threads, observe)
        projected = [o for o in outcomes if o.get("result_status") == "projected"]
        server_errors = _server_errors(outcomes)
        passed = len(projected) == 1 and not server_errors
        return {
            "race": "twin_duplicate_observations",
            "observation_id": observation_id,
            "threads": threads,
            "projected_count": len(projected),
            "result_statuses": dict(
                sorted(
                    {
                        status: sum(1 for o in outcomes if o.get("result_status") == status)
                        for status in {str(o.get("result_status") or "") for o in outcomes}
                    }.items()
                )
            ),
            "server_errors": len(server_errors),
            "passed": passed,
        }
    finally:
        setup.close()


def _race_connector_intake(base_url: str, threads: int) -> dict[str, object]:
    stamp = uuid4().hex[:10]
    square_payload = {
        "type": "inventory.count.updated",
        "data": {
            "object": {
                "inventory_counts": [
                    {
                        "catalog_object_id": f"sq_race_{stamp}",
                        "location_id": "local-site",
                        "quantity": "77",
                    }
                ]
            }
        },
    }

    def intake(client: httpx.Client, index: int) -> dict[str, object]:
        response = client.post("/connectors/square/intake", json={"payload": square_payload})
        body = response.json() if response.status_code == 200 else {}
        return {"status_code": response.status_code, "status": str(body.get("status") or "")}

    outcomes = _fire_concurrently(base_url, threads, intake)
    accepted = [o for o in outcomes if o.get("status") == "accepted"]
    server_errors = _server_errors(outcomes)
    passed = len(accepted) == 1 and not server_errors
    return {
        "race": "connector_duplicate_intake",
        "threads": threads,
        "accepted_count": len(accepted),
        "statuses": dict(
            sorted(
                {
                    status: sum(1 for o in outcomes if o.get("status") == status)
                    for status in {str(o.get("status") or "") for o in outcomes}
                }.items()
            )
        ),
        "server_errors": len(server_errors),
        "passed": passed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--threads", type=int, default=32)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 2 <= args.threads <= 128:
        parser.error("threads must be between 2 and 128")

    started = time.perf_counter()
    races = [
        _race_hitl(args.base_url, args.threads),
        _race_twin_observations(args.base_url, args.threads),
        _race_connector_intake(args.base_url, args.threads),
    ]
    receipt = {
        "schema_version": "phase-c-races/v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "duration_seconds": round(time.perf_counter() - started, 2),
        "races": races,
        "verdict": "PASS" if all(race["passed"] for race in races) else "FAIL",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
