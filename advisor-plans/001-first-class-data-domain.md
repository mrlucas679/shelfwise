# Plan 001: Make data domain a first-class event and twin boundary

> **Executor instructions**: Work on `developers`. Preserve all pre-existing dirty
> changes. Follow every step and run each verification before continuing. This plan
> was written from a dirty worktree, so the commit SHA alone is not the full baseline.
>
> **Drift check (run first)**:
> `git status --short` and
> `git diff --stat -- src/shelfwise_runtime src/shelfwise_contracts src/shelfwise_backend/app.py src/shelfwise_backend/event_store.py src/shelfwise_backend/routes_twin.py src/shelfwise_twin src/shelfwise_worldgen/world.py src/shelfwise_storage/schema.sql tests`.
> Compare the current code with the excerpts below. STOP if the named symbols no
> longer exist or their behavior differs materially. Never reset the dirty files.

## Status

- **Priority**: P0
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: none
- **Category**: security, bug, migration
- **Planned at**: commit `5e306c2`, 2026-07-13, dirty `developers` worktree

## Why This Matters

The application currently infers a record's trust boundary from payload flags and
ID/correlation prefixes. The guard exists only in `app._project_twin_event()`, so
bootstrap replay and direct service/worker calls can bypass it. The operational
twin must reject simulation data at its own boundary, independent of which route
or worker invoked it.

## Current State

- `src/shelfwise_contracts/__init__.py:112-176` - `Event` has no `data_domain`, and
  `parse_wire()` omits it from the allowed fields.
- `src/shelfwise_backend/app.py:2296-2327` - domain detection uses `synthetic`,
  `evt_demo_`, `world_`, and payload heuristics before calling the twin.
- `src/shelfwise_twin/service.py:59-72` - `project_event()` immediately creates
  entities and observations without checking provenance.
- `src/shelfwise_twin/service.py:133-149` - bootstrap filters only tenant and store.
- `src/shelfwise_twin/projection_worker.py:47-67` - the exported worker directly
  calls `self.service.project_event(event)`.
- `src/shelfwise_backend/routes_twin.py:135-147` - bootstrap replays all tenant
  events returned by the event store.
- `src/shelfwise_runtime/provenance.py` already defines the approved vocabulary:
  `operational_twin`, `world_simulation`, `training_fixture`, and `twin_scenario`.

## Commands You Will Need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused tests | `python -m pytest -q tests/test_provenance_boundaries.py tests/test_twin_projector.py tests/test_twin_projection_worker.py tests/test_twin_api.py tests/test_event_store.py` | all pass |
| Backend tests | `python -m pytest -q` | all pass; only existing documented skips |
| Lint | `python -m ruff check src tests scripts` | exit 0 |
| Contract | `python scripts/compare_capability_manifests.py` | capability contract OK |

## Scope

**In scope**:
- `src/shelfwise_runtime/provenance.py`, `src/shelfwise_runtime/__init__.py`
- `src/shelfwise_contracts/__init__.py`
- `src/shelfwise_backend/app.py`, `event_store.py`, `routes_twin.py`
- `src/shelfwise_twin/service.py`, `projection_worker.py`
- `src/shelfwise_worldgen/world.py`
- `src/shelfwise_storage/schema.sql`
- event/twin/provenance tests

**Out of scope**:
- Decision, learning, write-back, candidate, and open-order partitioning (plan 003).
- Operational facts implementation (plan 002).
- Training on W7900 or serving on MI300X.

## Steps

### Step 1: Put domain identity on the canonical event

Export `DataDomain`, `normalize_domain`, and a dedicated `DataDomainBoundaryError`
from `shelfwise_runtime`. Add `data_domain` to `Event`; include it in `parse_wire()`,
`to_dict()`, and CloudEvent output. Validate against `DataDomain` in `__post_init__`.
Use `operational_twin` as the backward-compatible default for real connector,
scanner, CSV, and API events. Make every world generator and every `/demo/*` event
constructor set `world_simulation` explicitly. Never infer new records from IDs.

**Verify**: add contract tests proving wire round-trip and invalid-domain rejection,
then run `python -m pytest -q tests/test_event_ingest.py tests/test_provenance_boundaries.py`.

### Step 2: Persist and filter event domain

Add `data_domain` to in-memory and Postgres event records. Add an optional required
domain filter to list/bootstrap call sites. Update `schema.sql` with an idempotent
column migration and `(tenant_id, data_domain, received_at)` index. For legacy rows
only, backfill explicit payload domains first, then known `evt_demo_`/`world_`
records as `world_simulation`, and leave all other records operational. Document in
the SQL that prefix inference is migration-only.

**Verify**: tests must store the same tenant's operational and simulation events and
return only the requested domain. Run `python -m pytest -q tests/test_event_store.py tests/test_database_schema.py`.

### Step 3: Move the twin guard into `TwinService`

Make `TwinService.project_event()` fail closed with `DataDomainBoundaryError` unless
`event.data_domain == operational_twin`. This is the authoritative guard. Update:

- `app._project_twin_event()` to translate that typed error into a
  `skipped_non_operational` receipt.
- `bootstrap_events()` to count and report skipped non-operational events and never
  call projection for them.
- `TwinProjectionWorker.run_once()` to ACK an intentional domain skip and return a
  `skipped_non_operational` receipt; malformed events still retry/dead-letter.

Remove `_is_simulation_event()` as a steady-state guard after all producers carry
the field.

**Verify**: new tests must prove a simulation event cannot project through the app
helper, direct service call, bootstrap replay, or projection worker. Run the focused
test command from the table.

### Step 4: Make bootstrap operational-only by construction

Change `/twin/stores/{store_id}/bootstrap` to request only
`operational_twin` events from the event store. Keep the service guard as defense in
depth. Return `events_considered`, `events_projected`, and
`events_skipped_non_operational` so the result is auditable.

**Verify**: extend `tests/test_twin_api.py` with one operational and one simulation
event for the same store; clear twin state; bootstrap; assert only the operational
value exists.

### Step 5: Run the complete local gate

Run Ruff, the full Python suite, and the capability contract. Regenerate the
capability manifest only if discovery reports intentional contract drift.

## Test Plan

- Event wire round-trip preserves domain.
- Unsupported domain fails validation.
- Every world/demo producer emits `world_simulation`.
- All four twin entry paths reject simulation.
- Operational event still projects idempotently.
- Bootstrap reports skip counts.
- Event listing can separate two domains for one tenant.

## Done Criteria

- [ ] `Event.to_dict()` always contains validated `data_domain`.
- [ ] No new event producer relies on ID/correlation heuristics.
- [ ] `TwinService.project_event()` itself blocks non-operational events.
- [ ] Bootstrap cannot import simulation state.
- [ ] Full tests, Ruff, and capability contract pass.
- [ ] No unrelated dirty file was reverted.

## STOP Conditions

- A required event producer cannot determine its domain from trusted execution context.
- The schema migration would discard existing rows.
- Fixing a test requires weakening the operational-only twin invariant.
- Any step requires a live model endpoint or cloud GPU.

## Maintenance Notes

Treat `data_domain` like `tenant_id`: every future producer must set it and every
trust-boundary store must preserve it. ID-prefix checks may remain only in the
explicit legacy migration, never in request-time policy.
