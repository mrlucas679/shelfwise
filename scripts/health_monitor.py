#!/usr/bin/env python3
"""Dependency-free ShelfWise liveness/readiness probe with bounded incident receipts."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

MAX_RESPONSE_BYTES = 64 * 1024
MAX_INCIDENT_ENTRIES = 500
OpenUrl = Callable[..., Any]


def monitor(base_url: str, *, timeout_seconds: float, opener: OpenUrl = urlopen) -> dict[str, Any]:
    """Probe public health and readiness endpoints without retaining response bodies."""
    origin = _safe_origin(base_url)
    checks = [
        _probe_endpoint(origin, "/health", timeout_seconds=timeout_seconds, opener=opener),
        _probe_endpoint(
            origin,
            "/readiness",
            timeout_seconds=timeout_seconds,
            opener=opener,
        ),
    ]
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "target_origin": origin,
        "healthy": all(check["ok"] for check in checks),
        "checks": checks,
    }


def append_incident(
    path: Path,
    receipt: dict[str, Any],
    *,
    max_entries: int = MAX_INCIDENT_ENTRIES,
) -> None:
    """Atomically retain only the newest bounded incident metadata."""
    if max_entries < 1:
        raise ValueError("max_entries must be positive")
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[str] = []
    if path.exists() and path.stat().st_size <= 2 * 1024 * 1024:
        existing = path.read_text(encoding="utf-8").splitlines()
    serialized = json.dumps(receipt, separators=(",", ":"), sort_keys=True)
    lines = [*existing[-(max_entries - 1) :], serialized]
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(path)


def post_alert(
    webhook_url: str,
    receipt: dict[str, Any],
    *,
    timeout_seconds: float,
    opener: OpenUrl = urlopen,
) -> None:
    """POST bounded incident metadata to an operator-configured HTTPS webhook."""
    parsed = urlsplit(webhook_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("alert webhook must be an HTTPS URL without embedded credentials")
    request = Request(
        webhook_url,
        data=json.dumps(receipt, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "ShelfWise-health-monitor/1"},
        method="POST",
    )
    with opener(request, timeout=timeout_seconds) as response:
        if int(response.status) < 200 or int(response.status) >= 300:
            raise RuntimeError("alert webhook rejected the incident")


def _probe_endpoint(
    origin: str,
    path: str,
    *,
    timeout_seconds: float,
    opener: OpenUrl,
) -> dict[str, Any]:
    request = Request(f"{origin}{path}", headers={"Accept": "application/json"})
    try:
        with opener(request, timeout=timeout_seconds) as response:
            status_code = int(response.status)
            raw_payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw_payload) > MAX_RESPONSE_BYTES:
                raise ValueError("response exceeds monitor limit")
            payload = json.loads(raw_payload)
            if not isinstance(payload, dict):
                raise ValueError("response must be a JSON object")
        payload_ok = _payload_is_ready(path, payload)
        return {
            "path": path,
            "ok": 200 <= status_code < 300 and payload_ok,
            "status_code": status_code,
            "error_code": None if payload_ok else "unready_payload",
        }
    except HTTPError as exc:
        return {
            "path": path,
            "ok": False,
            "status_code": exc.code,
            "error_code": "http_error",
        }
    except (URLError, TimeoutError):
        return {
            "path": path,
            "ok": False,
            "status_code": None,
            "error_code": "connection_error",
        }
    except ValueError:
        return {
            "path": path,
            "ok": False,
            "status_code": None,
            "error_code": "invalid_response",
        }


def _payload_is_ready(path: str, payload: dict[str, Any]) -> bool:
    if path == "/health":
        return payload.get("ok") is True and payload.get("service") == "shelfwise"
    checks = payload.get("checks")
    return (
        payload.get("ready") is True
        and isinstance(checks, dict)
        and checks.get("backend") == "ok"
        and checks.get("seed_data") == "ok"
    )


def _safe_origin(base_url: str) -> str:
    parsed = urlsplit(base_url.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("base URL must be HTTP(S) without embedded credentials")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--incident-log", type=Path)
    args = parser.parse_args(argv)
    if args.timeout_seconds <= 0 or args.timeout_seconds > 60:
        parser.error("--timeout-seconds must be greater than 0 and at most 60")
    try:
        receipt = monitor(args.base_url, timeout_seconds=args.timeout_seconds)
    except ValueError as exc:
        print(json.dumps({"healthy": False, "error_code": "invalid_configuration"}))
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(receipt, separators=(",", ":"), sort_keys=True))
    if receipt["healthy"]:
        return 0
    webhook = os.getenv("SHELFWISE_ALERT_WEBHOOK_URL", "").strip()
    if webhook:
        try:
            post_alert(webhook, receipt, timeout_seconds=args.timeout_seconds)
        except (OSError, RuntimeError, ValueError):
            receipt["alert_delivery"] = "failed"
    if args.incident_log is not None:
        append_incident(args.incident_log, receipt)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
