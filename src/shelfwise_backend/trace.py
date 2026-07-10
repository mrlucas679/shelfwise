from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from threading import Lock
from typing import Any


@dataclass(slots=True)
class CascadeTrace:
    correlation_id: str
    scenario: str | None
    spans: list[dict[str, Any]] = field(default_factory=list)
    evidence_agents: list[str] = field(default_factory=list)
    decision_id: str | None = None
    status: str = "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "correlation_id": self.correlation_id,
            "scenario": self.scenario,
            "status": self.status,
            "spans": deepcopy(self.spans),
            "evidence_agents": list(self.evidence_agents),
            "decision_id": self.decision_id,
        }


class TraceRegistry:
    """Bounded in-process registry for recent cascade traces."""

    def __init__(self, *, max_items: int = 200) -> None:
        self._lock = Lock()
        self._max_items = max_items
        self._order: deque[str] = deque()
        self._traces: dict[str, CascadeTrace] = {}

    def put(self, trace: CascadeTrace | dict[str, Any]) -> None:
        record = trace if isinstance(trace, CascadeTrace) else trace_from_cascade(trace)
        if not record.correlation_id:
            return
        with self._lock:
            if record.correlation_id not in self._traces:
                self._order.append(record.correlation_id)
            self._traces[record.correlation_id] = record
            while len(self._order) > self._max_items:
                expired = self._order.popleft()
                self._traces.pop(expired, None)

    def get(self, correlation_id: str) -> dict[str, Any] | None:
        with self._lock:
            found = self._traces.get(correlation_id)
            return found.to_dict() if found else None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                self._traces[item].to_dict()
                for item in reversed(self._order)
                if item in self._traces
            ]

    def clear(self) -> None:
        with self._lock:
            self._order.clear()
            self._traces.clear()


def trace_from_cascade(cascade: dict[str, Any]) -> CascadeTrace:
    """Create a trace record from a cascade response payload."""
    decision = cascade.get("decision") if isinstance(cascade.get("decision"), dict) else {}
    evidence = cascade.get("evidence") if isinstance(cascade.get("evidence"), list) else []
    return CascadeTrace(
        correlation_id=str(cascade.get("correlation_id") or ""),
        scenario=cascade.get("scenario"),
        spans=deepcopy(cascade.get("trace") if isinstance(cascade.get("trace"), list) else []),
        evidence_agents=[
            str(item.get("agent"))
            for item in evidence
            if isinstance(item, dict) and item.get("agent") is not None
        ],
        decision_id=str(decision.get("id")) if decision.get("id") else None,
        status=str(cascade.get("status") or "ok"),
    )
