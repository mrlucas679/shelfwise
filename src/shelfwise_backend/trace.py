from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from threading import Lock
from typing import Any


@dataclass(slots=True)
class CascadeTrace:
    correlation_id: str
    tenant_id: str
    data_domain: str
    scenario: str | None
    spans: list[dict[str, Any]] = field(default_factory=list)
    evidence_agents: list[str] = field(default_factory=list)
    decision_id: str | None = None
    status: str = "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "correlation_id": self.correlation_id,
            "tenant_id": self.tenant_id,
            "data_domain": self.data_domain,
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
        self._order: deque[tuple[str, str, str]] = deque()
        self._traces: dict[tuple[str, str, str], CascadeTrace] = {}

    def put(self, trace: CascadeTrace | dict[str, Any]) -> None:
        record = trace if isinstance(trace, CascadeTrace) else trace_from_cascade(trace)
        if not record.correlation_id:
            return
        key = (record.tenant_id, record.data_domain, record.correlation_id)
        with self._lock:
            if key not in self._traces:
                self._order.append(key)
            self._traces[key] = record
            while len(self._order) > self._max_items:
                expired = self._order.popleft()
                self._traces.pop(expired, None)

    def get(
        self, correlation_id: str, *, tenant_id: str, data_domain: str | None = None
    ) -> dict[str, Any] | None:
        with self._lock:
            matches = [
                trace
                for (trace_tenant, trace_domain, trace_id), trace in self._traces.items()
                if trace_tenant == tenant_id
                and trace_id == correlation_id
                and (data_domain is None or trace_domain == data_domain)
            ]
            return matches[0].to_dict() if len(matches) == 1 else None

    def list(
        self, *, tenant_id: str, data_domain: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            return [
                self._traces[item].to_dict()
                for item in reversed(self._order)
                if item in self._traces
                and self._traces[item].tenant_id == tenant_id
                and (data_domain is None or self._traces[item].data_domain == data_domain)
            ]

    def clear(self) -> None:
        with self._lock:
            self._order.clear()
            self._traces.clear()


def trace_from_cascade(cascade: dict[str, Any]) -> CascadeTrace:
    """Create a trace record from a cascade response payload."""
    decision = cascade.get("decision") if isinstance(cascade.get("decision"), dict) else {}
    tenant_id = str(cascade.get("tenant_id") or decision.get("tenant_id") or "").strip()
    if not tenant_id:
        raise ValueError("cascade trace tenant_id is required")
    evidence = cascade.get("evidence") if isinstance(cascade.get("evidence"), list) else []
    return CascadeTrace(
        correlation_id=str(cascade.get("correlation_id") or ""),
        tenant_id=tenant_id,
        data_domain=str(cascade.get("data_domain") or "world_simulation"),
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
