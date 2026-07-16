"""Run the Phase C concurrency ramp (C1) against a deployed ShelfWise stack over public HTTP.

Each ramp step runs N virtual users for a fixed window; every user holds its own
authenticated session and drives the ingest loop. The goal is to find the number where the
stack breaks (5xx, transport errors, or p95 collapse) and record it as measured capacity -
not to stop at a green low step.
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


def _percentile(sorted_values: list[float], pct: float) -> float | None:
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1, max(0, int(pct * len(sorted_values) + 0.5) - 1))
    return round(sorted_values[index], 2)


def _authenticated_client(base_url: str) -> httpx.Client:
    """Create one session-scoped client, bridging the Secure cookie to a bearer header.

    Mirrors deployment_shakedown's HTTP-only-origin workaround: the harness uses its own
    in-memory session token as a bearer credential and never serializes it anywhere.
    """
    client = httpx.Client(base_url=base_url.rstrip("/"), timeout=20.0)
    client.post("/auth/session").raise_for_status()
    token = client.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        client.close()
        raise RuntimeError("session cookie missing")
    client.headers["authorization"] = f"Bearer {token}"
    return client


def _user_loop(
    base_url: str,
    deadline: float,
    latencies: list[float],
    status_counts: dict[str, int],
    lock: threading.Lock,
    user_index: int,
) -> None:
    try:
        client = _authenticated_client(base_url)
    except Exception as exc:
        with lock:
            status_counts[f"session_error:{type(exc).__name__}"] = (
                status_counts.get(f"session_error:{type(exc).__name__}", 0) + 1
            )
        return
    try:
        while time.perf_counter() < deadline:
            payload = {
                "id": f"phase_c_ramp_{user_index}_{uuid4().hex}",
                "type": "scan",
                "ts": datetime.now(UTC).isoformat(),
                "actor": "phase_c_ramp",
                "source": "scanner",
                "tenant_id": "sa_retail_demo",
                "data_domain": "world_simulation",
                "payload": {"sku": "P00001883", "location": "store_12"},
            }
            started = time.perf_counter()
            try:
                response = client.post("/ingest", json=payload)
                status = str(response.status_code)
            except httpx.HTTPError as exc:
                status = type(exc).__name__
            elapsed_ms = (time.perf_counter() - started) * 1000
            with lock:
                latencies.append(elapsed_ms)
                status_counts[status] = status_counts.get(status, 0) + 1
    finally:
        client.close()


def _run_step(base_url: str, users: int, step_seconds: float) -> dict[str, object]:
    latencies: list[float] = []
    status_counts: dict[str, int] = {}
    lock = threading.Lock()
    deadline = time.perf_counter() + step_seconds
    threads = [
        threading.Thread(
            target=_user_loop,
            args=(base_url, deadline, latencies, status_counts, lock, index),
            daemon=True,
        )
        for index in range(users)
    ]
    started = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=step_seconds + 30)
    duration = time.perf_counter() - started
    latencies.sort()
    total = len(latencies)
    server_errors = sum(
        count for status, count in status_counts.items() if status.startswith("5")
    )
    transport_errors = sum(
        count for status, count in status_counts.items() if not status.isdigit()
    )
    return {
        "users": users,
        "duration_seconds": round(duration, 2),
        "requests": total,
        "requests_per_second": round(total / duration, 2) if duration else 0,
        "status_counts": dict(sorted(status_counts.items())),
        "p50_ms": _percentile(latencies, 0.50),
        "p95_ms": _percentile(latencies, 0.95),
        "p99_ms": _percentile(latencies, 0.99),
        "max_ms": round(latencies[-1], 2) if latencies else None,
        "server_errors": server_errors,
        "transport_errors": transport_errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--steps", default="1,8,32,64")
    parser.add_argument("--step-seconds", type=float, default=60.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    steps = [int(item) for item in args.steps.split(",") if item.strip()]
    if not steps or any(step < 1 for step in steps):
        parser.error("steps must be positive integers")
    if not 5 <= args.step_seconds <= 600:
        parser.error("step-seconds must be between 5 and 600")

    step_receipts: list[dict[str, object]] = []
    breaking_point: int | None = None
    baseline_p95: float | None = None
    for users in steps:
        receipt = _run_step(args.base_url, users, args.step_seconds)
        step_receipts.append(receipt)
        print(json.dumps(receipt, sort_keys=True))
        p95 = receipt["p95_ms"]
        if baseline_p95 is None and isinstance(p95, float):
            baseline_p95 = p95
        degraded = (
            receipt["server_errors"]
            or receipt["transport_errors"]
            or (
                baseline_p95 is not None
                and isinstance(p95, float)
                and p95 > max(10 * baseline_p95, 5_000.0)
            )
        )
        if degraded and breaking_point is None:
            breaking_point = users

    receipt = {
        "schema_version": "phase-c-ramp/v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "steps": step_receipts,
        "breaking_point_users": breaking_point,
        "verdict": "BREAKING_POINT_FOUND" if breaking_point else "NO_BREAK_UP_TO_MAX_STEP",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: receipt[k] for k in ("breaking_point_users", "verdict")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
