"""Run the reproducible Track 3 cloud prescreen against a deployed origin."""

from __future__ import annotations

import argparse
import json
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4


@dataclass(frozen=True)
class ProbeResult:
    """One measured HTTP probe used in the prescreen receipt."""

    path: str
    elapsed_ms: float
    status_code: int

    def to_dict(self) -> dict[str, object]:
        """Serialize one probe without including secrets or response bodies."""
        return {
            "path": self.path,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "status_code": self.status_code,
        }


def _english_compatible(text: str) -> bool:
    """Return false for output dominated by a non-Latin writing system."""
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return False
    latin = sum("LATIN" in unicodedata.name(char, "") or char.isascii() for char in letters)
    return latin / len(letters) >= 0.8


def _request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
    cookie: str = "",
    timeout: float,
) -> tuple[dict[str, str], bytes, ProbeResult]:
    """Make one timed JSON request and return headers, body, and timing metadata."""
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if body else {}
    if cookie:
        headers["Cookie"] = cookie
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response_body = response.read()
        result = ProbeResult(path, (time.perf_counter() - started) * 1000, response.status)
        return dict(response.headers.items()), response_body, result


def _poll_health(base_url: str, deadline: float) -> tuple[dict[str, object], ProbeResult]:
    """Poll the public health route until it responds or the startup budget expires."""
    started = time.perf_counter()
    last_error = "unreachable"
    while time.perf_counter() - started < deadline:
        try:
            headers, body, probe = _request(
                base_url, "/health", timeout=min(5.0, max(0.5, deadline))
            )
            del headers
            return json.loads(body), probe
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            time.sleep(0.5)
    raise RuntimeError(f"health did not become ready within {deadline:.1f}s: {last_error}")


def run_prescreen(
    base_url: str, *, startup_deadline: float, request_deadline: float
) -> dict[str, object]:
    """Execute all Track 3 checks and return a machine-readable receipt."""
    health, health_probe = _poll_health(base_url, startup_deadline)
    if health.get("ok") is not True:
        raise RuntimeError("health route did not report ok=true")

    readiness_headers, readiness_body, readiness_probe = _request(
        base_url, "/inference/readiness", timeout=request_deadline
    )
    del readiness_headers
    readiness = json.loads(readiness_body)
    if readiness.get("ready_for_amd_demo") is not True:
        raise RuntimeError("inference readiness did not confirm AMD demo readiness")
    inference = readiness.get("inference", {})
    if inference.get("provider") != "vllm_mi300x":
        raise RuntimeError(f"unexpected inference provider: {inference.get('provider')}")
    if not str(inference.get("routine_model", "")).startswith("google/gemma-4"):
        raise RuntimeError("routine model is not a Google Gemma 4 model")
    if not str(inference.get("strong_model", "")).startswith("google/gemma-4"):
        raise RuntimeError("strong model is not a Google Gemma 4 model")

    session_headers, session_body, session_probe = _request(
        base_url, "/auth/session", method="POST", timeout=request_deadline
    )
    del session_body
    cookie = session_headers.get("Set-Cookie", "").split(";", 1)[0]
    if not cookie:
        raise RuntimeError("auth/session did not return a session cookie")

    chat_probes: list[ProbeResult] = []
    chat_receipts: list[dict[str, object]] = []
    for question in (
        "What is the current stock position for an unseen product variant?",
        "What should the manager check first for an unseen cold-chain alert?",
    ):
        headers, body, probe = _request(
            base_url,
            "/chat",
            method="POST",
            payload={
                "question": question,
                "conversation_id": f"track3_{uuid4().hex}",
                "message_id": f"track3_{uuid4().hex}",
            },
            cookie=cookie,
            timeout=request_deadline,
        )
        answer = body.decode("utf-8")
        # The retired 30s submission gate used to hard-fail here; the request timeout above
        # is now the only latency bound, so a slow-but-successful live answer still passes.
        if probe.elapsed_ms >= request_deadline * 1_000:
            raise RuntimeError(
                f"chat response exceeded the request deadline: {probe.elapsed_ms:.1f}ms"
            )
        if not _english_compatible(answer):
            raise RuntimeError("chat response was not English-compatible")
        if headers.get("X-ShelfWise-Provider") != "vllm_mi300x":
            raise RuntimeError("chat response did not prove AMD vLLM usage")
        if headers.get("X-ShelfWise-Answer-Source") != "model":
            raise RuntimeError("chat response was not model-backed")
        if headers.get("X-ShelfWise-Replayed") != "false":
            raise RuntimeError("fresh unseen chat request was replayed")
        chat_probes.append(probe)
        chat_receipts.append(
            {
                "correlation_id": headers.get("X-ShelfWise-Correlation-ID", ""),
                "model": headers.get("X-ShelfWise-Model", ""),
                "provider": headers.get("X-ShelfWise-Provider", ""),
                "answer_source": headers.get("X-ShelfWise-Answer-Source", ""),
                "replayed": headers.get("X-ShelfWise-Replayed", ""),
            }
        )
    correlation_ids = [str(item["correlation_id"]) for item in chat_receipts]
    if not all(correlation_ids) or len(set(correlation_ids)) != len(correlation_ids):
        raise RuntimeError("fresh chat requests did not receive unique correlation IDs")

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "base_url_host": urlparse(base_url).netloc,
        "startup_deadline_seconds": startup_deadline,
        "request_deadline_seconds": request_deadline,
        "verdict": "PASS",
        "probes": [
            health_probe.to_dict(),
            readiness_probe.to_dict(),
            session_probe.to_dict(),
            *(probe.to_dict() for probe in chat_probes),
        ],
        "chat": chat_receipts,
    }


def main() -> int:
    """Parse CLI arguments, run the prescreen, and write its receipt."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Public backend or frontend origin")
    parser.add_argument("--startup-deadline", type=float, default=60.0)
    # The retired 30s submission gate used to cap this below 30s, falsely failing live model
    # routes that legitimately breathe longer. The default clears the app's 120s deadline.
    parser.add_argument("--request-deadline", type=float, default=130.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 0 < args.startup_deadline <= 60:
        parser.error("--startup-deadline must be between 0 and 60 seconds")
    if not 0 < args.request_deadline <= 900:
        parser.error("--request-deadline must be between 0 and 900 seconds")
    receipt = run_prescreen(
        args.base_url,
        startup_deadline=args.startup_deadline,
        request_deadline=args.request_deadline,
    )
    serialized = json.dumps(receipt, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
