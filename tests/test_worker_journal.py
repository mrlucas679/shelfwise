from __future__ import annotations

import asyncio
from dataclasses import replace

from fastapi.testclient import TestClient

from shelfwise_action import InMemoryDecisionStore
from shelfwise_backend.app import app
from shelfwise_backend.event_bus import InMemoryEventBus
from shelfwise_backend.worker import (
    Capability,
    CapabilityRegistry,
    CascadeWorker,
    InMemoryJournal,
    Plan,
    PlanRunner,
    PlanStep,
    Schedule,
    Scheduler,
    Turn,
    WorkerLoopService,
    compact,
    journaled,
    validate_plan,
    worker_enabled,
)
from shelfwise_contracts import Event
from shelfwise_runtime import DataDomain


def _event(event_id: str = "evt_worker_4011") -> Event:
    return Event.parse_wire(
        {
            "id": event_id,
            "type": "scan",
            "ts": "2026-07-06T10:14:00Z",
            "actor": "store_12",
            "source": "scanner",
            "tenant_id": "sa_retail_demo",
            "data_domain": "world_simulation",
            "payload": {"sku": "4011", "location": "store_12"},
        }
    )


def _cold_chain_event(event_id: str = "evt_worker_cold_chain") -> Event:
    return Event.parse_wire(
        {
            "id": event_id,
            "type": "cold_chain_alert",
            "ts": "2026-07-06T10:14:00Z",
            "actor": "store_12",
            "source": "api",
            "tenant_id": "sa_retail_demo",
            "data_domain": "world_simulation",
            "payload": {
                "site_id": "store_12",
                "asset_id": "fridge_dairy_1",
                "category": "dairy",
                "diagnosis": "generator_failed",
                "severity": 2,
                "predicted_minutes_to_unsafe": "18",
                "measured_outage_hours": "4",
                "stock_at_risk": {"minor_units": 643500, "currency": "ZAR"},
            },
        }
    )


def test_journaled_replays_completed_step_without_rerunning() -> None:
    journal = InMemoryJournal()
    calls = 0

    def step() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"ok": True, "calls": calls}

    first = journaled(journal, "run_1", "agent:inventory", step)
    second = journaled(journal, "run_1", "agent:inventory", step)

    assert first == {"ok": True, "calls": 1}
    assert second == first
    assert calls == 1


def test_worker_processes_one_bus_event_and_records_done_run() -> None:
    client = TestClient(app)
    ingest = client.post(
        "/ingest",
        json={
            "id": "evt_worker_route",
            "type": "scan",
            "ts": "2026-07-06T10:14:00Z",
            "actor": "store_12",
                "source": "scanner",
                "tenant_id": "sa_retail_demo",
                "data_domain": "world_simulation",
            "payload": {"sku": "4011", "location": "store_12"},
        },
    )

    processed = client.post("/worker/process-one")
    runs = client.get("/worker/runs")
    idle = client.post("/worker/process-one")

    assert ingest.status_code == 200
    assert processed.status_code == 200
    result = processed.json()["result"]
    assert result["status"] == "done"
    assert result["run_id"] == (
        "event:sa_retail_demo:world_simulation:evt_worker_route"
    )
    assert result["cascade"]["decision"]["action"]["type"] == "apply_markdown"
    assert runs.status_code == 200
    assert runs.json()["runs"][0]["status"] == "done"
    assert idle.json()["result"]["status"] == "idle"


