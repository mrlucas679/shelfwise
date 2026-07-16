# Plan 003: Isolate decisions, HITL, learning, and operational ledgers

> **Executor instructions**: Execute after plans 001 and 002. This is a state
> migration. Preserve all rows and prove both tenant and domain isolation.
>
> **Drift check (run first)**: compare current decision, learning, candidate,
> open-order, write-back, trace, model-run, and schema code against this plan. STOP
> if another migration has changed keys or added `data_domain` already.

## Status

- **Priority**: P0
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: plans 001 and 002
- **Category**: security, migration, bug
- **Planned at**: commit `5e306c2`, 2026-07-13, dirty `developers` worktree

## Why This Matters

Tenant isolation is not enough when one tenant runs both drills and a live shop.
Today simulation approvals can update the same learning thresholds and open-order
suppression state used by operational decisions. The operational approval queue
also cannot distinguish a drill recommendation from a real recommendation.

## Current State

- In-memory decisions are keyed only by `decision_id`; Postgres decisions use
  `id text primary key` (`store.py:21-40`, `schema.sql:3-9`).
- Learning keys are `(tenant_id, metric)` and `(tenant_id, decision_id)`
  (`shelfwise_memory/__init__.py:45-53`, `schema.sql:95-113`).
- Open orders are keyed by `(tenant_id, order_id)` and are observed before the
  simulation twin guard (`app.py:2272-2274`).
- Candidate, write-back, trace, model-run, and tenant-fact records have tenant but
  no domain.
- Both threshold tools call `memory.thresholds()` without `tenant_id`
  (`mcp_surface.py:99-101` and `512-514`).
- Approval always records learning and creates a write-back task for an approved
  decision (`app.py:2179-2194`).

## Commands You Will Need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused tests | `python -m pytest -q tests/test_learning_tenant_scope.py tests/test_open_orders.py tests/test_candidate_store.py tests/test_decision_identity.py tests/test_tenant_auth.py` | all pass |
| Schema tests | `python -m pytest -q tests/test_database_schema.py` | all pass |
| Full suite | `python -m pytest -q` | all pass |
| Lint | `python -m ruff check src tests scripts` | exit 0 |

## Scope

**In scope**:
- `src/shelfwise_action/store.py`
- `src/shelfwise_memory/__init__.py`
- candidate/open-order/write-back/tenant-fact stores
- `src/shelfwise_backend/app.py`, `trace.py`, `tools/mcp_surface.py`
- `src/shelfwise_mlops/registry.py`
- `src/shelfwise_storage/schema.sql` and RLS/schema tests
- frontend-facing API filters, but not frontend rendering (plan 005)

**Out of scope**:
- Changing deterministic decision formulas.
- Direct source-system mutation; write-back remains task-only.
- Production GPU testing.

## Steps

### Step 1: Close the tenant threshold leak immediately

Pass `tenant_id=tenant_id` in both generated-world and live `get_thresholds` tools.
Update test doubles to accept the keyword. Add tool-level tests with two tenants;
store-level tests alone are insufficient.

**Verify**: `python -m pytest -q tests/test_learning_tenant_scope.py tests/test_live_tool_boundary.py`.

### Step 2: Carry domain into every cascade result and decision

Extend causality attachment so result, decision, evidence envelope, and trace all
contain the event's validated `data_domain`. Demo cascades without an event must
receive an explicit world-simulation domain from their route. Reject persistence of
a decision with no domain after migration compatibility is complete.

**Verify**: every cascade test asserts domain on result and decision.

### Step 3: Partition mutable operational stores

Change in-memory and Postgres APIs/keys to include
`(tenant_id, data_domain, record_id)` for decisions, learning events, thresholds,
open orders, candidates, write-back tasks, model runs, traces, and consolidated
tenant facts. All list/get/transition methods must require or explicitly default a
domain at the caller boundary. Update indexes for `(tenant_id, data_domain, ...)`.

Use idempotent SQL migrations. Backfill from an existing payload `data_domain`
first; migration-only legacy demo/world identifiers may map to world simulation;
unclassified rows default operational and must be counted in a migration receipt.
Do not delete rows. Preserve RLS on tenant and add domain predicates in application
queries.

**Verify**: memory and Postgres integration tests store the same logical IDs in two
domains and retrieve/transition them independently.

### Step 4: Make HITL domain-safe

Operational `/decisions` and detail/approve/reject default to
`operational_twin`. Simulation review must be explicit and restricted to the demo
role/path. Require the requested domain to match the persisted decision before a
transition.

For `world_simulation` approvals:

- record learning only in the simulation namespace;
- never create an operational write-back task;
- return `write_back.status = simulation_only`.

For operational approvals, preserve task-only write-back behavior and include the
domain in the idempotency key.

**Verify**: an attempted operational approval of a simulation decision returns 404
or 409 and produces no learning/write-back record.

### Step 5: Isolate suppression and consolidation

Call `open_order_store.observe_event()` with the event domain and use only the same
domain's coverage in candidate suppression. Consolidate learning into tenant facts
within the same domain. Observability must report counts by domain rather than a
single blended total.

**Verify**: a simulation shipment cannot suppress an operational reorder candidate;
a simulation learning event cannot change operational thresholds.

### Step 6: Run migration and complete gates

Run schema tests against Postgres if available locally. If Postgres is unavailable,
the plan is not DONE; mark it BLOCKED with the exact missing service. Then run the
full suite and Ruff.

## Test Plan

- Tool-level tenant threshold isolation.
- Same tenant and same record ID in two domains.
- Cross-domain get/approve/reject blocked.
- Simulation approval creates no operational write-back.
- Simulation threshold movement does not affect operational thresholds.
- Simulation open order does not suppress operational candidate.
- Observability reports separate domain counts.

## Done Criteria

- [ ] Every mutable record produced by a cascade has `tenant_id` and `data_domain`.
- [ ] Store keys and queries partition both values.
- [ ] Simulation cannot create operational side effects.
- [ ] Threshold tools pass authenticated tenant identity.
- [ ] Migration preserves existing rows and reports backfill counts.
- [ ] Full tests and Ruff pass, including Postgres schema tests.

## STOP Conditions

- A migration would drop or overwrite existing records.
- A store API cannot resolve domain without accepting model-supplied arguments.
- A simulation approval still reaches the normal write-back path.
- Postgres migration cannot be tested.

## Maintenance Notes

Domain must be part of every idempotency key that can exist in both a drill and live
operation. RLS remains tenant protection; application/store domain filters provide
the second boundary.
