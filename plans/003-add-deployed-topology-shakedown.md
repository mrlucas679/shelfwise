# Plan 003: Add a deployed-topology shakedown

> **Executor instructions**: Build a new external HTTP harness; do not weaken or replace the existing
> in-process harness. Never make ordinary CI depend on the GPU.
>
> **Drift check**: `git diff --stat 9c907b3..HEAD -- src/shelfwise_eval/full_system.py scripts/track3_prescreen.py docker-compose.production.yml .github/workflows/ci.yml tests`

## Status

- **State**: DONE (public-origin production topology and shakedown passed in GitHub Actions on `45debdf`)
- **Priority**: P1
- **Effort**: L
- **Risk**: MED
- **Depends on**: plans 001 and 002
- **Category**: tests / architecture
- **Planned at**: commit `9c907b3`, 2026-07-13

## Why this matters

The 15-minute receipt is valuable domain proof but is not deployed-system proof. The harness imports
`TestClient` at `src/shelfwise_eval/full_system.py:25`, creates it in-process at `:325-329`, and can
reset memory stores at `:1477-1501`. It therefore bypasses Nginx, real sockets, container limits,
Secure-cookie transport, Postgres pooling/RLS, Redis durability, and frontend delivery. A production
release needs a second receipt that goes through the public origin.

## Current state

- `scripts/track3_prescreen.py` already demonstrates bounded external HTTP probing, cookie handling,
  header checks, and safe JSON receipts; use its request conventions.
- Production Compose runs Nginx frontend, FastAPI backend, Postgres, Redis, and migrations.
- CI performs only a short public-origin smoke and optional two-question model gate.
- The in-process harness must remain the fast exhaustive domain regression suite.

## Commands

| Purpose | Command | Expected success |
|---|---|---|
| Unit tests | `python -m pytest -q tests/test_track3_prescreen.py tests/test_deployment_shakedown.py` | all pass |
| Compose | `docker compose -f docker-compose.production.yml up --build -d --wait` | ready under 60s |
| External short gate | `python scripts/deployment_shakedown.py --base-url http://127.0.0.1 --cycles 3 --output reports/deployment-shakedown.json` | PASS |

## Scope

**In scope**: a new external harness and tests, CI short-mode wiring, report schema/docs, and minimal
read-only diagnostic endpoints only if evidence cannot otherwise be obtained.

**Out of scope**: refactoring the existing full-system driver, browser automation, destructive DB
reset endpoints, or requiring live inference in ordinary CI.

## Steps

### Step 1: Define a deployment receipt schema

Create a typed receipt containing public-origin startup time, frontend response, auth/session cookie
properties, inference readiness, storage/bus backends, route outcomes, decision IDs, HITL transition
matches, learning movements, write-back receipts, chat headers, latency, and failure codes. Never
store cookie or API-key values.

**Verify**: schema serialization tests assert secrets are absent.

### Step 2: Drive the public API over real HTTP

Create `scripts/deployment_shakedown.py` using bounded `httpx.Client` calls. Establish a same-origin
session, execute generated scenarios through public routes, approve and reject actual returned IDs,
query learning/writeback/observability, and ask unseen chat questions. Require model headers only in
`--live-required` mode. Reuse pure integrity helpers where possible, but do not access app globals.

**Verify**: fake-server tests cover pass, route failure, replay, tenant mismatch, chat fallback, and
request timeout.

### Step 3: Add a short production-Compose CI gate

After Compose startup, run three offline-safe cycles against `http://127.0.0.1`. Assert reported
backends are Postgres and Redis and auth mode is JWT. Upload the JSON receipt on failure. Keep the
existing optional live model check separate.

**Verify**: CI local equivalent passes without GPU credentials.

### Step 4: Define the manual 15-minute live release gate

Document the exact `--duration-seconds 900 --live-required` command against an HTTPS origin. Require
zero route/chat/HITL failures, all unique decision IDs, observed learning movement, and model-backed
headers. Save the receipt under a timestamped directory.

**Verify**: a real cloud run produces PASS and can be revalidated without network access.

## Test plan

- Deterministic HTTP fake tests for every failure code.
- One CI integration run through Nginx/Postgres/Redis.
- One manual MI300X live-required 15-minute receipt before release.

## Done criteria

- [ ] In-process and deployed-topology receipts are labeled separately.
- [ ] CI proves Nginx + JWT + Postgres + Redis over public HTTP.
- [ ] Live mode proves real MI300X headers with zero fallback.
- [ ] No secret value appears in artifacts.

## STOP conditions

- Required verification needs a destructive production reset route.
- The public origin is plaintext outside explicit local CI.
- The implementation starts coupling ordinary CI to GPU availability.

## Maintenance notes

Do not merge aggregate counters from the two harnesses. They answer different questions and should
remain separately named in submission evidence.
