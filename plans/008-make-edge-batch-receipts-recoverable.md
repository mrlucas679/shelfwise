# Plan 008: Make edge-batch receipt recording recoverable

> **Executor instructions**: Follow this plan step by step. Keep the edge route fail-closed and preserve replay idempotency. A failed projection must remain retryable; do not make it look successful.

> **Drift check (run first)**: `git diff --stat f7d3ab8..HEAD -- src/shelfwise_backend/routes_twin.py src/shelfwise_edge/registry.py tests/test_edge_gateway.py`

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: none
- **Category**: bug
- **Planned at**: commit `f7d3ab8`, 2026-07-21

## Why this matters

`POST /twin/edge/observations` records `(tenant_id, batch_id)` as consumed before projecting its observations. If `twin_service.accept()` fails after that reservation, the route returns HTTP 500 but the next retry returns `{status: "duplicate", accepted: 0}`. The observation is therefore lost permanently. This violates the project’s receipt-based ingestion promise and the data-system requirement to make retry/idempotency semantics explicit under partial failure.

## Current state

- `src/shelfwise_backend/routes_twin.py:248-253` calls `edge_device_registry.record_batch(...)`, then projects with `[twin_service.accept(item) for item in batch.observations]`.
- `src/shelfwise_edge/registry.py:59-65` implements `record_batch` as an irreversible insertion into an in-memory set; it has no pending/complete/failed state and no release operation.
- `tests/test_edge_gateway.py:40-68` verifies only a fully successful replay; it does not inject a projection failure and retry the same signed request.
- Reproduction: forcing `twin_service.accept` to raise produces HTTP 500; restoring it and sending the identical signed batch returns HTTP 202 with `status: duplicate` and `accepted: 0`.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused test | `$env:PYTHONPATH='src'; python -m pytest -q tests/test_edge_gateway.py tests/test_twin_api.py` | all pass |
| Lint | `$env:PYTHONPATH='src'; python -m ruff check src tests` | exit 0 |
| Full regression | `$env:PYTHONPATH='src'; python -m pytest -q` | all pass |

## Scope

**In scope**:

- `src/shelfwise_backend/routes_twin.py`
- `src/shelfwise_edge/registry.py`
- `tests/test_edge_gateway.py`

**Out of scope**:

- Replacing the documented in-memory registry with a database-backed device-provisioning system.
- Changing HMAC validation, batch payload schema, or the no-raw-media policy.
- Relaxing idempotency for successfully projected batches.

## Steps

### Step 1: Model batch lifecycle explicitly

Replace the one-way `record_batch` concept with a minimal lifecycle that can atomically claim a batch, mark it complete only after every observation is projected, and release or mark failed when projection throws. Keep successful claims permanently replay-safe. Preserve the registry lock around all state transitions.

Choose names that expose the business state, such as `claim_batch`, `complete_batch`, and `release_batch`; do not hide state mutation behind an ambiguous boolean method.

**Verify**: add direct registry tests if needed, including claim → release → claim and claim → complete → duplicate.

### Step 2: Make the route retry-safe and truthful

In `ingest_edge_observations`, claim only after signature and scope validation. Project observations in a `try` block. On any unexpected projection failure, release the claim, log the batch identifier without signing material, and return a bounded retryable failure. Complete the claim only after all projections succeed. Do not return `duplicate` for a failed attempt.

**Verify**: focused route test passes.

### Step 3: Add failure-then-retry coverage

Extend `tests/test_edge_gateway.py` by monkeypatching `twin_service.accept` to fail once after the signed batch is validated. Use `TestClient(..., raise_server_exceptions=False)` so the response is asserted. Restore the dependency, resend the identical body/signature, and assert the retry projects the observation. Retain the existing success-replay test to prove completed batches still return duplicate.

Also add a two-observation partial-success case: let the first projection complete, make the second throw, then retry the identical signed batch. The retry must report one projected and one duplicate receipt. This proves the release behavior composes correctly with the projector's per-observation idempotency instead of duplicating already-committed state.

**Verify**: `$env:PYTHONPATH='src'; python -m pytest -q tests/test_edge_gateway.py tests/test_twin_api.py` → all pass.

### Step 4: Run the normal gates

Run lint and the full suite. Review the change specifically for races between concurrent duplicates: exactly one request may project a claimed batch, and a failed claim must not strand it.

**Verify**: run every command in the table above.

## Done criteria

- [ ] A projection failure does not consume a batch id permanently.
- [ ] Retrying a previously failed, valid signed batch can project it.
- [ ] A partial projection failure retries without duplicating the already-projected observation.
- [ ] Replaying a completed batch remains a duplicate with zero accepted observations.
- [ ] Focused tests, lint, and full pytest pass.
- [ ] No files outside scope are changed.

## STOP conditions

- Stop if twin projection can partially commit some observations before a later observation fails and the underlying store has no rollback or reconciliation API. Report that transactional boundary; do not silently release and duplicate partial state.
- Stop if a durable registry implementation already exists elsewhere and should replace the in-memory registry; inspect it and revise the scope before coding.
- Stop if the required retry status is governed by an external edge-device protocol not present in this repository.

## Maintenance notes

The current registry is explicitly process-local. This plan fixes retry correctness within that boundary; multi-process and restart-safe exactly-once delivery require a future durable receipt store with the same pending/completed state model.
