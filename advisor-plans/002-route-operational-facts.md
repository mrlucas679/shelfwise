# Plan 002: Route operational workloads to operational facts

> **Executor instructions**: Execute only after plan 001. Preserve dirty work.
> Operational mode must fail closed on missing store facts; it must never fall back
> to `WorldFactsProvider`.
>
> **Drift check (run first)**: inspect `git status --short` and compare the excerpts
> below with `src/shelfwise_backend/app.py`, `cascade.py`, `worker/worker.py`,
> `state.py`, `world_facts.py`, and `tools/mcp_surface.py`. STOP on material drift.

## Status

- **Priority**: P0
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: `advisor-plans/001-first-class-data-domain.md`
- **Category**: bug, architecture
- **Planned at**: commit `5e306c2`, 2026-07-13, dirty `developers` worktree

## Why This Matters

A real stock, sale, supplier, or cold-chain event currently triggers deterministic
and agentic reasoning against the generated world. This can produce a numerically
valid recommendation for the wrong shop state. The same domain router must be used
by direct API execution, the production Redis worker, product endpoints, and tools.

## Current State

- `app._cascade_for_event():2385-2415` passes global `world_facts` for scan,
  supplier, sale, and cold-chain events.
- `worker.default_cascade_handler():166-178` calls cascades without facts; each
  cascade defaults to the generated world.
- `cascade.py:75-89`, `341-352`, `587-603`, and `991-1012` call
  `(facts or _default_facts()).get_scenario_facts(...)` and create
  `SourceRef.dataset("generated_world", ...)`.
- `/products/attention`, `/products/search`, `/data/seed/summary`, and
  `/tools/platform` always use `world_facts`.
- Production Compose sets `APP_ENV=production` and `WORKER_ENABLED=true`, so the
  unqualified worker handler is the production cascade path.

## Commands You Will Need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused tests | `python -m pytest -q tests/test_workflow_contract.py tests/test_worker_journal.py tests/test_product_catalog_api.py tests/test_live_tool_boundary.py` | all pass |
| Full suite | `python -m pytest -q` | all pass |
| Lint | `python -m ruff check src tests scripts` | exit 0 |

## Scope

**In scope**:
- a new provider protocol/router under `src/shelfwise_backend/`
- operational provider backed by twin, product catalog, inventory positions,
  open orders, tenant profile, and connector facts
- `app.py`, `state.py`, `cascade.py`, `agentic_cascade.py`
- `worker/worker.py`, `worker/service.py`
- `tools/mcp_surface.py`, `product_catalog.py`, `world_facts.py`
- focused provider/dispatcher/worker/API tests

**Out of scope**:
- Persisted domain partitioning (plan 003).
- Adding new decision-science formulas.
- Model-size or GPU deployment changes.

## Steps

### Step 1: Define a domain-neutral facts contract

Extract a `RetailFactsProvider` protocol for only the methods actually consumed by
cascades, product views, and tools. Keep `WorldFactsProvider` as the simulation
implementation. Add `OperationalFactsProvider` that reads exact tenant/store facts
from existing stores. It must return source references identifying the connector,
twin observation, inventory record, or catalog record that supplied each value.

Do not fabricate defaults for missing operational price, demand, expiry, supplier,
or temperature. Return a typed `MissingOperationalFacts` containing required field
names and source scope.

**Verify**: unit tests create two different values in world and operational stores;
each provider returns only its own value and source reference.

### Step 2: Add one `FactsRouter`

Create a router with `for_domain(data_domain, tenant_id, store_id)` returning either
the operational or simulation provider. Reject `training_fixture` and
`twin_scenario` for cascades. Construct it once in `state.py` and inject it into all
dispatchers. Do not read `APP_ENV` inside decision logic; environment only chooses
the default request/view domain at the API boundary.

**Verify**: router tests cover all four enum values and prove there is no fallback.

### Step 3: Unify direct and worker cascade dispatch

Replace duplicate logic in `app._cascade_for_event()` and
`worker.default_cascade_handler()` with one injected `CascadeDispatcher`. It must:

1. read `event.data_domain`;
2. select facts through `FactsRouter`;
3. execute the correct cascade;
4. attach tenant, event, and domain causality;
5. turn missing operational facts into a monitor/insufficient-evidence result or a
   typed no-decision receipt, never generated-world substitution.

Inject the dispatcher into `CascadeWorker` in `state.py`. Demo routes continue to
construct world-simulation events and therefore keep using world facts.

**Verify**: run the same operational event synchronously and through
`CascadeWorker`; assert the same domain, source refs, and decision/no-decision
result. Run the focused tests.

### Step 4: Route product views and tools by domain

Add a validated `data_domain` query/default to product attention/search/seed summary
and platform-tool listing. Production defaults operational; local demo defaults
world simulation. Return `data_domain`, `source_refs`, and `missing_data` in every
response. Build live tools for operational mode and generated-world tools only for
simulation mode.

Operational catalog search may use `product_catalog_store`; stock and freshness must
come from inventory/twin stores. An empty live store returns an empty result with
`missing_data`, not an auto-populated world snapshot.

**Verify**: API tests under `APP_ENV=production` assert generated-world population is
not triggered by operational product requests.

### Step 5: Remove misleading provider coupling

Change cascade type hints from concrete `WorldFactsProvider` to the protocol. Make
source refs provider-owned or derived from provider receipts so operational runs do
not emit `generated_world` labels. Keep deterministic math unchanged.

**Verify**: `rg -n 'WorldFactsProvider' src/shelfwise_backend/cascade.py` returns no
concrete cascade dependency, and operational cascade tests contain no
`generated_world` source refs.

## Test Plan

- Conflicting world/live values select the requested domain.
- Direct and queued paths are equivalent.
- Missing live facts fail closed and identify missing fields.
- Simulation demos still work.
- Product endpoints and tools return explicit domain/provenance.
- Operational requests never lazy-create a generated world.

## Done Criteria

- [ ] One dispatcher serves API and worker execution.
- [ ] Operational events never call `WorldFactsProvider`.
- [ ] Operational source refs identify real stores/observations.
- [ ] Missing live data cannot become a synthetic recommendation.
- [ ] Focused tests, full tests, and Ruff pass.

## STOP Conditions

- An operational formula requires a fact for which no current source store exists;
  report the missing source and add a typed missing-fact result instead of inventing it.
- Worker and direct execution cannot share a dispatcher without changing public API
  response shapes; stop and report the exact conflict.
- A proposed fix selects facts using ID prefixes or untrusted model arguments.

## Maintenance Notes

Every future workflow should receive a provider/router, not import the global
`world_facts`. Reviewers should search for new direct `WorldFactsProvider` imports in
operational modules.
