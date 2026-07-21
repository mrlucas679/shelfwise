# Plan 011: Make concurrent learning-threshold updates monotonic

> **Executor instructions**: Preserve the learning rule that each metric stores the largest confirmed exposure. Fix the Postgres race without weakening tenant or data-domain isolation.

> **Drift check (run first)**: `git diff --stat f7d3ab8..HEAD -- src/shelfwise_memory/__init__.py tests/test_postgres_schema_contract.py`

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: none
- **Category**: bug
- **Planned at**: commit `f7d3ab8`, 2026-07-21

## Why this matters

`PostgresLearningStore.record_approved_decision` uses `SELECT ... FOR UPDATE` before an upsert. That locks an existing threshold row, but locks nothing when the metric is first seen. Two different approved decisions for the same new metric can therefore both calculate `previous_threshold=0`; their subsequent upserts use the unconditionally supplied value. If the larger exposure commits first and the smaller exposure commits last, the persisted threshold regresses, contradicting the documented “largest confirmed is now” rule.

## Current state

- `src/shelfwise_memory/__init__.py:202-215` reads the threshold with `FOR UPDATE`, then builds an event from that snapshot.
- `src/shelfwise_memory/__init__.py:217-234` upserts `threshold_units = excluded.threshold_units` without enforcing a maximum.
- `_exposure_event` at `src/shelfwise_memory/__init__.py:433-469` defines the intended threshold as `max(previous_threshold, exposure)`.
- `tests/test_postgres_schema_contract.py:257-291` covers double-submit of the *same* decision. It does not race distinct decisions with the same metric and different exposures.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused Postgres test | `$env:PYTHONPATH='src'; python -m pytest -q tests/test_postgres_schema_contract.py -k learning` | all selected tests pass |
| Lint | `$env:PYTHONPATH='src'; python -m ruff check src tests` | exit 0 |
| Full regression | `$env:PYTHONPATH='src'; python -m pytest -q` | all pass |

## Scope

**In scope**:

- `src/shelfwise_memory/__init__.py`
- `tests/test_postgres_schema_contract.py`

**Out of scope**:

- Changing learning metric definitions or exposure formulas.
- Removing the event-per-decision audit trail.
- Replacing Postgres transaction semantics with an in-memory lock.

## Steps

### Step 1: Make the database update monotonic

Change the threshold upsert so the stored value is the greater of its current value and the new value, using a database expression rather than the stale application snapshot. Keep the event insertion idempotent by `(tenant_id, data_domain, decision_id)`.

Decide whether event receipts must report the final committed threshold or the pre-race local calculation. If they must report the final value, obtain it after the upsert inside the same transaction and construct/store the event consistently.

**Verify**: the SQL cannot lower an existing threshold for the same tenant, domain, and metric.

### Step 2: Add a distinct-decision concurrency regression

Create two approved decisions for the same tenant/domain/SKU/metric with unequal exposures and distinct decision IDs. Start them concurrently against the real Postgres store, then assert the final threshold is the larger exposure and each decision has exactly one learning event. Repeat enough times or synchronize at the pre-upsert barrier to make the original check-then-write race deterministic.

**Verify**: focused Postgres test fails before the fix and passes after it.

### Step 3: Preserve existing semantics

Run the existing same-decision concurrency test and tenant/domain isolation tests. Confirm no threshold from another tenant or domain participates in the maximum.

## Done criteria

- [ ] Concurrent distinct decisions cannot decrease a stored threshold.
- [ ] Each decision still creates exactly one idempotent learning event.
- [ ] Threshold monotonicity is proven against real Postgres, not only an in-memory fake.
- [ ] Focused tests, lint, and full pytest pass.

## STOP conditions

- Stop if product semantics require the threshold to reflect the most recent result rather than the largest exposure; update the domain contract first because existing messages and code say otherwise.
- Stop if changing the upsert makes stored event receipts contradict the committed threshold; design one transactionally consistent receipt model before proceeding.

## Maintenance notes

Any aggregate derived from independently committed events must state whether it is monotonic, last-write-wins, or serializable. Use database-level enforcement for that invariant; a prior `SELECT FOR UPDATE` cannot protect a row that does not exist.
