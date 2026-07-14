from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from scripts import deployment_shakedown as shakedown


class FakeDeployment:
    def __init__(self, *, failure_path: str | None = None, tenant_mismatch: bool = False) -> None:
        self.failure_path = failure_path
        self.tenant_mismatch = tenant_mismatch
        self.decisions: dict[str, dict[str, Any]] = {}
        self.learning_events: list[dict[str, Any]] = []
        self.chat_messages: set[str] = set()
        self.counter = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == self.failure_path:
            return httpx.Response(503, request=request, json={"detail": "route unavailable"})
        if path == "/":
            return httpx.Response(200, request=request, text="<html>ShelfWise</html>")
        if path == "/health":
            return httpx.Response(200, request=request, json={"ok": True})
        if path == "/auth/session":
            return httpx.Response(
                200,
                request=request,
                headers={"set-cookie": "shelfwise_session=opaque-cookie; Path=/"},
                json={"mode": "jwt", "session": {"tenant_id": "tenant_a"}},
            )
        if path == "/readiness":
            return httpx.Response(
                200,
                request=request,
                json={
                    "ready": True,
                    "checks": {
                        "auth_mode": "jwt",
                        "tenant_auth_secret_configured": True,
                        "amd_demo": "pending",
                        "decision_store": "PostgresDecisionStore",
                        "learning_store": "PostgresLearningStore",
                        "event_store": "PostgresEventStore",
                        "event_bus": "RedisEventBus",
                    },
                    "inference": {"provider": "offline"},
                },
            )
        if path == "/demo/golden":
            self.counter += 1
            decision_id = f"decision-{self.counter}"
            decision = {
                "id": decision_id,
                "status": "pending",
                "tenant_id": "other" if self.tenant_mismatch else "tenant_a",
            }
            self.decisions[decision_id] = decision.copy()
            return httpx.Response(200, request=request, json={"decision": decision})
        if path.startswith("/decisions/"):
            parts = path.strip("/").split("/")
            decision_id = parts[1]
            decision = self.decisions.get(decision_id)
            if decision is None:
                return httpx.Response(404, request=request, json={"detail": "not found"})
            if len(parts) == 2:
                return httpx.Response(200, request=request, json={"decision": decision})
            action = parts[2]
            decision["status"] = "approved" if action == "approve" else "rejected"
            payload: dict[str, Any] = {"decision": decision, "learning_event": None}
            if action == "approve":
                event = {
                    "id": f"learning-{decision_id}",
                    "previous_threshold": 10,
                    "updated_threshold": 11,
                    "delta_units": 1,
                }
                self.learning_events.append(event)
                payload["learning_event"] = event
                decision["write_back"] = {"status": "pending_external_write"}
            return httpx.Response(200, request=request, json=payload)
        if path == "/decisions":
            return httpx.Response(
                200,
                request=request,
                json={"decisions": list(self.decisions.values())},
            )
        if path == "/learning":
            return httpx.Response(200, request=request, json={"events": self.learning_events})
        if path == "/writeback/tasks":
            tasks = [
                {"id": f"task-{event['id']}", "status": "pending_external_write"}
                for event in self.learning_events
            ]
            return httpx.Response(200, request=request, json={"tasks": tasks})
        if path == "/mlops/observability":
            return httpx.Response(200, request=request, json={"snapshot": {}})
        if path == "/chat":
            body = json.loads(request.content.decode())
            message_id = str(body["message_id"])
            replayed = message_id in self.chat_messages
            self.chat_messages.add(message_id)
            return httpx.Response(
                200,
                request=request,
                headers={
                    "X-ShelfWise-Answer-Source": "offline",
                    "X-ShelfWise-Correlation-ID": f"chat:{message_id}",
                    "X-ShelfWise-Provider": "offline",
                    "X-ShelfWise-Replayed": str(replayed).lower(),
                },
                text="Fallback answer",
            )
        return httpx.Response(404, request=request, json={"detail": "not found"})


