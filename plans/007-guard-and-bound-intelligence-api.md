# Plan 007: Guard and bound the intelligence API surface

> **Executor instructions**: Follow this plan step by step. Run every verification command and confirm the expected result before moving on. Stop on any condition listed below; do not widen the public API contract or modify unrelated routes.

> **Drift check (run first)**: `git diff --stat f7d3ab8..HEAD -- src/shelfwise_backend/intelligence_api.py tests/test_store_intelligence_api.py tests/test_tenant_auth.py`

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: security
- **Planned at**: commit `f7d3ab8`, 2026-07-21

## Why this matters

The four `POST /intelligence/*` calculation routes are included directly on the FastAPI app, but have no `write_path_guard`, rate limit, or authenticated tenant dependency. In `SHELFWISE_AUTH_MODE=jwt`, a request without any token to `/intelligence/deliveries/reconcile` currently receives HTTP 200. Although these endpoints do not persist a decision today, they accept caller-controlled work and are part of the operations API; every comparable POST endpoint uses the shared guard and rate-limit convention. This contradicts the project rule that trust boundaries are validated server-side and creates an unauthenticated resource-consumption surface.

## Current state

- `src/shelfwise_backend/intelligence_api.py:13` creates `APIRouter(prefix="/intelligence", tags=["intelligence"])` with no router-level dependencies.
- `src/shelfwise_backend/intelligence_api.py:66-132` defines four POST endpoints with no dependency declarations or tenant context parameter.
- `src/shelfwise_backend/app.py:281-282` includes this router directly, while most mutation routes in the same file use `Depends(write_path_guard), WRITE_LIMIT_DEP`.
- `src/shelfwise_backend/routes_twin.py:29-35` is the local pattern for protected extracted routes: import `WRITE_LIMIT_DEP` and `write_path_guard`, then put `dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP]` on the mutating route.
- `tests/test_store_intelligence_api.py` currently tests happy paths and validation only. `tests/test_tenant_auth.py` shows the project pattern for JWT-mode negative tests.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Lint | `$env:PYTHONPATH='src'; python -m ruff check src tests` | exit 0 |
| Focused tests | `$env:PYTHONPATH='src'; python -m pytest -q tests/test_store_intelligence_api.py tests/test_tenant_auth.py` | all pass |
| Full regression | `$env:PYTHONPATH='src'; python -m pytest -q` | all pass |

## Scope

**In scope**:

- `src/shelfwise_backend/intelligence_api.py`
- `tests/test_store_intelligence_api.py`
- `tests/test_tenant_auth.py` only if shared JWT helpers make that the cleaner established test location

**Out of scope**:

- Changing calculation result shapes or the decision-science functions.
- Adding tenant-specific persistence to the stateless calculation endpoints.
- Changing the public-demo session policy or API-key mechanism.

## Steps

### Step 1: Apply the existing mutation-boundary policy

Import `Depends`, `WRITE_LIMIT_DEP`, and `write_path_guard`. Apply `dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP]` consistently to all four POST routes. Do not add a tenant dependency merely for appearance: these endpoints have no tenant-scoped storage today, so use the same API-key/write-rate boundary used by other stateless writes.

**Verify**: inspect all four decorators; each contains both shared dependencies.

### Step 2: Add security and capacity regressions

Extend the API tests to set `SHELFWISE_AUTH_MODE=jwt`, `TENANT_AUTH_SECRET`, and a non-empty `API_KEY`. Assert each intelligence route returns 401 without the API key and succeeds with the configured key when its valid body is supplied. Also add a test proving the shared rate limiter can reject repeated calls by configuring it to a small deterministic capacity, then restore its normal test state to avoid leaking configuration across tests.

**Verify**: `$env:PYTHONPATH='src'; python -m pytest -q tests/test_store_intelligence_api.py tests/test_tenant_auth.py` → all pass.

### Step 3: Run the normal gates

Run lint and the full suite. Do not claim the route is safe solely because a happy-path calculation still returns 200.

**Verify**: run every command in the table above.

## Test plan

- All four routes: missing API key is rejected when one is configured.
- One representative route: correct API key preserves its existing response contract.
- One representative route: rate-limit exhaustion returns 429.
- Existing domain validation: cross-SKU FEFO input still returns 422.

## Done criteria

- [ ] Every `/intelligence/*` POST has the common write guard and rate-limit dependency.
- [ ] JWT/API-key negative-path tests exist and pass.
- [ ] `python -m ruff check src tests` exits 0.
- [ ] Full pytest exits 0.
- [ ] No files outside scope are changed.

## STOP conditions

- Stop if a documented client must access an intelligence route without the browser session or API key; report that contract conflict first.
- Stop if applying `write_path_guard` changes a documented public demo flow.
- Stop if the shared limiter cannot be safely reset in test isolation; add a focused fixture rather than mutating global state ad hoc.

## Maintenance notes

When adding an extracted router, make its auth/rate-limit policy explicit at router construction or on every mutating decorator. Add a contract test that enumerates mutation routes and rejects unguarded additions, so this omission cannot recur during future router splits.
