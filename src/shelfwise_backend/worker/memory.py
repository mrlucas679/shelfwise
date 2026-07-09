from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from shelfwise_mlops import OutcomeRecord, consolidate_outcomes

from .journal import InMemoryJournal, PostgresJournal, journaled

OutcomeReader = Callable[[str], list[OutcomeRecord]]


class MemoryConsolidationWorker:
    """Journal governed memory consolidation for tenant learning facts."""

    def __init__(
        self,
        *,
        journal: InMemoryJournal | PostgresJournal,
        fact_store: Any,
        records_for_tenant: OutcomeReader,
    ) -> None:
        self._journal = journal
        self._fact_store = fact_store
        self._records_for_tenant = records_for_tenant

    def process_tenant(self, tenant_id: str) -> dict[str, Any]:
        records = self._records_for_tenant(tenant_id)
        run_id = f"memory_consolidation_{tenant_id}_{_fingerprint(records)}"
        self._journal.start_run(run_id, tenant_id=tenant_id)
        try:
            result = journaled(
                self._journal,
                run_id,
                "consolidate_outcomes",
                lambda: self._consolidate(records),
            )
            self._journal.finish_run(run_id, status="done")
        except Exception:
            self._journal.finish_run(run_id, status="failed")
            raise
        return {
            "tenant_id": tenant_id,
            "run_id": run_id,
            "status": "done",
            **result,
        }

    def _consolidate(self, records: list[OutcomeRecord]) -> dict[str, Any]:
        facts = consolidate_outcomes(records)
        persisted = self._fact_store.record_many(facts)
        return {
            "records_considered": len(records),
            "facts_written": len(persisted),
            "facts": persisted,
        }


def _fingerprint(records: list[OutcomeRecord]) -> str:
    payload = [
        {
            "tenant_id": record.tenant_id,
            "sku": record.sku,
            "action": record.action,
            "success_score": str(record.success_score),
            "evidence_refs": list(record.evidence_refs),
        }
        for record in records
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
