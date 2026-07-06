from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from threading import Lock
from typing import Any


class DecisionStore:
    """Tiny in-memory HITL store for the hackathon demo.

    The durable Postgres journal is a later slice. For the demo we need a
    clear approval loop that is deterministic, idempotent enough for repeat
    clicks, and visible to the frontend.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._decisions: dict[str, dict[str, Any]] = {}

    def upsert(self, decision: dict[str, Any]) -> dict[str, Any]:
        decision_id = str(decision.get("id", ""))
        if not decision_id:
            raise ValueError("decision must include id")

        with self._lock:
            existing = self._decisions.get(decision_id)
            if existing and existing.get("status") in {"approved", "rejected"}:
                return deepcopy(existing)

            record = deepcopy(decision)
            record.setdefault("created_at", _now())
            record.setdefault("updated_at", record["created_at"])
            record.setdefault("review", None)
            self._decisions[decision_id] = record
            return deepcopy(record)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [deepcopy(item) for item in self._decisions.values()]

    def get(self, decision_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._decisions.get(decision_id)
            return deepcopy(item) if item else None

    def approve(self, decision_id: str, *, reviewer: str = "demo_manager") -> dict[str, Any] | None:
        return self._transition(decision_id, "approved", reviewer)

    def reject(self, decision_id: str, *, reviewer: str = "demo_manager") -> dict[str, Any] | None:
        return self._transition(decision_id, "rejected", reviewer)

    def _transition(self, decision_id: str, status: str, reviewer: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._decisions.get(decision_id)
            if item is None:
                return None
            updated = deepcopy(item)
            updated["status"] = status
            updated["updated_at"] = _now()
            updated["review"] = {
                "reviewer": reviewer,
                "status": status,
                "reviewed_at": updated["updated_at"],
            }
            self._decisions[decision_id] = updated
            return deepcopy(updated)


def _now() -> str:
    return datetime.now(UTC).isoformat()
