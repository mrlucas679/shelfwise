from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import health_monitor
from scripts.health_monitor import append_incident, monitor, post_alert


class _Response:
    def __init__(self, payload: dict[str, object], status: int = 200) -> None:
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")
        self._read = False

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, _: int = -1) -> bytes:
        if self._read:
            return b""
        self._read = True
        return self._body


def test_monitor_requires_both_liveness_and_seed_readiness() -> None:
    def healthy_opener(request: Any, **_: object) -> _Response:
        path = request.full_url
        if path.endswith("/health"):
            return _Response({"ok": True, "service": "shelfwise"})
        return _Response(
            {"ready": True, "checks": {"backend": "ok", "seed_data": "ok"}}
        )

    result = monitor(
        "https://shelfwise.example/client?ignored=yes",
        timeout_seconds=2,
        opener=healthy_opener,
    )

    assert result["healthy"] is True
    assert result["target_origin"] == "https://shelfwise.example"
    assert [check["path"] for check in result["checks"]] == [
        "/health",
        "/readiness",
    ]


def test_monitor_fails_closed_on_unready_seed_without_copying_payload() -> None:
    def unready_opener(request: Any, **_: object) -> _Response:
        path = request.full_url
        if path.endswith("/health"):
            return _Response({"ok": True, "service": "shelfwise", "secret": "hidden"})
        return _Response(
            {
                "ready": True,
                "checks": {"backend": "ok", "seed_data": "error"},
                "raw_exception": "must not escape",
            }
        )

    result = monitor(
        "http://127.0.0.1:8000",
        timeout_seconds=2,
        opener=unready_opener,
    )

    assert result["healthy"] is False
    assert result["checks"][1]["error_code"] == "unready_payload"
    assert "secret" not in json.dumps(result)
    assert "raw_exception" not in json.dumps(result)


def test_incident_log_is_bounded(tmp_path: Path) -> None:
    path = tmp_path / "incidents.jsonl"
    for index in range(5):
        append_incident(path, {"sequence": index}, max_entries=3)

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [record["sequence"] for record in records] == [2, 3, 4]


def test_webhook_rejects_insecure_or_embedded_credentials() -> None:
    with pytest.raises(ValueError):
        post_alert(
            "http://alerts.example/hook",
            {"healthy": False},
            timeout_seconds=2,
        )
    with pytest.raises(ValueError):
        post_alert(
            "https://user:password@alerts.example/hook",
            {"healthy": False},
            timeout_seconds=2,
        )


def test_cli_exits_nonzero_and_writes_bounded_failure_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    receipt = {
        "checked_at": "2026-07-28T12:00:00+00:00",
        "target_origin": "https://shelfwise.example",
        "healthy": False,
        "checks": [
            {
                "path": "/health",
                "ok": False,
                "status_code": 503,
                "error_code": "http_error",
            }
        ],
    }
    monkeypatch.setattr(health_monitor, "monitor", lambda *_args, **_kwargs: receipt)
    path = tmp_path / "incidents.jsonl"

    exit_code = health_monitor.main(
        [
            "--base-url",
            "https://shelfwise.example",
            "--incident-log",
            str(path),
        ]
    )

    assert exit_code == 1
    assert json.loads(path.read_text())["checks"][0]["error_code"] == "http_error"
