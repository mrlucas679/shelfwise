"""Route canonical events through one provenance-aware cascade entry point."""

from __future__ import annotations

from typing import Any

from shelfwise_contracts import Event, EventType
from shelfwise_runtime import DataDomain

from .cascade import (
    run_catalog_price_check,
    run_cold_chain_cascade,
    run_expiry_risk_check,
    run_golden_cascade,
    run_inventory_exception_cascade,
    run_procurement_cascade,
    run_recall_cascade,
    run_sales_cascade,
)
from .operational_facts import MissingOperationalFacts, OperationalFactsProvider


class CascadeDispatcher:
    """Use world facts only for world events and measured facts for live events."""

    def __init__(
        self,
        *,
        world_facts: Any,
        twin_service: Any,
        product_catalog_store: Any,
        inventory_position_store: Any,
    ) -> None:
        self._world_facts = world_facts
        self._twin_service = twin_service
        self._product_catalog_store = product_catalog_store
        self._inventory_position_store = inventory_position_store

    def run(self, event: Event) -> dict[str, Any] | None:
        """Dispatch one event and preserve its tenant, domain, and causality."""
        try:
            result = self._run(event)
        except MissingOperationalFacts as exc:
            result = {
                "scenario": "insufficient_operational_facts",
                "decision": None,
                "evidence": [],
                "trace": [],
                "status": "insufficient_operational_facts",
                "missing_data": list(exc.missing),
            }
        if result is None:
            return None
        return attach_event_causality(result, event)

    def run_worker(self, event: Event) -> dict[str, Any]:
        """Return a journal-safe receipt even when an event needs no cascade."""
        result = self.run(event)
        if result is not None:
            return result
        return attach_event_causality(
            {
                "scenario": None,
                "decision": None,
                "evidence": [],
                "trace": [],
                "status": "ignored",
            },
            event,
        )

    def _run(self, event: Event) -> dict[str, Any] | None:
        if event.data_domain not in {
            DataDomain.OPERATIONAL_TWIN,
            DataDomain.WORLD_SIMULATION,
        }:
            return {
                "scenario": None,
                "decision": None,
                "evidence": [],
                "trace": [],
                "status": "ignored_data_domain",
            }

        if event.type is EventType.RECALL_NOTICE:
            return run_recall_cascade(event)
        if event.type is EventType.INVENTORY_EXCEPTION:
            return run_inventory_exception_cascade(event)
        if event.type is EventType.SALE and _is_catalog_price_event(event):
            return run_catalog_price_check(event)
        if event.type is EventType.SALE and _has_partial_catalog_price(event):
            return None
        if event.type is EventType.EXPIRY_ENTRY:
            return run_expiry_risk_check(event)

        if event.data_domain is DataDomain.OPERATIONAL_TWIN:
            _require_operational_context(event)
        facts = self._facts_for(event)
        if event.type is EventType.SCAN:
            return run_golden_cascade(event, facts=facts)
        if event.type is EventType.SUPPLIER_UPDATE:
            return run_procurement_cascade(event, facts=facts)
        if event.type is EventType.SALE:
            return run_sales_cascade(event, facts=facts)
        if event.type is EventType.COLD_CHAIN_ALERT:
            return run_cold_chain_cascade(event, facts=facts)
        return None

    def _facts_for(self, event: Event) -> Any:
        if event.data_domain is DataDomain.WORLD_SIMULATION:
            return self._world_facts
        return OperationalFactsProvider(
            event,
            twin_service=self._twin_service,
            product_catalog_store=self._product_catalog_store,
            inventory_position_store=self._inventory_position_store,
        )


def attach_event_causality(result: dict[str, Any], event: Event) -> dict[str, Any]:
    """Stamp every result and decision with the event's trust boundary."""
    result["correlation_id"] = event.correlation_id
    result["tenant_id"] = event.tenant_id
    result["data_domain"] = event.data_domain.value
    decision = result.get("decision")
    if isinstance(decision, dict):
        decision["caused_by"] = [event.id]
        decision["tenant_id"] = event.tenant_id
        decision["data_domain"] = event.data_domain.value
    return result


def _is_catalog_price_event(event: Event) -> bool:
    return {"unit_price_cents", "catalog_price_cents"} <= event.payload.keys()


def _has_partial_catalog_price(event: Event) -> bool:
    return "unit_price_cents" in event.payload or "catalog_price_cents" in event.payload


def _require_operational_context(event: Event) -> None:
    """Reject demo-only assumptions that are not present in a live event or twin."""
    required: tuple[str, ...] = ()
    if event.type is EventType.SCAN:
        required = (
            "payday_multiplier",
            "cold_chain_area",
            "cold_chain_outage_hours",
            "cold_chain_average_temp_c",
        )
    elif event.type is EventType.COLD_CHAIN_ALERT:
        required = ("temp_c",)
    missing = [field for field in required if event.payload.get(field) is None]
    if missing:
        raise MissingOperationalFacts(missing)
