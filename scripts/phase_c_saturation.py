"""Run a bounded public-HTTP ingest saturation probe for the Phase C Compose stack."""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx


def main() -> int:
    """Submit unique scan events for a fixed duration and write a secret-free receipt."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--duration-seconds", type=float, default=300.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.duration_seconds <= 900:
        parser.error("duration-seconds must be between 1 and 900")

    status_counts: dict[str, int] = {}
    latencies: list[float] = []
    started = time.perf_counter()
    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=20.0) as client:
        client.post("/auth/session").raise_for_status()
        token = client.cookies.get("shelfwise_session")
        if not token:
            raise RuntimeError("session cookie missing")
        client.headers["authorization"] = f"Bearer {token}"
        while time.perf_counter() - started < args.duration_seconds:
            event_id = f"phase_c_saturation_{uuid4().hex}"
            payload = {
                "id": event_id,
                "type": "scan",
                "ts": datetime.now(UTC).isoformat(),
                "actor": "phase_c_load",
                "source": "scanner",
                "tenant_id": "sa_retail_demo",
                "data_domain": "world_simulation",
                "payload": {"sku": "P00001883", "location": "store_12"},
            }
            request_started = time.perf_counter()
            try:
                response = client.post("/ingest", json=payload)
                status = str(response.status_code)
            except httpx.HTTPError as exc:
                status = type(exc).__name__
            latencies.append((time.perf_counter() - request_started) * 1000)
            status_counts[status] = status_counts.get(status, 0) + 1
    latencies.sort()
    total = len(latencies)
    receipt = {
        "schema_version": "phase-c-saturation/v1",
        "duration_seconds": round(time.perf_counter() - started, 2),
        "requests": total,
        "status_counts": status_counts,
        "p50_ms": round(latencies[total // 2], 2) if total else None,
        "p95_ms": round(latencies[max(0, int(total * 0.95) - 1)], 2) if total else None,
        "max_ms": round(max(latencies), 2) if total else None,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(receipt, sort_keys=True))
    return 0 if status_counts and set(status_counts) <= {"200"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
