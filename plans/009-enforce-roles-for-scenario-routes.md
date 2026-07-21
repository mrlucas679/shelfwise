# Plan 009: Enforce roles for state-changing scenario routes

> **Executor instructions**: Make the existing role policy explicit on scenario routes that create events or decisions. Preserve read-only previews and do not change the response contracts.

> **Drift check (run first)**: `git diff --stat f7d3ab8..HEAD -- src/shelfwise_backend/app.py tests/test_tenant_auth.py tests/test_golden_cascade.py`

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: security
- **Planned at**: commit `f7d3ab8`, 2026-07-21

## Why this matters

The scenario POST routes create events and pending decisions but use `_SCENARIO_WRITE_DEPS`, which only contains `write_path_guard` and `WRITE_LIMIT_DEP`. They accept `CURRENT_TENANT_DEP`, so any valid JWT role is authenticated but not authorized. Reproduction with a signed `analyst` JWT returns HTTP 200 from `POST /scenarios/golden` and creates a pending decision. The same role is correctly rejected from `/ingest` and decision approval, making this a policy bypass introduced by the separate scenario path.

## Current state

- `src/shelfwise_backend/app.py:2194` defines `_SCENARIO_WRITE_DEPS = [Depends(write_path_guard), WRITE_LIMIT_DEP]`.
- `src/shelfwise_backend/app.py:2420-2796` applies that dependency list to state-changing scenario routes; each uses `ctx: TenantContext = CURRENT_TENANT_DEP` rather than `INGEST_AUTH_DEP` or a role-specific dependency.
- `src/shelfwise_backend/deps.py:123-134` defines `INGEST_AUTH`, allowing only owner, manager, and inventory roles.
- `tests/test_tenant_auth.py` proves analysts are blocked from `/ingest` but does not exercise scenario routes.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused tests | `$env:PYTHONPATH='src'; python -m pytest -q tests/test_tenant_auth.py tests/test_golden_cascade.py` | all pass |
| Lint | `$env:PYTHONPATH='src'; python -m ruff check src tests` | exit 0 |
| Full regression | `$env:PYTHONPATH='src'; python -m pytest -q` | all pass |

## Scope

**In scope**:

- `src/shelfwise_backend/app.py`
- `tests/test_tenant_auth.py`
- `tests/test_golden_cascade.py` only for existing scenario-route expectations

**Out of scope**:

- Changing role definitions in `tenant.py`.
- Requiring an API key in local mode when none is configured.
- Changing safe GET scenario previews unless inspection proves they mutate state.

## Steps

### Step 1: Separate state-changing scenario authorization from read-only preview access

Define a clearly named dependency collection for scenario POST routes that combines the existing write guard/rate limiter with `INGEST_AUTH_DEP` (or the project’s exact existing equivalent). Apply it to every POST scenario endpoint that records an event, invokes an agentic cascade, or creates a decision. Keep GET preview routes separate and explicitly review whether they should retain rate limiting.

**Verify**: no state-changing scenario POST uses only `CURRENT_TENANT_DEP` for authorization.

### Step 2: Add role-matrix regressions

In JWT mode, assert: no token is rejected; an analyst is rejected; an inventory or manager role succeeds; a valid user from another tenant receives only their own created decision. Cover at least one deterministic route and one agentic route without requiring a live model (use the route’s existing fail-closed/offline test configuration).

**Verify**: focused tests pass.

### Step 3: Validate no preview regression

Retain the current preview behavior where documented, including its no-trace/no-decision invariant. If a GET route is unnecessarily subject to a write guard, split the dependency lists only after adding a test that proves it remains read-only and cannot be abused for unbounded work.

**Verify**: focused tests, lint, and full pytest all pass.

## Done criteria

- [ ] Analyst JWT cannot create scenario events or decisions.
- [ ] Authorized operational roles retain successful scenario execution.
- [ ] Read-only previews do not create traces or decisions.
- [ ] Focused tests, lint, and full pytest pass.
- [ ] No files outside scope change.

## STOP conditions

- Stop if product requirements deliberately allow analysts to launch scenarios; obtain a documented role-policy decision rather than weakening the existing ingest policy by inference.
- Stop if agentic scenario regression tests require a live model credential; use deterministic authorization testing before model invocation.

## Maintenance notes

Future router extraction should make a route’s required role visible at the decorator or router level. Add a route-policy contract test so authenticated-but-underprivileged users cannot gain a new mutation path through a convenience/demo endpoint.
