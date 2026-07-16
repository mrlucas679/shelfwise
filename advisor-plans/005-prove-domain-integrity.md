# Plan 005: Prove domain integrity in the UI, telemetry, and deployment

> **Executor instructions**: Execute after plans 001-004. This is the release gate.
> Local validation must finish before spending cloud GPU credit. Do not add a new
> frontend dependency without explicit operator approval.
>
> **Drift check (run first)**: compare frontend decision loading/queue code, chat
> audit construction, full-system artifacts, deployment shakedown, CI, and production
> Compose with the current-state evidence below.

## Status

- **Priority**: P1
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: plans 001-004
- **Category**: tests, observability, frontend, deployment
- **Planned at**: commit `5e306c2`, 2026-07-13, dirty `developers` worktree

## Why This Matters

The product can be internally correct and still fail the demonstration if the UI
blends drill and live decisions or the deployment test validates only non-empty
text. Release proof must show the selected domain, exact source facts, actual Gemma
tool calls, no cross-domain side effects, and working multi-user conversations.

## Current State

- Frontend `Decision` and `LearningEvent` types have no domain (`App.tsx:30-47`).
- `pendingQueue()` filters status only (`App.tsx:605-612`).
- The load path fetches unqualified `/decisions` and `/learning`
  (`App.tsx:3176-3188`).
- Product copy calls the generated world a live query (`App.tsx:2197-2206`).
- Chat creates `AuditLog()` inside each request's tool registry
  (`chat.py:345-359`), while `/tools/platform/audit` reads the separate global log.
- Full-system cycle/trail/manifest rows omit `data_domain`.
- Production CI runs `/demo/golden`, then asks generated-product questions although
  production chat defaults to `operational_twin`; it asserts only latency, non-empty
  text, character shape, and distinct responses (`ci.yml:101-193`).
- Chat identity itself is implemented: the backend keys by tenant, user, and
  conversation ID and checks message replay.

## Commands You Will Need

| Purpose | Command | Expected on success |
|---|---|---|
| Backend | `python -m pytest -q` | all pass |
| Lint | `python -m ruff check src tests scripts` | exit 0 |
| Frontend | `cd frontend; npm run typecheck; npm run build` | both exit 0 |
| Capability | `python scripts/compare_capability_manifests.py` | contract OK |
| Deployment local | `python scripts/deployment_shakedown.py --base-url http://127.0.0.1 --cycles 3 --output reports/deployment-shakedown.json` | PASS when topology is running |

## Scope

**In scope**:
- `frontend/src/App.tsx` and existing CSS
- chat audit/metadata wiring
- trace/model-run/observability domain fields
- `src/shelfwise_eval/full_system.py` and artifact validator
- `scripts/deployment_shakedown.py`, its tests, CI, and production Compose
- browser E2E only after dependency approval

**Out of scope**:
- Design-system rewrite.
- New agents or model sizes.
- Starting the cloud droplet before all local gates pass.

## Steps

### Step 1: Make the operator's domain visible and safe

Add `data_domain` to frontend types and API payload handling. Default the normal
workspace to operational data. Show a restrained persistent label such as
`Live store` or `Simulation drill`; do not use color alone. Keep simulation runs in
their existing operations/drill area, not in the operational approval queue.

Filter queue, resolved timeline, thresholds, tasks, product views, and twin views by
the selected/returned domain. An operational transition request must send/confirm
the domain. Replace the generated-world-as-live wording with truthful simulation
language.

**Verify**: typecheck/build pass; fixture/API tests show a simulation pending
decision never appears in the live queue.

### Step 2: Make tool tracing use the shared audit sink

Pass the application's tenant-scoped `tool_audit` into chat instead of constructing
a fresh `AuditLog()` in `_run_agentic_chat`. Include correlation ID, data domain,
tool name, bounded non-secret arguments, result status, and duration. Preserve the
authenticated tenant override. Keep the log bounded/persistent according to the
existing store backend.

**Verify**: one chat request that calls a live tool is visible through
`/tools/platform/audit` with matching tenant, correlation, and operational domain.