def _run(monkeypatch, fake: FakeDeployment, **kwargs: Any) -> shakedown.DeploymentReceipt:
    real_client = httpx.Client

    def client_factory(*args: Any, **client_kwargs: Any) -> httpx.Client:
        client_kwargs["transport"] = fake.transport()
        return real_client(*args, **client_kwargs)

    monkeypatch.setattr(shakedown.httpx, "Client", client_factory)
    return shakedown.run_deployment_shakedown(
        shakedown.DeploymentShakedownConfig(
            base_url="http://fake.example",
            cycles=3,
            request_timeout=0.5,
            startup_deadline=0.1,
            poll_interval=0.001,
            **kwargs,
        )
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"base_url": "fake.example"}, "absolute http or https"),
        ({"base_url": "https://user:password@example.test"}, "credentials"),
        ({"cycles": 0}, "cycles"),
        ({"request_timeout": 30}, "request_timeout"),
        ({"duration_seconds": 901}, "duration_seconds"),
    ],
)
def test_deployment_shakedown_config_rejects_unsafe_or_unbounded_values(
    overrides: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "base_url": "http://fake.example",
        "cycles": 3,
        "request_timeout": 0.5,
        "startup_deadline": 0.1,
        "duration_seconds": 0.0,
        "poll_interval": 0.001,
    }
    values.update(overrides)

    with pytest.raises(ValueError, match=message):
        shakedown.DeploymentShakedownConfig(**values)


def test_deployment_shakedown_passes_short_mode_and_records_fallback(monkeypatch) -> None:
    receipt = _run(monkeypatch, FakeDeployment())

    assert receipt.verdict == "PASS", receipt.failures
    assert receipt.hitl.approvals == 2
    assert receipt.hitl.rejections == 1
    assert receipt.chat.fallback_answers == 3
    assert receipt.chat.replay_checks == receipt.chat.replay_matches == 3
    assert receipt.learning.movements == receipt.learning.movements_expected == 2
    assert receipt.writeback.pending_external_writes == 2


def test_deployment_shakedown_reports_timeout_without_raising(monkeypatch) -> None:
    class TimeoutServer(FakeDeployment):
        def handle(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                raise httpx.ReadTimeout("timed out", request=request)
            return super().handle(request)

    receipt = _run(monkeypatch, TimeoutServer())

    assert receipt.verdict == "FAIL"
    assert "request_timeout" in receipt.failures
    assert "startup_deadline_exceeded" in receipt.failures
    assert all("opaque-cookie" not in json.dumps(receipt.to_dict()) for _ in [0])


def test_deployment_shakedown_reports_route_failure(monkeypatch) -> None:
    receipt = _run(monkeypatch, FakeDeployment(failure_path="/learning"))

    assert receipt.verdict == "FAIL"
    assert "learning_route_failure" in receipt.failures
    assert any(route.path == "/learning" and not route.ok for route in receipt.routes)


def test_deployment_shakedown_rejects_tenant_mismatch(monkeypatch) -> None:
    receipt = _run(monkeypatch, FakeDeployment(tenant_mismatch=True))

    assert receipt.verdict == "FAIL"
    assert receipt.hitl.tenant_mismatches == 3
    assert any(item.startswith("tenant_mismatch:") for item in receipt.failures)


def test_deployment_shakedown_marks_missing_session_secret_cookie_without_storing_secrets(
    monkeypatch,
) -> None:
    class NoCookieServer(FakeDeployment):
        def handle(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/auth/session":
                return httpx.Response(
                    200,
                    request=request,
                    json={"mode": "jwt", "session": {"tenant_id": "tenant_a"}},
                )
            return super().handle(request)

    monkeypatch.delenv("SHELFWISE_API_KEY", raising=False)
    receipt = _run(monkeypatch, NoCookieServer())
    serialized = json.dumps(receipt.to_dict())

    assert receipt.verdict == "FAIL"
    assert "auth_session_cookie_missing" in receipt.failures
    assert receipt.auth.api_key_configured is False
    assert "opaque-cookie" not in serialized
    assert "SHELFWISE_API_KEY" not in serialized