def test_named_deployment_worker_route_requires_internal_credential(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("SHELFWISE_WORKER_API_KEY", raising=False)

    response = TestClient(app).post("/worker/process-one")

    assert response.status_code == 503
    assert response.json()["detail"] == "Worker control plane is unavailable"


def test_worker_retries_failed_event_then_dead_letters_it_without_ever_acking() -> None:
    bus = InMemoryEventBus(max_retries=2)
    journal = InMemoryJournal()
    decisions = InMemoryDecisionStore()
    event = _event("evt_worker_failure")
    bus.publish(event)
    worker = CascadeWorker(
        bus=bus,
        journal=journal,
        decision_store=decisions,
        handler=lambda _: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    first = worker.process_one()
    second = worker.process_one()
    idle = worker.process_one()

    assert first.status == "failed"
    assert first.error == "boom"
    assert first.dead_lettered is False
    assert second.status == "failed"
    assert second.dead_lettered is True
    assert bus.dead_letter()[0]["event"]["id"] == "evt_worker_failure"
    assert idle.status == "idle"
    assert journal.list_runs()[0]["status"] == "failed"
    assert journal.list_runs()[0]["run_id"] == (
        "event:sa_retail_demo:world_simulation:evt_worker_failure"
    )


def test_worker_processes_cold_chain_alert() -> None:
    bus = InMemoryEventBus()
    journal = InMemoryJournal()
    decisions = InMemoryDecisionStore()
    bus.publish(_cold_chain_event())
    worker = CascadeWorker(bus=bus, journal=journal, decision_store=decisions)

    result = worker.process_one()

    assert result.status == "done"
    assert result.cascade is not None
    assert result.cascade["scenario"] == "cold_chain_generator_failure_facilities_review"
    assert decisions.list()[0]["role"] == "facilities_manager"
    assert decisions.list()[0]["action"]["type"] == "dispatch_facilities_check"


def test_worker_journal_isolates_same_event_id_across_data_domains() -> None:
    bus = InMemoryEventBus()
    journal = InMemoryJournal()
    decisions = InMemoryDecisionStore()
    simulated = _event("evt_shared_domain_id")
    operational = replace(simulated, data_domain=DataDomain.OPERATIONAL_TWIN)
    bus.publish(simulated)
    bus.publish(operational)
    seen: list[str] = []

    def handler(event: Event) -> dict:
        seen.append(event.data_domain.value)
        return {
            "status": "ok",
            "decision": None,
            "data_domain": event.data_domain.value,
        }

    worker = CascadeWorker(
        bus=bus,
        journal=journal,
        decision_store=decisions,
        handler=handler,
    )

    first = worker.process_one()
    second = worker.process_one()
    runs = journal.list_runs(tenant_id="sa_retail_demo")

    assert first.status == second.status == "done"
    assert seen == ["world_simulation", "operational_twin"]
    assert {run["data_domain"] for run in runs} == {
        "world_simulation",
        "operational_twin",
    }
    assert len({run["run_id"] for run in runs}) == 2


def test_worker_loop_service_processes_queue_when_enabled(monkeypatch) -> None:
    async def run() -> tuple[dict, list[dict], list[dict]]:
        monkeypatch.setenv("WORKER_ENABLED", "true")
        bus = InMemoryEventBus()
        journal = InMemoryJournal()
        decisions = InMemoryDecisionStore()
        bus.publish(_event("evt_worker_loop"))
        service = WorkerLoopService(
            CascadeWorker(bus=bus, journal=journal, decision_store=decisions),
            poll_s=0.01,
        )
        await service.start()
        try:
            # The actual cascade runs on a worker thread; permit a bounded five-second
            # deadline so full-suite CPU contention cannot make this integration check flaky.
            for _ in range(250):
                if decisions.list():
                    break
                await asyncio.sleep(0.02)
        finally:
            await service.stop()
        return service.status(), decisions.list(), journal.list_runs()

    status, decisions, runs = asyncio.run(run())

    assert worker_enabled() is True
    assert status["processed"] >= 1
    assert decisions[0]["action"]["type"] == "apply_markdown"
    assert decisions[0]["caused_by"] == ["evt_worker_loop"]
    assert runs[0]["status"] == "done"


def test_worker_loop_service_reports_reclaim_counts_and_errors(monkeypatch) -> None:
    async def run() -> dict:
        monkeypatch.setenv("WORKER_ENABLED", "true")
        worker = CascadeWorker(
            bus=InMemoryEventBus(),
            journal=InMemoryJournal(),
            decision_store=InMemoryDecisionStore(),
        )
        calls = 0

        def reclaim_stale(*, min_idle_ms: int) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("redis unavailable")
            assert min_idle_ms == 9000
            return 2

        worker.reclaim_stale = reclaim_stale
        service = WorkerLoopService(
            worker,
            poll_s=0.01,
            reclaim_interval_s=0.01,
            reclaim_idle_ms=9000,
        )
        await service.start()
        try:
            await asyncio.sleep(0.05)
        finally:
            await service.stop()
        return service.status()

    status = asyncio.run(run())

    assert status["reclaimed"] >= 2
    assert status["reclaim_errors"] >= 1
    assert status["last_reclaim_error"] == "redis unavailable"


def _registry() -> CapabilityRegistry:
    async def read_stock(params: dict) -> dict[str, object]:
        _ = params
        return {"on_hand": 240}

    async def write_price(params: dict) -> dict[str, object]:
        return {"applied": params.get("discount_pct", 0)}

    registry = CapabilityRegistry()
    registry.register(Capability("read_stock", read_stock, frozenset({"system", "manager"})))
    registry.register(Capability("write_price", write_price, frozenset({"manager"}), writes=True))
    return registry


def test_plan_validation_gates_exposure_and_write_compensation() -> None:
    registry = _registry()
    bad = Plan(
        plan_id="p1",
        tenant_id="tenant_1",
        actor_role="system",
        steps=[
            PlanStep(key="s1", capability="write_price", params={"discount_pct": 30}),
            PlanStep(key="s2", capability="missing"),
        ],
    )
    good = Plan(
        plan_id="p2",
        tenant_id="tenant_1",
        actor_role="manager",
        steps=[
            PlanStep(key="s1", capability="read_stock"),
            PlanStep(
                key="s2",
                capability="write_price",
                params={"discount_pct": 30},
                compensation={"undo": "restore_price"},
            ),
        ],
    )

    problems = validate_plan(bad, registry)

    assert any("not exposed" in item for item in problems)
    assert any("unknown capability" in item for item in problems)
    assert any("without compensation" in item for item in problems)
    assert validate_plan(good, registry) == []


def test_plan_runner_journals_steps_and_emits_progress() -> None:
    async def run() -> tuple[dict, list[dict], list[dict]]:
        progress: list[dict] = []

        async def publish(kind: str, data: dict) -> None:
            progress.append({"kind": kind, **data})

        journal = InMemoryJournal()
        runner = PlanRunner(_registry(), journal, publish)
        plan = Plan(
            plan_id="p3",
            tenant_id="tenant_1",
            actor_role="manager",
            steps=[
                PlanStep(key="s1", capability="read_stock"),
                PlanStep(
                    key="s2",
                    capability="write_price",
                    params={"discount_pct": 30},
                    compensation={"undo": "restore_price"},
                ),
            ],
        )
        result = await runner.run(plan)
        return result.to_dict(), progress, journal.list_runs()

    result, progress, runs = asyncio.run(run())

    assert result["status"] == "done"
    assert result["outputs"]["s2"] == {"applied": 30}
    assert [(event["i"], event["total"], event["status"]) for event in progress] == [
        (1, 2, "ok"),
        (2, 2, "ok"),
    ]
    assert runs[0]["run_id"] == "plan:tenant_1:operational_twin:p3"
    assert all(event["data_domain"] == "operational_twin" for event in progress)


def test_plan_runner_does_not_auto_invoke_compensation_on_a_later_step_failure() -> None:
    """A step's `compensation` is a recorded rollback instruction, not code PlanRunner
    runs automatically - make that contract explicit and machine-checked instead of only
    documented in a comment, since every registered write capability today is exercised
    by single-step plans only, and it would be easy for a future multi-step write plan to
    be added assuming rollback is automatic when it silently is not.
    """
    applied_prices: list[int] = []

    async def write_price(params: dict) -> dict[str, object]:
        applied_prices.append(int(params.get("discount_pct", 0)))
        return {"applied": params.get("discount_pct", 0)}

    async def always_fails(params: dict) -> dict[str, object]:
        _ = params
        raise RuntimeError("downstream step failed")

    registry = CapabilityRegistry()
    registry.register(Capability("write_price", write_price, frozenset({"manager"}), writes=True))
    registry.register(Capability("always_fails", always_fails, frozenset({"manager"}), writes=True))

    async def run() -> tuple[dict, list[dict]]:
        journal = InMemoryJournal()

        async def publish(kind: str, data: dict) -> None:
            _ = kind, data

        runner = PlanRunner(registry, journal, publish)
        plan = Plan(
            plan_id="p_rollback",
            tenant_id="tenant_1",
            actor_role="manager",
            steps=[
                PlanStep(
                    key="s1",
                    capability="write_price",
                    params={"discount_pct": 30},
                    compensation={"undo": "restore_price", "to_discount_pct": 0},
                ),
                PlanStep(
                    key="s2",
                    capability="always_fails",
                    compensation={"undo": "none"},
                ),
            ],
        )
        result = await runner.run(plan)
        run_id = "plan:tenant_1:operational_twin:p_rollback"
        return result.to_dict(), journal.list_runs(), journal.compensations(run_id)

    result, runs, compensations = asyncio.run(run())

    assert result["status"] == "failed"
    assert result["failed_step"] == "s2"
    assert applied_prices == [30], (
        "the write that already succeeded before the later step failed must still show "
        "its real side effect - PlanRunner does not undo it automatically"
    )
    assert runs[0]["status"] == "failed"
    assert compensations == [{"undo": "restore_price", "to_discount_pct": 0}], (
        "the completed write step's rollback instruction must still be readable from the "
        "journal for a human/operator to act on, even though nothing auto-applied it"
    )


def test_plan_validation_rejects_simulation_write_capabilities() -> None:
    plan = Plan(
        plan_id="sim-write",
        tenant_id="tenant_1",
        data_domain=DataDomain.WORLD_SIMULATION,
        actor_role="manager",
        steps=[
            PlanStep(
                key="s1",
                capability="write_price",
                compensation={"undo": "restore_price"},
            )
        ],
    )

    assert validate_plan(plan, _registry()) == ["write step outside operational domain: s1"]


def test_scheduler_fires_when_due_and_skips_overlap() -> None:
    async def run() -> tuple[int, int, int, bool]:
        now = {"t": 0.0}

        async def publish(_kind: str, _data: dict) -> None:
            return None

        runner = PlanRunner(_registry(), InMemoryJournal(), publish)
        scheduler = Scheduler(runner, clock=lambda: now["t"])
        counter = {"i": 0}

        def make_plan() -> Plan:
            counter["i"] += 1
            return Plan(
                plan_id=f"sweep_{counter['i']}",
                tenant_id="tenant_1",
                actor_role="manager",
                steps=[PlanStep(key="s1", capability="read_stock")],
            )

        schedule = Schedule(name="sweep", every_s=60, make_plan=make_plan)
        scheduler.add(schedule)
        first = await scheduler.tick()
        second = await scheduler.tick()
        schedule.running = True
        now["t"] = 61.0
        due_while_running = bool(scheduler.due())
        schedule.running = False
        third = await scheduler.tick()
        return first, second, third, due_while_running

    first, second, third, due_while_running = asyncio.run(run())

    assert first == 1
    assert second == 0
    assert due_while_running is False
    assert third == 1


def test_compaction_keeps_pinned_facts_and_reports_the_fold() -> None:
    turns = [
        Turn("user", "x" * 400),
        Turn("assistant", "y" * 400),
        Turn("user", "what about SKU 4011?"),
    ]

    output, report = compact(
        turns,
        pinned={"dec_1_recovered": "R378", "sku": "4011"},
        budget_chars=500,
    )
    text = " ".join(turn.text for turn in output)

    assert "R378" in text
    assert "4011" in text
    assert "what about SKU 4011?" in text
    assert report["folded"] >= 1
    assert "[compacted]" in text