### Step 3: Tag telemetry and harness artifacts

Add domain to cascade traces, model runs, full-system config/cycles/decision trail,
feature receipts, and observability summaries. Extend artifact integrity validation
to fail when rows are missing domain or a simulation run claims operational state.
Report counts/tokens/latency per domain so simulation load is not presented as live
store traffic.

**Verify**: generate a short local artifact; validator passes; remove/change a row's
domain in a copied fixture and assert validator failure.

### Step 4: Split deployment proof into simulation and operational phases

Update `deployment_shakedown.py`:

1. Simulation phase runs `/demo/golden`, reviews only world-simulation decisions,
   and asserts simulation learning moves only simulation thresholds with no
   operational write-back.
2. Operational phase onboards a named store, ingests two products and a measured
   cold-chain temperature, checks the twin snapshot, triggers an operational
   workflow, and queries chat using exact SKU/asset facts.
3. Assert returned domain, source refs, shared tool-audit receipts, model/provider
   headers when `--live-required`, conversation replay behavior, and unchanged
   operational values after simulation.
4. Run two authenticated users with separate conversation IDs and overlapping
   message IDs; assert no history or replay leakage.

Do not require the cloud endpoint in short/local mode. `--live-required` must fail
closed unless Gemma network inference and tool calls are proven.

**Verify**: fake-server tests cover every failure code; local deployed topology PASS.

### Step 5: Make CI assert provenance, not presence

Replace the production generated-product chat questions with the operational setup
from step 4. Add explicit assertions:

- operational twin contains no world-simulation observations;
- live queue contains no simulation decisions;
- tool audit proves a live twin tool call;
- chat response metadata says `operational_twin`;
- simulation and operational threshold/ledger counts remain separate.

Keep the simulation smoke as a separate named phase. If live endpoint secrets are
absent, skip only live model proof; never claim it passed.

**Verify**: workflow syntax, local equivalent commands, and all tests pass.

### Step 6: Browser-verify the real workflow

Obtain explicit approval before adding Playwright. If approved, add a minimal E2E
suite for session creation, two-user chat persistence, live-domain label, approval
isolation, cold-chain value rendering, and simulation drill separation at desktop
and mobile widths. If not approved, run the same cases with the available browser
tool and save screenshots/receipts outside source code.

**Verify**: no overlapping text, no simulation decision in live queue, chat persists
more than two messages, and approval actions target the displayed domain.

### Step 7: Spend cloud credit only for the final live gate

After all local gates are green, start the MI300X endpoint and run one bounded
`--live-required` shakedown. Capture tokens, latency, model/provider, tool calls,
concurrency, and domain receipts. Stop the droplet immediately after the proof run.

## Test Plan

- Frontend live/simulation queue isolation.
- Shared chat tool audit with correlation and domain.
- Artifact validator rejects missing/wrong domain.
- Simulation phase cannot alter operational state.
- Operational phase uses exact ingested SKU and temperature.
- Two users and conversation/message IDs remain isolated.
- Live-required mode proves Gemma network/tool use and fails on fallback.

## Done Criteria

- [ ] UI always identifies the active data domain.
- [ ] Operational approval queue excludes simulation.
- [ ] Chat tool calls appear in the shared audit trail.
- [ ] Harness and telemetry report domain on every relevant row.
- [ ] CI asserts provenance and isolation, not merely non-empty responses.
- [ ] Frontend typecheck/build, full tests, Ruff, capability contract, and local
      deployment shakedown pass.
- [ ] One bounded cloud proof passes before recording the final video.

## STOP Conditions

- A new frontend dependency is needed without operator approval.
- Local deployment shakedown is not green; do not start the paid GPU.
- Live-required proof has a fallback answer, missing tool receipt, or missing domain.
- UI can transition a decision without confirming its persisted domain.

## Maintenance Notes

Future demos must state whether they prove simulation behavior, operational behavior,
or live model execution. Those are separate claims and should remain separate in UI,
telemetry, CI, and submission evidence.
