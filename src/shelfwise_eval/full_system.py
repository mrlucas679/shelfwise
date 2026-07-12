"""Receipt-driven full-system world simulation for ShelfWise.

The driver exercises the public FastAPI surface and the real in-process connector,
event-bus, worker, cascade, HITL, learning, write-back, and observability components.
It produces row-level evidence and treats integrity failures as process failures.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from shelfwise_contracts import Event, EventType
from shelfwise_runtime import durable_dir
from shelfwise_worldgen.scenarios import SCENARIOS, build
from shelfwise_worldgen.world import (
    EVENT_TYPE_ROUTES,
    assert_world_event_contract,
    span_event_stream,
)

from .autopilot import APPROVE, REJECT, resolution_receipt, review_decision

SCENARIO_ROTATION = (
    "golden_expiry",
    "critic_rejection",
    "procurement",
    "sales",
    "misprice",
    "cold_chain",
    "recall_quarantine",
    "inventory_exception",
    "connector_duplicate_invalid",
    "multimodal_review",
    "auth_tenant_isolation",
    "worker_retry_dlq",
    "hitl_approval_rejection",
    "writeback",
    "learning",
)

SUPPORT_FEATURES = (
    "world_event_stream",
    "event_bus",
    "tools_agents",
    "chat_inference",
    "observability",
)

REQUIRED_FEATURE_RECEIPTS = frozenset((*SCENARIO_ROTATION, *SUPPORT_FEATURES))

REQUIRED_ROUTE_RECEIPTS = frozenset(
    {
        "POST /ingest",
        "POST /worker/process-one",
        "POST /demo/golden",
        "POST /demo/critic-rejection",
        "POST /demo/procurement",
        "POST /demo/sales",
        "POST /demo/cold-chain",
        "POST /demo/recall",
        "POST /demo/inventory-exception",
        "POST /connectors/square/intake",
        "POST /connectors/shopify/intake",
        "POST /scan/barcode",
        "GET /decisions/{decision_id}",
        "POST /decisions/{decision_id}/approve",
        "POST /decisions/{decision_id}/reject",
        "GET /writeback/tasks",
        "GET /learning",
        "GET /tools/platform",
        "POST /intelligence/stock/fefo-split",
        "POST /chat",
        "GET /mlops/model-runs",
        "GET /mlops/observability",
    }
)

_GOLDEN_AGENTS = {
    "inventory",
    "demand",
    "expiry",
    "opportunity",
    "simulation",
    "critic",
    "executive",
}
_DOMAIN_AGENTS = {*_GOLDEN_AGENTS, "sales", "procurement", "cold_chain"}
_OFFLINE_MARKERS = (
    "Current ShelfWise state:",
    "The current recommendation is",
    "ShelfWise is tracking",
)


@dataclass(frozen=True, slots=True)
class FullSystemConfig:
    """Bound one full-system run without weakening its minimum coverage."""

    base_seed: int = 20_260_710
    world_scenario_ids: tuple[str, ...] = field(default_factory=lambda: tuple(SCENARIOS))
    world_cycles: int = field(default_factory=lambda: len(SCENARIOS))
    duration_seconds: float | None = None
    assortment_sizes: tuple[int | None, ...] = (None,)
    catalog_scales: tuple[str, ...] = ("supermarket",)
    event_limit: int = 80
    chat_every_n_cycles: int = 1
    live_required: bool = False
    reset_state: bool = True
    run_id: str = ""
    artifact_dir: Path | str | None = None

    def __post_init__(self) -> None:
        unknown = set(self.world_scenario_ids) - set(SCENARIOS)
        if unknown:
            raise ValueError(f"unknown world scenarios: {sorted(unknown)}")
        if not self.world_scenario_ids:
            raise ValueError("at least one world scenario is required")
        if self.world_cycles < len(self.world_scenario_ids):
            raise ValueError("world_cycles must complete at least one scenario rotation")
        if self.duration_seconds is not None and self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")
        if self.event_limit < len(EventType):
            raise ValueError(f"event_limit must be at least {len(EventType)}")
        if self.chat_every_n_cycles <= 0:
            raise ValueError("chat_every_n_cycles must be positive")
        if not self.assortment_sizes:
            raise ValueError("at least one assortment size is required")
        if any(size is not None and size <= 0 for size in self.assortment_sizes):
            raise ValueError("assortment sizes must be positive or None")
        if not self.catalog_scales or any(not scale.strip() for scale in self.catalog_scales):
            raise ValueError("catalog scales must be non-empty")
        if self.artifact_dir is not None:
            object.__setattr__(self, "artifact_dir", Path(self.artifact_dir))


@dataclass(frozen=True, slots=True)
class FeatureReceipt:
    feature: str
    passed: bool
    detail: str
    route: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature": self.feature,
            "passed": self.passed,
            "detail": self.detail,
            "route": self.route,
            "evidence": deepcopy(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class RouteReceipt:
    key: str
    feature: str
    status_code: int
    ok: bool
    request_index: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "feature": self.feature,
            "status_code": self.status_code,
            "ok": self.ok,
            "request_index": self.request_index,
        }


@dataclass(frozen=True, slots=True)
class FullSystemReport:
    run_id: str
    started_at: str
    finished_at: str
    config: dict[str, Any]
    totals: dict[str, Any]
    event_contract: dict[str, Any]
    feature_receipts: tuple[FeatureReceipt, ...]
    route_receipts: tuple[RouteReceipt, ...]
    decision_trail: tuple[dict[str, Any], ...]
    failures: tuple[str, ...]
    artifact_dir: str = ""

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def exit_code(self) -> int:
        return 0 if self.passed else 1

    def require_passed(self) -> None:
        """Raise so notebooks and scripts cannot print a false pass."""
        if self.failures:
            raise FullSystemFailure("; ".join(self.failures))

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "passed": self.passed,
            "exit_code": self.exit_code,
            "config": deepcopy(self.config),
            "totals": deepcopy(self.totals),
            "event_contract": deepcopy(self.event_contract),
            "required_features": sorted(REQUIRED_FEATURE_RECEIPTS),
            "required_routes": sorted(REQUIRED_ROUTE_RECEIPTS),
            "feature_receipts": [item.to_dict() for item in self.feature_receipts],
            "route_receipts": [item.to_dict() for item in self.route_receipts],
            "failures": list(self.failures),
            "artifact_dir": self.artifact_dir,
        }


class FullSystemFailure(RuntimeError):
    """Raised when a report is consumed as a required pass but contains failures."""


def audit_full_system_integrity(
    *,
    decision_trail: Sequence[Mapping[str, Any]],
    feature_receipts: Sequence[FeatureReceipt],
    route_receipts: Sequence[RouteReceipt],
    live_required: bool,
    chat_calls: int,
    chat_model_answers: int,
    chat_offline_answers: int = 0,
    chat_errors: int = 0,
) -> list[str]:
    """Return stable failure codes for every non-negotiable harness invariant."""
    failures: list[str] = []
    decision_ids = [str(row.get("decision_id") or "") for row in decision_trail]
    counts = Counter(item for item in decision_ids if item)
    for decision_id, count in sorted(counts.items()):
        if count > 1:
            failures.append(f"decision_reuse:{decision_id}:count={count}")

    for row in decision_trail:
        action = str(row.get("requested_action") or "")
        if action in {APPROVE, REJECT} and not bool(row.get("matched")):
            failures.append(
                "hitl_request_result_mismatch:"
                f"{row.get('decision_id') or 'missing'}:"
                f"{','.join(str(item) for item in row.get('mismatches') or ['unknown'])}"
            )
        if bool(row.get("learning_movement_expected")):
            try:
                delta = int(row.get("learning_delta") or 0)
            except (TypeError, ValueError):
                delta = 0
            if delta <= 0:
                failures.append(
                    f"learning_noop:{row.get('decision_id') or 'missing'}:delta={delta}"
                )

    feature_rows: dict[str, list[FeatureReceipt]] = {}
    for receipt in feature_receipts:
        feature_rows.setdefault(receipt.feature, []).append(receipt)
    for feature in sorted(REQUIRED_FEATURE_RECEIPTS):
        rows = feature_rows.get(feature, [])
        if not rows:
            failures.append(f"missing_feature_receipt:{feature}")
        elif not any(row.passed for row in rows):
            detail = rows[-1].detail if rows else "missing"
            failures.append(f"failed_feature_receipt:{feature}:{detail}")

    route_rows: dict[str, list[RouteReceipt]] = {}
    for receipt in route_receipts:
        route_rows.setdefault(receipt.key, []).append(receipt)
    for route in sorted(REQUIRED_ROUTE_RECEIPTS):
        rows = route_rows.get(route, [])
        if not rows:
            failures.append(f"missing_route_receipt:{route}")
        elif not any(row.ok for row in rows):
            failures.append(
                f"failed_route_receipt:{route}:status={rows[-1].status_code}"
            )

    if live_required:
        if chat_calls <= 0:
            failures.append("live_chat_calls_zero")
        if chat_model_answers != chat_calls:
            failures.append(
                f"live_model_answer_mismatch:model={chat_model_answers}:calls={chat_calls}"
            )
        if chat_offline_answers:
            failures.append(f"live_offline_answers:{chat_offline_answers}")
        if chat_errors:
            failures.append(f"live_chat_errors:{chat_errors}")
    return sorted(set(failures))


def run_full_system(config: FullSystemConfig | None = None) -> FullSystemReport:
    """Run the complete scenario rotation and return an auditable report."""
    effective = config or FullSystemConfig()
    runtime = _load_runtime()
    if effective.reset_state:
        _reset_in_memory_state(runtime)
    runtime.write_limiter.configure(capacity=1_000_000, refill_per_s=50_000.0, max_keys=4096)

    environment = {
        "WORKER_ENABLED": "false",
        "SHELFWISE_AUTH_MODE": "off",
        "SHELFWISE_TENANT_ID": "local",
    }
    with (
        _temporary_environment(environment),
        TestClient(runtime.app, raise_server_exceptions=False) as client,
    ):
        driver = _FullSystemDriver(config=effective, runtime=runtime, client=client)
        return driver.run()


class _FullSystemDriver:
    def __init__(self, *, config: FullSystemConfig, runtime: Any, client: TestClient) -> None:
        self.config = config
        self.runtime = runtime
        self.client = client
        self.tenant_id = "local"
        self.site_id = self.runtime.world_facts.get_scenario_facts(self.tenant_id).location
        self.run_id = config.run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.started_at = datetime.now(UTC).isoformat()
        self.features: list[FeatureReceipt] = []
        self.routes: list[RouteReceipt] = []
        self.trail: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []
        self.internal_failures: list[str] = []
        self.agents: set[str] = set()
        self.cascade_scenarios: Counter[str] = Counter()
        self.scenario_counts: Counter[str] = Counter()
        self.event_types: Counter[str] = Counter()
        self.world_consumers: set[EventType] = set()
        self.chat_samples: list[dict[str, Any]] = []
        self.learning_payload: dict[str, Any] = {}
        self.totals: Counter[str] = Counter()
        self.event_contract: dict[str, Any] = {}
        self._request_index = 0
        self._processed_decisions: set[str] = set()
        if config.artifact_dir:
            self._artifact_dir = Path(config.artifact_dir)
        elif os.getenv("SHELFWISE_PERSIST_ROOT", "").strip():
            self._artifact_dir = durable_dir("HARNESS_RUN_DIR", "harness/runs") / self.run_id
        else:
            self._artifact_dir = None
        self._trail_path = self._artifact_path("decision_trail.jsonl")
        self._cycles_path = self._artifact_path("cycles.jsonl")
        self._prepare_artifacts()

    def run(self) -> FullSystemReport:
        self._probe_worker_retry_dlq()
        self._drive_world_rotation()
        self._probe_demo_scenarios()
        self._probe_misprice()
        self._probe_connectors()
        self._probe_multimodal_review()
        self._probe_auth_tenant_isolation()
        self._probe_tools_and_agents()
        self._resolve_hitl()
        self._probe_writeback_and_learning()
        self._record_chat_feature()
        self._probe_observability()

        report = self._build_report()
        try:
            self._write_artifacts(report)
        except OSError as exc:
            self.internal_failures.append(f"artifact_write_failed:{type(exc).__name__}:{exc}")
            report = self._build_report()
        return report

    def _drive_world_rotation(self) -> None:
        deadline = (
            time.monotonic() + self.config.duration_seconds
            if self.config.duration_seconds is not None
            else None
        )
        cycle = 0
        contract_samples: list[dict[str, Any]] = []
        while cycle < self.config.world_cycles or (
            deadline is not None and time.monotonic() < deadline
        ):
            scenario_id = self.config.world_scenario_ids[
                cycle % len(self.config.world_scenario_ids)
            ]
            seed = self.config.base_seed + cycle
            size = self.config.assortment_sizes[cycle % len(self.config.assortment_sizes)]
            scale = self.config.catalog_scales[cycle % len(self.config.catalog_scales)]
            try:
                world, _schedule = build(
                    scenario_id,
                    seed_override=seed,
                    assortment_size=size,
                    catalog_scale=scale,
                    tenant_id=self.tenant_id,
                )
                stream = list(world.run())
                contract = assert_world_event_contract(stream)
                sample = span_event_stream(stream, self.config.event_limit)
                sampled_types = {event.type for event in sample}
                if sampled_types != set(EventType):
                    missing = sorted(item.value for item in set(EventType) - sampled_types)
                    raise AssertionError(f"spanning sample missing event types: {missing}")
                accepted = self._ingest_world_sample(
                    sample,
                    cycle=cycle,
                    scenario_id=scenario_id,
                    seed=seed,
                )
                self.totals["world_events_generated"] += len(stream)
                self.totals["world_events_submitted"] += len(sample)
                self.totals["world_events_accepted"] += accepted
                self.scenario_counts[scenario_id] += 1
                contract_samples.append(contract)
                self._append_cycle(
                    {
                        "cycle": cycle,
                        "scenario_id": scenario_id,
                        "seed": seed,
                        "assortment_size": size,
                        "catalog_scale": scale,
                        "events_generated": len(stream),
                        "events_submitted": len(sample),
                        "events_accepted": accepted,
                        "event_types": sorted(item.value for item in sampled_types),
                    }
                )
                if cycle % self.config.chat_every_n_cycles == 0:
                    product = world.products[seed % len(world.products)]
                    self._ask_chat(_product_question(product), cycle=cycle)
            except Exception as exc:
                self.internal_failures.append(
                    f"world_cycle_failed:{scenario_id}:{seed}:{type(exc).__name__}:{exc}"
                )
            cycle += 1

        expected_consumers = {
            event_type
            for event_type, route in EVENT_TYPE_ROUTES.items()
            if route.consumer is not None
        }
        missing_consumers = expected_consumers - self.world_consumers
        rotation_complete = set(self.config.world_scenario_ids) <= set(self.scenario_counts)
        passed = (
            bool(contract_samples)
            and not missing_consumers
            and rotation_complete
            and self.totals["world_events_accepted"] == self.totals["world_events_submitted"]
        )
        self.event_contract = {
            "routes": {
                event_type.value: {
                    "consumer": route.consumer,
                    "stored_only": route.stored_only,
                    "reason": route.reason,
                }
                for event_type, route in EVENT_TYPE_ROUTES.items()
            },
            "observed_types": sorted(self.event_types),
            "observed_consumers": sorted(item.value for item in self.world_consumers),
            "stored_only": sorted(
                event_type.value
                for event_type, route in EVENT_TYPE_ROUTES.items()
                if route.stored_only
            ),
        }
        self._feature(
            "world_event_stream",
            passed,
            (
                f"cycles={sum(self.scenario_counts.values())} "
                f"accepted={self.totals['world_events_accepted']} "
                f"missing_consumers={sorted(item.value for item in missing_consumers)}"
            ),
            route="POST /ingest",
            evidence=self.event_contract,
        )

    def _ingest_world_sample(
        self,
        sample: Sequence[Event],
        *,
        cycle: int,
        scenario_id: str,
        seed: int,
    ) -> int:
        accepted = 0
        for event in sample:
            self.event_types[event.type.value] += 1
            response = self._request(
                "world_event_stream",
                "POST",
                "/ingest",
                json=event.to_dict(),
            )
            body = _json_body(response)
            if response.status_code == 200 and body.get("status") == "accepted":
                accepted += 1
            elif body.get("status") == "duplicate":
                self.totals["event_duplicates"] += 1
            cascade = body.get("cascade") if isinstance(body.get("cascade"), dict) else None
            route = EVENT_TYPE_ROUTES[event.type]
            if route.stored_only and cascade is not None:
                self.internal_failures.append(
                    f"stored_only_event_consumed:{event.type.value}:{event.id}"
                )
            if route.consumer and cascade is not None:
                self.world_consumers.add(event.type)
            if cascade is not None:
                self._capture_cascade(
                    cascade,
                    source=f"world:{cycle}:{scenario_id}:{seed}:{event.id}",
                )
        return accepted

    def _probe_demo_scenarios(self) -> None:
        golden = self._demo(
            feature="golden_expiry",
            path="/demo/golden",
            scenario="stage4_loadshedding_x_payday_yoghurt",
            action="apply_markdown",
            statuses={"pending"},
        )
        expiry_seen = self.cascade_scenarios["expiry_risk_markdown_review"] > 0
        golden_agents = {
            str(item.get("agent"))
            for item in golden.get("evidence", [])
            if isinstance(item, dict)
        }
        self._feature(
            "golden_expiry",
            bool(golden) and expiry_seen and golden_agents >= _GOLDEN_AGENTS,
            f"golden_agents={sorted(golden_agents)} expiry_seen={expiry_seen}",
            route="POST /demo/golden",
        )

        critic = self._demo(
            feature="critic_rejection",
            path="/demo/critic-rejection",
            scenario="critic_rejects_unsupported_supplier_switch",
            action="monitor",
            statuses={"rejected"},
        )
        critic_decision = _decision_from(critic)
        self._feature(
            "critic_rejection",
            critic_decision.get("critic_verdict") == "rejected",
            (
                f"status={critic_decision.get('status')} "
                f"critic={critic_decision.get('critic_verdict')}"
            ),
            route="POST /demo/critic-rejection",
        )

        procurement = self._demo(
            feature="procurement",
            path="/demo/procurement",
            scenario="procurement_reorder_supplier_cover",
            action="reorder",
            statuses={"pending"},
        )
        self._feature(
            "procurement",
            bool(procurement),
            _cascade_detail(procurement),
            route="POST /demo/procurement",
        )

        sales = self._demo(
            feature="sales",
            path="/demo/sales",
            scenario="pos_sale_price_integrity",
            action="record_sale",
            statuses={"approved"},
        )
        self._feature(
            "sales",
            bool(sales),
            _cascade_detail(sales),
            route="POST /demo/sales",
        )

        cold_chain = self._demo(
            feature="cold_chain",
            path="/demo/cold-chain",
            scenario="cold_chain_generator_failure_facilities_review",
            action="dispatch_facilities_check",
            statuses={"pending"},
        )
        self._feature(
            "cold_chain",
            bool(cold_chain),
            _cascade_detail(cold_chain),
            route="POST /demo/cold-chain",
        )

        recall = self._demo(
            feature="recall_quarantine",
            path="/demo/recall",
            scenario="supplier_lot_recall_quarantine",
            action="quarantine_lot",
            statuses={"pending"},
        )
        self._feature(
            "recall_quarantine",
            bool(recall),
            _cascade_detail(recall),
            route="POST /demo/recall",
        )

        inventory_exception = self._demo(
            feature="inventory_exception",
            path="/demo/inventory-exception",
            scenario="inventory_exception_review",
            action="investigate_shrink",
            statuses={"pending"},
        )
        self._feature(
            "inventory_exception",
            bool(inventory_exception),
            _cascade_detail(inventory_exception),
            route="POST /demo/inventory-exception",
        )

    def _demo(
        self,
        *,
        feature: str,
        path: str,
        scenario: str,
        action: str,
        statuses: set[str],
    ) -> dict[str, Any]:
        response = self._request(feature, "POST", path)
        body = _json_body(response)
        decision = _decision_from(body)
        actual_action = _action_type(decision)
        valid = (
            response.status_code == 200
            and body.get("scenario") == scenario
            and actual_action == action
            and str(decision.get("status")) in statuses
        )
        if valid:
            self._capture_cascade(body, source=f"demo:{feature}")
            return body
        self.internal_failures.append(
            f"demo_contract_failed:{feature}:{_cascade_detail(body)}"
        )
        return {}

    def _probe_misprice(self) -> None:
        seed = self.config.base_seed + 900_000
        tenant_id = "local"
        scenario = self.runtime.world_facts.get_scenario_facts(tenant_id)
        observed_price = scenario.unit_price.amount / 2
        event = {
            "id": f"evt_full_misprice_{seed}",
            "type": "sale",
            "ts": "2026-07-10T10:14:00Z",
            "actor": scenario.location,
            "source": "pos_csv",
            "tenant_id": tenant_id,
            "correlation_id": f"full_misprice_{seed}",
            "payload": {
                "sku": scenario.sku,
                "units": 250,
                "unit_price_cents": int(observed_price * 100),
                "catalog_price_cents": scenario.unit_price.minor_units,
            },
        }
        response = self._request("misprice", "POST", "/ingest", json=event)
        body = _json_body(response)
        cascade = body.get("cascade") if isinstance(body.get("cascade"), dict) else {}
        decision = _decision_from(cascade)
        passed = (
            response.status_code == 200
            and cascade.get("scenario") == "pos_price_outlier_review"
            and decision.get("status") == "pending"
            and decision.get("critic_verdict") == "review_required"
        )
        if cascade:
            self._capture_cascade(cascade, source="probe:misprice_high_exposure")
        self._feature(
            "misprice",
            passed,
            _cascade_detail(cascade),
            route="POST /ingest",
        )

    def _probe_connectors(self) -> None:
        suffix = self.config.base_seed % 10_000
        square_payload = {
            "type": "inventory.count.updated",
            "data": {
                "object": {
                    "inventory_counts": [
                        {
                            "catalog_object_id": f"sq_full_{suffix}",
                            "location_id": "local-site",
                            "quantity": str(240 + suffix % 10),
                        }
                    ]
                }
            },
        }
        first = self._request(
            "connector_duplicate_invalid",
            "POST",
            "/connectors/square/intake",
            json={"payload": square_payload},
        )
        duplicate = self._request(
            "connector_duplicate_invalid",
            "POST",
            "/connectors/square/intake",
            json={"payload": square_payload},
        )
        invalid = self._request(
            "connector_duplicate_invalid",
            "POST",
            "/connectors/shopify/intake",
            json={
                "payload": {
                    "id": 700_000 + suffix,
                    "created_at": "2026-07-10T10:00:00Z",
                }
            },
        )
        first_body = _json_body(first)
        duplicate_body = _json_body(duplicate)
        invalid_body = _json_body(invalid)
        passed = (
            first.status_code == duplicate.status_code == invalid.status_code == 200
            and first_body.get("status") == "accepted"
            and duplicate_body.get("status") == "duplicate"
            and invalid_body.get("status") == "invalid"
            and invalid_body.get("event") is None
        )
        self._feature(
            "connector_duplicate_invalid",
            passed,
            (
                f"statuses={first_body.get('status')},"
                f"{duplicate_body.get('status')},{invalid_body.get('status')}"
            ),
            route="POST /connectors/{system}/intake",
        )

    def _probe_multimodal_review(self) -> None:
        response = self._request(
            "multimodal_review",
            "POST",
            "/scan/barcode",
            json={
                "code": "SKU-local-probe",
                "location": "local-site",
            },
        )
        body = _json_body(response)
        candidate = body.get("candidate") if isinstance(body.get("candidate"), dict) else {}
        event = candidate.get("event") if isinstance(candidate.get("event"), dict) else {}
        passed = (
            response.status_code == 200
            and body.get("requires_human_review") is True
            and event.get("type") == "scan"
            and event.get("payload", {}).get("sku") == "local-probe"
        )
        self._feature(
            "multimodal_review",
            passed,
            f"review={body.get('requires_human_review')} type={event.get('type')}",
            route="POST /scan/barcode",
        )

    def _probe_auth_tenant_isolation(self) -> None:
        from shelfwise_backend.tenant import encode_hs256_token

        secret = f"full-system-{self.config.base_seed}"
        expires = int(time.time()) + 3_600

        def token(tenant_id: str) -> str:
            return encode_hs256_token(
                {
                    "tenant_id": tenant_id,
                    "user_id": "full_system_manager",
                    "role": "manager",
                    "exp": expires,
                },
                secret=secret,
            )

        owner_headers = {"Authorization": f"Bearer {token('local')}"}
        other_headers = {"Authorization": f"Bearer {token('other_tenant')}"}
        event_id = f"evt_full_auth_{self.config.base_seed}"
        with _temporary_environment(
            {"SHELFWISE_AUTH_MODE": "jwt", "TENANT_AUTH_SECRET": secret}
        ):
            created = self._request(
                "auth_tenant_isolation",
                "POST",
                "/ingest",
                headers=owner_headers,
                json={
                    "id": event_id,
                    "type": "scan",
                    "ts": "2026-07-10T10:14:00Z",
                    "actor": self.site_id,
                    "source": "scanner",
                    "tenant_id": "local",
                    "payload": {"sku": "local-probe", "location": "local-site"},
                },
            )
            created_body = _json_body(created)
            cascade = (
                created_body.get("cascade")
                if isinstance(created_body.get("cascade"), dict)
                else {}
            )
            decision_id = str(_decision_from(cascade).get("id") or "missing")
            owner = self._request(
                "auth_tenant_isolation",
                "GET",
                f"/decisions/{decision_id}",
                route_key="GET /decisions/{decision_id}",
                headers=owner_headers,
            )
            cross_read = self._request(
                "auth_tenant_isolation",
                "GET",
                f"/decisions/{decision_id}",
                route_key="GET /decisions/{decision_id}",
                expected={404},
                headers=other_headers,
            )
            cross_approve = self._request(
                "auth_tenant_isolation",
                "POST",
                f"/decisions/{decision_id}/approve",
                route_key="POST /decisions/{decision_id}/approve",
                expected={404},
                headers=other_headers,
            )
            cross_reject = self._request(
                "auth_tenant_isolation",
                "POST",
                f"/decisions/{decision_id}/reject",
                route_key="POST /decisions/{decision_id}/reject",
                expected={404},
                headers=other_headers,
            )
        if cascade:
            self._capture_cascade(cascade, source="probe:auth_owner")
        passed = (
            created.status_code == 200
            and owner.status_code == 200
            and cross_read.status_code == 404
            and cross_approve.status_code == 404
            and cross_reject.status_code == 404
        )
        self._feature(
            "auth_tenant_isolation",
            passed,
            (
                f"created={created.status_code} owner={owner.status_code} "
                f"cross={cross_read.status_code}/{cross_approve.status_code}/"
                f"{cross_reject.status_code}"
            ),
            route="GET|POST /decisions/{decision_id}",
        )

    def _probe_worker_retry_dlq(self) -> None:
        from shelfwise_action import InMemoryDecisionStore
        from shelfwise_backend.event_bus import InMemoryEventBus
        from shelfwise_backend.worker import CascadeWorker, InMemoryJournal

        event_id = f"evt_full_worker_{self.config.base_seed}"
        ingest = self._request(
            "worker_retry_dlq",
            "POST",
            "/ingest",
            json={
                "id": event_id,
                "type": "stock_update",
                "ts": "2026-07-10T08:00:00Z",
                "actor": self.site_id,
                "source": "wms_csv",
                "tenant_id": "local",
                "payload": {"sku": "WORKER-PROBE", "on_hand": 10},
            },
        )
        processed = self._request(
            "worker_retry_dlq", "POST", "/worker/process-one"
        )
        processed_body = _json_body(processed).get("result") or {}

        bus = InMemoryEventBus(max_retries=2)
        journal = InMemoryJournal()
        failed_event = Event.parse_wire(
            {
                "id": f"evt_full_dlq_{self.config.base_seed}",
                "type": "scan",
                "ts": "2026-07-10T10:14:00Z",
                "actor": self.site_id,
                "source": "scanner",
                "tenant_id": "local",
                "payload": {"sku": "local-probe", "location": "local-site"},
            }
        )
        bus.publish(failed_event)

        def fail_handler(_event: Event) -> dict[str, Any]:
            raise RuntimeError("forced full-system worker failure")

        worker = CascadeWorker(
            bus=bus,
            journal=journal,
            decision_store=InMemoryDecisionStore(),
            handler=fail_handler,
        )
        first = worker.process_one()
        second = worker.process_one()
        dead = bus.dead_letter()
        passed = (
            ingest.status_code == 200
            and processed.status_code == 200
            and processed_body.get("status") == "done"
            and first.status == "failed"
            and first.dead_lettered is False
            and second.status == "failed"
            and second.dead_lettered is True
            and len(dead) == 1
            and dead[0].get("event", {}).get("id") == failed_event.id
        )
        self._feature(
            "worker_retry_dlq",
            passed,
            (
                f"app={processed_body.get('status')} retry={first.dead_lettered} "
                f"dlq={second.dead_lettered} dead={len(dead)}"
            ),
            route="POST /worker/process-one",
        )

    def _probe_tools_and_agents(self) -> None:
        catalog = self._request("tools_agents", "GET", "/tools/platform")
        tools_body = _json_body(catalog)
        tools = tools_body.get("tools") if isinstance(tools_body.get("tools"), list) else []
        fefo = self._request(
            "tools_agents",
            "POST",
            "/intelligence/stock/fefo-split",
            json={
                "sku": "milk_2l",
                "as_of": "2026-07-10",
                "batches": [
                    {
                        "sku": "milk_2l",
                        "lot": "MILK-OLD",
                        "units": 10,
                        "expiry_date": "2026-07-11",
                        "received_date": "2026-07-08",
                        "location": "fridge_a",
                    },
                    {
                        "sku": "milk_2l",
                        "lot": "MILK-NEW",
                        "units": 20,
                        "expiry_date": "2026-07-18",
                        "received_date": "2026-07-10",
                        "location": "fridge_a",
                    },
                ],
            },
        )
        split = _json_body(fefo).get("batch_split") or {}
        passed = (
            catalog.status_code == 200
            and bool(tools)
            and all(item.get("read_only") is True for item in tools if isinstance(item, dict))
            and fefo.status_code == 200
            and split.get("total_units") == 30
            and self.agents >= _DOMAIN_AGENTS
        )
        self._feature(
            "tools_agents",
            passed,
            (
                f"tools={len(tools)} agents={sorted(self.agents)} "
                f"total_units={split.get('total_units')}"
            ),
            route="GET /tools/platform; POST /intelligence/stock/fefo-split",
        )

    def _resolve_hitl(self) -> None:
        approvals = 0
        rejections = 0
        for captured in self.decisions:
            decision = captured["decision"]
            source = str(captured["source"])
            decision_id = str(decision.get("id") or "")
            if decision_id in self._processed_decisions:
                self._append_trail(
                    {
                        "decision_id": decision_id,
                        "source": source,
                        "initial_status": decision.get("status"),
                        "requested_action": "duplicate",
                        "matched": False,
                        "mismatches": ["decision id already observed in this run"],
                        "ts": datetime.now(UTC).isoformat(),
                    }
                )
                continue
            self._processed_decisions.add(decision_id)
            verdict = review_decision(decision)
            action = str(verdict.get("action") or "")
            if action not in {APPROVE, REJECT}:
                self._append_trail(
                    {
                        "decision_id": decision_id,
                        "source": source,
                        "initial_status": decision.get("status"),
                        "requested_action": action,
                        "returned_decision_id": decision_id,
                        "returned_status": decision.get("status"),
                        "matched": True,
                        "mismatches": [],
                        "autopilot": verdict,
                        "learning_movement_expected": False,
                        "learning_delta": 0,
                        "ts": datetime.now(UTC).isoformat(),
                    }
                )
                continue

            response = self._request(
                "hitl_approval_rejection",
                "POST",
                f"/decisions/{decision_id}/{action}",
                route_key=f"POST /decisions/{{decision_id}}/{action}",
            )
            body = _json_body(response)
            receipt = resolution_receipt(
                decision_id=decision_id,
                verdict=verdict,
                status_code=response.status_code,
                payload=body,
            )
            learning = (
                body.get("learning_event")
                if isinstance(body.get("learning_event"), dict)
                else {}
            )
            movement_expected = _learning_movement_expected(decision, learning)
            row = {
                "decision_id": decision_id,
                "source": source,
                "initial_status": decision.get("status"),
                "critic_verdict": decision.get("critic_verdict"),
                "action_type": _action_type(decision),
                "autopilot": verdict,
                **receipt,
                "learning_event_id": learning.get("id"),
                "learning_metric": learning.get("metric"),
                "learning_previous": learning.get("previous_threshold"),
                "learning_updated": learning.get("updated_threshold"),
                "learning_delta": learning.get("delta_units", 0),
                "learning_movement_expected": movement_expected,
                "ts": datetime.now(UTC).isoformat(),
            }
            self._append_trail(row)
            if receipt["matched"] and action == APPROVE:
                approvals += 1
            if receipt["matched"] and action == REJECT:
                rejections += 1

        self.totals["approved"] = approvals
        self.totals["rejected"] = rejections
        mismatches = sum(
            1
            for row in self.trail
            if row.get("requested_action") in {APPROVE, REJECT} and not row.get("matched")
        )
        self.totals["hitl_mismatches"] = mismatches
        self._feature(
            "hitl_approval_rejection",
            approvals > 0 and rejections > 0 and mismatches == 0,
            f"approved={approvals} rejected={rejections} mismatches={mismatches}",
            route="POST /decisions/{decision_id}/{approve|reject}",
        )

    def _probe_writeback_and_learning(self) -> None:
        tasks_response = self._request("writeback", "GET", "/writeback/tasks")
        tasks_body = _json_body(tasks_response)
        tasks = tasks_body.get("tasks") if isinstance(tasks_body.get("tasks"), list) else []
        writeback_ok = (
            tasks_response.status_code == 200
            and self.totals["approved"] > 0
            and len(tasks) >= self.totals["approved"]
            and all(task.get("status") == "pending_external_write" for task in tasks)
        )
        self._feature(
            "writeback",
            writeback_ok,
            f"tasks={len(tasks)} approvals={self.totals['approved']}",
            route="GET /writeback/tasks",
        )

        learning_response = self._request("learning", "GET", "/learning")
        self.learning_payload = _json_body(learning_response)
        events = (
            self.learning_payload.get("events")
            if isinstance(self.learning_payload.get("events"), list)
            else []
        )
        expected_rows = [row for row in self.trail if row.get("learning_movement_expected")]
        moved_rows = [row for row in expected_rows if int(row.get("learning_delta") or 0) > 0]
        self.totals["learning_events"] = len(events)
        self.totals["learning_movements_expected"] = len(expected_rows)
        self.totals["learning_movements"] = len(moved_rows)
        learning_ok = (
            learning_response.status_code == 200
            and bool(events)
            and bool(expected_rows)
            and len(moved_rows) == len(expected_rows)
        )
        self._feature(
            "learning",
            learning_ok,
            (
                f"events={len(events)} movement={len(moved_rows)}/"
                f"{len(expected_rows)}"
            ),
            route="GET /learning",
        )

    def _ask_chat(self, question: str, *, cycle: int) -> None:
        before = {run.id for run in self.runtime.model_run_registry.list()}
        started = time.monotonic()
        response = self._request(
            "chat_inference",
            "POST",
            "/chat",
            json={
                "question": question,
                "live_required": self.config.live_required,
                # Synthetic world questions must not accumulate a shared conversation
                # transcript and distort later prompt size or latency measurements.
                "conversation_id": f"full-system-{self.run_id}-{cycle}",
                "message_id": f"full-system-message-{cycle}",
            },
        )
        latency_ms = round((time.monotonic() - started) * 1_000, 1)
        after = [run for run in self.runtime.model_run_registry.list() if run.id not in before]
        model_answer = any(
            run.status == "ok" and run.provider != "offline" for run in after
        ) and not _looks_offline(response.text)
        self.totals["chat_calls"] += 1
        if response.status_code != 200:
            self.totals["chat_errors"] += 1
        elif model_answer:
            self.totals["chat_model_answers"] += 1
        else:
            self.totals["chat_offline_answers"] += 1
        if len(self.chat_samples) < 20:
            self.chat_samples.append(
                {
                    "cycle": cycle,
                    "question": question,
                    "answer": response.text[:500],
                    "latency_ms": latency_ms,
                    "model_answer": model_answer,
                    "model_run_ids": [run.id for run in after],
                    "model_run_errors": [
                        run.error_detail for run in after if run.error_detail
                    ],
                }
            )

    def _record_chat_feature(self) -> None:
        runs = self._request("chat_inference", "GET", "/mlops/model-runs")
        passed = (
            self.totals["chat_calls"] > 0
            and self.totals["chat_errors"] == 0
            and runs.status_code == 200
            and (
                not self.config.live_required
                or (
                    self.totals["chat_model_answers"] == self.totals["chat_calls"]
                    and self.totals["chat_offline_answers"] == 0
                )
            )
        )
        self._feature(
            "chat_inference",
            passed,
            (
                f"calls={self.totals['chat_calls']} model={self.totals['chat_model_answers']} "
                f"offline={self.totals['chat_offline_answers']} errors={self.totals['chat_errors']}"
            ),
            route="POST /chat; GET /mlops/model-runs",
        )

    def _probe_observability(self) -> None:
        response = self._request(
            "observability", "GET", "/mlops/observability", params={"limit": 500}
        )
        snapshot = _json_body(response).get("snapshot") or {}
        sections = {
            "decisions",
            "inference",
            "connectors",
            "events",
            "writeback",
            "worker",
            "learning",
        }
        passed = response.status_code == 200 and sections <= set(snapshot)
        self._feature(
            "observability",
            passed,
            f"sections={sorted(snapshot)}",
            route="GET /mlops/observability",
        )
        bus = snapshot.get("events", {}).get("bus", {}) if isinstance(snapshot, dict) else {}
        bus_ok = int(bus.get("messages_total") or 0) > 0 and bool(bus.get("streams"))
        self._feature(
            "event_bus",
            bus_ok,
            (
                f"backend={bus.get('backend')} messages={bus.get('messages_total')} "
                f"streams={bus.get('streams')}"
            ),
            route="GET /mlops/observability",
        )

    def _capture_cascade(self, cascade: dict[str, Any], *, source: str) -> None:
        scenario = str(cascade.get("scenario") or "")
        if scenario:
            self.cascade_scenarios[scenario] += 1
        evidence = cascade.get("evidence") if isinstance(cascade.get("evidence"), list) else []
        self.agents.update(
            str(item.get("agent"))
            for item in evidence
            if isinstance(item, dict) and item.get("agent")
        )
        decision = _decision_from(cascade)
        if decision:
            self.decisions.append({"decision": deepcopy(decision), "source": source})

    def _request(
        self,
        feature: str,
        method: str,
        path: str,
        *,
        route_key: str | None = None,
        expected: set[int] | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> Any:
        request_headers = _write_headers()
        request_headers.update(headers or {})
        response = self.client.request(method, path, headers=request_headers, **kwargs)
        self._request_index += 1
        accepted = expected or {200}
        key = route_key or f"{method.upper()} {path}"
        self.routes.append(
            RouteReceipt(
                key=key,
                feature=feature,
                status_code=response.status_code,
                ok=response.status_code in accepted,
                request_index=self._request_index,
            )
        )
        return response

    def _feature(
        self,
        feature: str,
        passed: bool,
        detail: str,
        *,
        route: str = "",
        evidence: dict[str, Any] | None = None,
    ) -> None:
        self.features.append(
            FeatureReceipt(
                feature=feature,
                passed=bool(passed),
                detail=detail,
                route=route,
                evidence=evidence or {},
            )
        )

    def _append_trail(self, row: dict[str, Any]) -> None:
        self.trail.append(deepcopy(row))
        if self._trail_path is not None:
            with self._trail_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")

    def _append_cycle(self, row: dict[str, Any]) -> None:
        if self._cycles_path is not None:
            with self._cycles_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")

    def _build_report(self) -> FullSystemReport:
        self.totals["world_cycles"] = sum(self.scenario_counts.values())
        self.totals["decisions_total"] = len(self.decisions)
        self.totals["unique_decision_ids"] = len(
            {str(item["decision"].get("id") or "") for item in self.decisions}
        )
        self.totals["feature_receipts"] = len(self.features)
        self.totals["route_receipts"] = len(self.routes)
        audited = audit_full_system_integrity(
            decision_trail=self.trail,
            feature_receipts=self.features,
            route_receipts=self.routes,
            live_required=self.config.live_required,
            chat_calls=self.totals["chat_calls"],
            chat_model_answers=self.totals["chat_model_answers"],
            chat_offline_answers=self.totals["chat_offline_answers"],
            chat_errors=self.totals["chat_errors"],
        )
        failures = tuple(sorted(set((*self.internal_failures, *audited))))
        return FullSystemReport(
            run_id=self.run_id,
            started_at=self.started_at,
            finished_at=datetime.now(UTC).isoformat(),
            config=_public_config(self.config),
            totals=dict(sorted(self.totals.items())),
            event_contract=deepcopy(self.event_contract),
            feature_receipts=tuple(self.features),
            route_receipts=tuple(self.routes),
            decision_trail=tuple(deepcopy(self.trail)),
            failures=failures,
            artifact_dir=str(self._artifact_dir or ""),
        )

    def _prepare_artifacts(self) -> None:
        if self._artifact_dir is None:
            return
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        for path in (self._trail_path, self._cycles_path):
            if path is not None:
                path.write_text("", encoding="utf-8")

    def _write_artifacts(self, report: FullSystemReport) -> None:
        if self._artifact_dir is None:
            return
        _write_json(self._artifact_dir / "manifest.json", report.to_dict())
        _write_json(
            self._artifact_dir / "feature_receipts.json",
            [item.to_dict() for item in self.features],
        )
        _write_json(
            self._artifact_dir / "route_receipts.json",
            [item.to_dict() for item in self.routes],
        )
        _write_json(self._artifact_dir / "learning_events.json", self.learning_payload)
        _write_json(self._artifact_dir / "chat_samples.json", self.chat_samples)

    def _artifact_path(self, name: str) -> Path | None:
        return self._artifact_dir / name if self._artifact_dir is not None else None


def _learning_movement_expected(
    decision: Mapping[str, Any], learning: Mapping[str, Any]
) -> bool:
    if not learning:
        return False
    try:
        previous = int(learning.get("previous_threshold") or 0)
    except (TypeError, ValueError):
        previous = 0
    target = _learning_target(decision)
    return target is not None and target > previous


def _learning_target(decision: Mapping[str, Any]) -> int | None:
    action = decision.get("action") if isinstance(decision.get("action"), Mapping) else {}
    action_type = str(action.get("type") or "")
    expected = (
        decision.get("expected_outcome")
        if isinstance(decision.get("expected_outcome"), Mapping)
        else {}
    )
    if action_type == "apply_markdown":
        predicted = _int(expected.get("predicted_sell_through_units"))
        if predicted <= 0:
            return None
        uplift = max(
            1,
            int((Decimal(predicted) * Decimal("0.12")).to_integral_value()),
        )
        return predicted + uplift
    if action_type == "review_price_exception":
        return abs(_int(expected.get("revenue_exposure_minor_units")))
    if action_type == "dispatch_facilities_check":
        return abs(
            _int(
                expected.get("stock_at_risk_minor_units")
                or expected.get("incremental_profit_minor_units")
            )
        )
    if action_type == "review_expiry_markdown":
        return max(_int(expected.get("days_to_expiry")), 1)
    return None


def _load_runtime() -> Any:
    from shelfwise_backend.app import (
        app,
        cold_chain_demo,
        decision_store,
        event_bus,
        event_store,
        inbound_record_store,
        journal,
        learning_store,
        model_run_registry,
        product_catalog_store,
        prompt_registry,
        tenant_fact_store,
        tenant_profile_store,
        tool_audit,
        trace_registry,
        world_facts,
        worldgen_run_store,
        write_limiter,
        writeback_sink,
    )

    return SimpleNamespace(
        app=app,
        cold_chain_demo=cold_chain_demo,
        decision_store=decision_store,
        event_bus=event_bus,
        event_store=event_store,
        inbound_record_store=inbound_record_store,
        journal=journal,
        learning_store=learning_store,
        model_run_registry=model_run_registry,
        product_catalog_store=product_catalog_store,
        prompt_registry=prompt_registry,
        tenant_fact_store=tenant_fact_store,
        tenant_profile_store=tenant_profile_store,
        tool_audit=tool_audit,
        trace_registry=trace_registry,
        worldgen_run_store=worldgen_run_store,
        write_limiter=write_limiter,
        writeback_sink=writeback_sink,
        world_facts=world_facts,
    )


def _reset_in_memory_state(runtime: Any) -> None:
    if os.getenv("SHELFWISE_STORE_BACKEND", "memory").strip().lower() != "memory":
        return
    for store in (
        runtime.decision_store,
        runtime.learning_store,
        runtime.event_store,
        runtime.inbound_record_store,
        runtime.product_catalog_store,
        runtime.journal,
        runtime.trace_registry,
        runtime.tool_audit,
        runtime.model_run_registry,
        runtime.prompt_registry,
        runtime.tenant_fact_store,
        runtime.tenant_profile_store,
        runtime.writeback_sink,
        runtime.worldgen_run_store,
        runtime.cold_chain_demo,
    ):
        clear = getattr(store, "clear", None)
        if callable(clear):
            clear()
    if os.getenv("SHELFWISE_BUS_BACKEND", "memory").strip().lower() == "memory":
        runtime.event_bus.clear()


@contextmanager
def _temporary_environment(values: Mapping[str, str]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _write_headers() -> dict[str, str]:
    api_key = os.getenv("API_KEY", "")
    return {"x-api-key": api_key} if api_key else {}


def _json_body(response: Any) -> dict[str, Any]:
    try:
        body = response.json()
    except (TypeError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


def _decision_from(cascade: Mapping[str, Any]) -> dict[str, Any]:
    decision = cascade.get("decision")
    return decision if isinstance(decision, dict) else {}


def _action_type(decision: Mapping[str, Any]) -> str:
    action = decision.get("action") if isinstance(decision.get("action"), Mapping) else {}
    return str(action.get("type") or "")


def _cascade_detail(cascade: Mapping[str, Any]) -> str:
    decision = _decision_from(cascade)
    return (
        f"scenario={cascade.get('scenario')} status={decision.get('status')} "
        f"action={_action_type(decision)}"
    )


def _product_question(product: object) -> str:
    name = str(getattr(product, "name", getattr(product, "generic_name", "this product")))
    category = str(
        getattr(product, "department", getattr(product, "category", "its category"))
    )
    return f"Why might {name} in {category} need attention right now?"


def _looks_offline(answer: str) -> bool:
    return any(answer.startswith(marker) for marker in _OFFLINE_MARKERS)


def _int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _public_config(config: FullSystemConfig) -> dict[str, Any]:
    return {
        "base_seed": config.base_seed,
        "world_scenario_ids": list(config.world_scenario_ids),
        "world_cycles": config.world_cycles,
        "duration_seconds": config.duration_seconds,
        "assortment_sizes": list(config.assortment_sizes),
        "catalog_scales": list(config.catalog_scales),
        "event_limit": config.event_limit,
        "chat_every_n_cycles": config.chat_every_n_cycles,
        "live_required": config.live_required,
        "reset_state": config.reset_state,
    }


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )


def _parse_assortment_sizes(value: str) -> tuple[int | None, ...]:
    sizes: list[int | None] = []
    for raw in value.split(","):
        item = raw.strip().lower()
        sizes.append(None if item in {"", "default", "none"} else int(item))
    return tuple(sizes)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the ShelfWise full-system world driver.")
    parser.add_argument("--base-seed", type=int, default=20_260_710)
    parser.add_argument("--world-cycles", type=int, default=len(SCENARIOS))
    parser.add_argument("--duration-seconds", type=float)
    parser.add_argument("--event-limit", type=int, default=80)
    parser.add_argument("--assortment-sizes", default="default")
    parser.add_argument("--catalog-scales", default="supermarket")
    parser.add_argument("--chat-every", type=int, default=1)
    parser.add_argument("--live-required", action="store_true")
    parser.add_argument("--no-reset", action="store_true")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        report = run_full_system(
            FullSystemConfig(
                base_seed=args.base_seed,
                world_cycles=args.world_cycles,
                duration_seconds=args.duration_seconds,
                assortment_sizes=_parse_assortment_sizes(args.assortment_sizes),
                catalog_scales=tuple(
                    item.strip() for item in args.catalog_scales.split(",") if item.strip()
                ),
                event_limit=args.event_limit,
                chat_every_n_cycles=args.chat_every,
                live_required=args.live_required,
                reset_state=not args.no_reset,
                run_id=args.run_id,
                artifact_dir=args.output_dir,
            )
        )
    except Exception as exc:
        print(f"SHELFWISE FULL SYSTEM ERROR: {type(exc).__name__}: {exc}")
        return 2

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        status = "PASS" if report.passed else "FAIL"
        print(
            f"SHELFWISE FULL SYSTEM {status}: "
            f"{report.totals.get('feature_receipts', 0)} feature receipts, "
            f"{report.totals.get('route_receipts', 0)} route receipts"
        )
        for failure in report.failures:
            print(f"FAIL {failure}")
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "REQUIRED_FEATURE_RECEIPTS",
    "REQUIRED_ROUTE_RECEIPTS",
    "SCENARIO_ROTATION",
    "FeatureReceipt",
    "FullSystemConfig",
    "FullSystemFailure",
    "FullSystemReport",
    "RouteReceipt",
    "audit_full_system_integrity",
    "main",
    "run_full_system",
]
