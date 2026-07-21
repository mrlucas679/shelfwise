# HANDOFF — current continuation state as of 2026-07-15

> **Working-product branch boundary:** This continuation is implementation work after the
> hackathon and belongs on `developers`. Treat `main` as the protected working-product branch;
> do not commit or merge these changes there without an explicit release decision.

> **Read this section first.** The historical notes below remain as evidence, but many of
> their branch names, counts, and deadlines are stale. The authoritative working branch is
> `developers`; only `main` and `developers` exist locally and on `origin`.

## Phase C break campaign COMPLETE — 2026-07-15

Plan 006's Phase C ran to completion against the real production Compose topology
(Nginx → uvicorn → Postgres → Redis, `APP_ENV=production`, JWT auth) over public HTTP.
Completion report with the capacity table: **`reports/break_campaign_20260715T000000Z.md`**.

- **C1 ramp (new `scripts/phase_c_ramp.py`):** no crash up to **128 concurrent users** — zero
  5xx, zero dropped connections at every step. Sustained accepted-write capacity ~9-10
  events/s on this host (backend capped at 2 CPUs); from 64 users the write rate limiter
  sheds cleanly with 429s. Latency knee at 32 users (p95 ≈ 4.7s, still all-200).
- **C4 races (new `scripts/phase_c_races.py`, 32 threads):** HITL approve/reject and twin
  duplicate observations held. Connector duplicate intake surfaced **two real defects, both
  fixed**: (1) `schema.sql` still carried the 3-column inbound dedup key while the store's
  ON CONFLICT names 4 — every deployed-Postgres intake 500'd; migration ported into
  `schema.sql` and pinned by a static drift test in `tests/test_connectors.py` plus
  write-path coverage in `tests/test_postgres_schema_contract.py`. (2) Concurrent duplicate
  intakes raced the deterministic pkey ahead of the dedup arbiter (`UniqueViolation`,
  2/32 500s); `inbound_store.record()` now treats a concurrent duplicate as a duplicate.
- Data-loss check: 2,200 accepted ramp events == 2,200 Postgres rows. C2 (5-min saturation,
  100% 200s) and C3 (Redis stop / backend restart recovery) were banked 2026-07-14.
- `.env` note: LLM endpoints deliberately point at a dead local port for instant fail-closed
  503s. **Phase D must replace them with the new droplet's :8000/:8001** (comment in `.env`).

**Phase D is unblocked** pending GPU/credit authorization. Runbook: plan 006 Phase D
(D1 sanity + A3 <29s verification → D2 15-min soak with faults/blackout/Postgres → D3
concurrency through the app → D4 30-min endurance only if D2/D3 clean).

## Forensic audit of the full live campaign — 2026-07-14 (night)

The droplet is destroyed; all artifacts persisted locally. A no-sugarcoat forensic audit of the
15m/30m soaks and the concurrency benchmark is in **`reports/soak_audit_20260714.md`** — read it
before citing any headline number from today. Key corrections it makes to earlier framing:
the 504s are NOT warmup/CUDA-graph noise but an SLO arithmetic problem (~19 effective tok/s on
both tiers, dual-model single GPU → any >~500-token response breaks the 30s ceiling at any
concurrency); the timeout middleware sheds clients but not GPU work (sync threadpool routes →
zombie cascades after every 504); the app itself has never been run concurrently (soak is
single-threaded TestClient, benchmark bypassed the app); 88% of route receipts are one endpoint
eating clean generated input; the 30m run replays all 145 of the 15m run's seeds (not
independent); agentic coverage is one-shot and did not scale with duration (12 model calls in
both runs). P0 fixes identified: deadline-aware cascade execution + cancellation of zombie work
— both fixable in app code without a GPU.

## First live MI300X run + stress campaign: two real bugs found and fixed — 2026-07-14 (evening)

**Live run.** Ran the real 15-minute `shelfwise_eval.full_system` shakedown against a freshly
bootstrapped MI300X droplet (both Gemma tiers, dual-model routing). 145 world cycles, 11,600
events accepted, 145/145 chat calls got genuine live model answers, 1,271 approvals/135
rejections with zero HITL mismatches, 360/360 learning movements matched expected. Two findings,
both understood precisely (not just observed and shrugged at):
- `POST /scenarios/golden/agentic` and `POST /scenarios/cold-chain/agentic` consistently hit the
  server's 29s compliance deadline (`SUBMISSION_TIMEOUT_LIMIT_S = 30`) - reproduced identically
  in both a 15-second sanity check and the full 15-minute run, so not a cold-start artifact. All
  six agentic cascades have the same Critic→Executive role-count structure, so this isn't a code
  bug - most likely evidence-payload/response-length variance pushing generation time over
  budget on this droplet's `enforce_eager` (non-cudagraph) vLLM config. Real, documented, not
  fixed (fixing it would mean changing the droplet's inference-serving flags, not app code).
- The "decision reuse" audit failure was traced to the trail and is a **false positive in the
  test harness itself**: the deterministic `/scenarios/procurement` cascade and its agentic
  counterpart correctly converged on the same scenario-stable decision ID (working as designed,
  no duplicate decision minted) - the harness's own audit logic just isn't aware that's expected
  for a deterministic/agentic pair on the same scenario. Not fixed yet; low priority since it's a
  harness precision issue, not an app defect.

**Stress campaign** ("try to break it," at the user's explicit request). Two real, independent
bugs found and fixed, both through actually running load against the code rather than reading it:

1. **`scripts/fleet_scale_shakedown.py` silently under-delivered requested scale.** Requested
   2,000,000 rows; the fleet catalog (`FLEET_SKU_TARGET = 500_000` in
   `shelfwise_worldgen/catalog/generate.py`) can never supply more than 500,000, and
   `islice(..., rows)` just stopped early with zero warning - the CLI's own summary line
   (`wrote ...: 500000 rows`) gave no hint that 1.5M rows were silently missing. Fixed:
   `run_fleet_scale_shakedown` now returns `requested_rows_fully_processed`/`rows_shortfall`
   fields, and the CLI prints an explicit warning and exits 1 on a shortfall instead of a clean
   0. 2 tests (one exercising the real 500k ceiling implicitly via the field values, one via a
   monkeypatched small source for speed).
2. **`TaskWriteBackSink.create_task`/`complete_task` had no lock around a check-then-write
   sequence** - found by code audit while investigating what a concurrent duplicate-approve
   request does, then confirmed structurally (no `Lock` anywhere in the class) - a genuine defect
   regardless of whether one specific test run reproduces it. Honesty note: real concurrent-
   threads stress tests (up to 500 calls / 64 threads / 10 trials, even with
   `sys.setswitchinterval` forced to be extremely aggressive) never actually caught this race
   misfiring under CPython's GIL - so this is a structural fix for a provable design gap
   (relying on GIL timing for correctness is not portable, and free-threaded CPython exists),
   not a bug caught red-handed in the act. Fixed with a `Lock`, matching the pattern already used
   in `InMemoryDecisionStore`. The Postgres-backed `PostgresTaskWriteBackSink` was already safe
   (deterministic hash-derived task ID + `on conflict ... do nothing`), so this only affected the
   in-memory/default backend. New test:
   `test_task_writeback_sink_stays_idempotent_under_real_concurrent_approval_race` (200 calls,
   32 threads) in `tests/test_connectors.py`.

Full suite after both fixes: 618 passed, 6 skipped. Ruff clean. Capability manifest 201/201
(regenerated - `writeback.py`'s source hash changed).

## Full-system harness now covers the whole app, and resets cleanly — 2026-07-14 (later still)

Follow-up to the data-loss fix below: user clarified the upcoming 15-then-30-minute GPU run must
exercise "everything this application is connected to... at full capacity," using synthetic data
that is "easy to remove after." Checked both halves against `src/shelfwise_eval/full_system.py`
and found real gaps in both:

**Coverage gap.** The harness's probes predate this session's newer subsystems - zero coverage of
the digital twin, edge device ingestion, candidate lifecycle history, connector poll status, or
the catalog endpoints. Added four new probe methods (`_probe_operational_twin_and_edge`,
`_probe_candidate_lifecycle`, `_probe_connector_poll_and_catalog`) exercising: twin onboarding +
observation intake + store/snapshot/fidelity reads; a real HMAC-signed edge observation batch
(same signing code path as production); a candidate's full observed -> suppressed lifecycle via
its history endpoint; and the connector-poll-status/catalog-products reads. Registered as five new
`SUPPORT_FEATURES` and their routes in `REQUIRED_ROUTE_RECEIPTS`, so `report.passed` now actually
requires all of them to pass, not just the original golden/procurement/sales rotation. Verified
with a real local 30-second dry run: feature receipts went from 20 to 25, all five new ones
`passed: true`.

**"Easy to remove after" gap.** `_reset_in_memory_state()` (the function that wipes in-memory
state between/after runs) predates most of the stores the app now has - `candidate_store`,
`chat_store`, `open_order_store`, `inventory_position_store`, `connector_cursor_store`,
`world_snapshot_store`, `edge_device_registry`, and the twin's internal store/calibrations/
onboarding-manifest registries were all silently left un-cleared. Fixed `_load_runtime()` to
expose them and added them to the reset list. Verified with a dedicated test that populates every
one of these stores directly, runs the reset, and asserts each is empty afterward - not asserted
from reading the code, actually exercised.

Note: `_reset_in_memory_state` only ever ran for `SHELFWISE_STORE_BACKEND=memory` (unchanged,
correct - it must never auto-wipe a real Postgres database). For a droplet run using Postgres,
"easy to remove after" is the existing `persist/` bind-mount directory: stop the compose stack,
`rm -rf persist/`, done - no new mechanism needed there.

4 new tests, full suite 616 passed, Ruff clean, capability manifest unchanged (eval tooling, not
an app route).

## Fixed the real cause of lost soak-run data, before the next live GPU session — 2026-07-14 (later still)

User is about to create a real MI300X droplet ($6.17 AMD credit, 25-day expiry) to run a 15-minute
full-application shakedown, then decide on 30 minutes - and asked to make sure data isn't lost
this time. Investigated why `reports/` already had `soak_final`, `soak_final2`, `soak_final3`,
`soak_final4`, `soak_diag`, `soak_15m_retry`, `soak_postfix_final` sitting around - all but two
(`reports/soak` and `reports/soak_15min_20260711T042648Z`, the one documented "known-good" run)
have a `cycles.jsonl` with only 3 lines, meaning they crashed almost immediately.

Found the real bug in `src/shelfwise_eval/full_system.py`: `manifest.json` and every other
summary artifact (`feature_receipts.json`, `route_receipts.json`, `learning_events.json`,
`chat_samples.json`) were written exactly once, only after every probe phase in `run()` completed
successfully. Only `decision_trail.jsonl`/`cycles.jsonl` were written incrementally per cycle. So
any interruption - SSH drop, droplet timeout, Ctrl+C, an unhandled exception - meant the entire
summarized report was lost, leaving only a raw, unsummarized trail. This is exactly the pattern
that produced the pile of retry directories: each crash meant starting over with no report to
diagnose from.

Fixed two things:
1. `_FullSystemDriver.run()` now writes a best-effort `manifest.json` (and the other artifacts)
   on every exit path, including an exception or `KeyboardInterrupt` - verified with a real test
   that monkeypatches a probe phase to raise mid-run and confirms `manifest.json` still exists,
   `passed: false`, and totals reflect everything accumulated before the interruption.
2. Reusing an `--output-dir` that already has a `manifest.json` now fails fast
   (`FileExistsError`) instead of silently truncating the previous run's `decision_trail.jsonl`/
   `cycles.jsonl` - exactly the directory-name-collision pattern visible in the `soak_final*`
   history. New `--overwrite-artifacts` flag for the rare case that's actually intended.

Updated `docs/mi300x-recreate-runbook.md`'s Application Shakedown section: timestamped
`--output-dir` for every run (no more static `reports/soak_15m`), explicit "run 15 minutes,
inspect the result, then decide on 30 minutes" sequencing matching what the user asked for, and
both commands ready to copy-paste.

Verified for real, not just via unit tests: ran a local 20-second dry run end to end
(`SHELFWISE_STORE_BACKEND=memory`, no live model), confirmed a second run against the same
directory was refused with the new clear error, and confirmed
`scripts/validate_full_system_artifact.py` accepts the completed run's manifest cleanly. 3 new
tests (`tests/test_full_system_harness.py`). Full suite: 615 passed, Ruff clean, capability
manifest unchanged (this is eval tooling, not an app route).

Did not touch the existing `reports/soak_final*`/`soak_diag`/etc directories - they're evidence
of the bug just fixed, not something to clean up unilaterally; left for the user to decide.

## Training data matrix expanded (harness code only, no GPU run) — 2026-07-14 (later still)

Third item from the earlier "still external" list: "full training matrix." Confirmed the real
environment boundary first - actual training runs on the separate ROCm/Jupyter pod
(`docs/model-training.md` says so explicitly), which this session has no access to. Asked the
user what "expand" should mean given that; agreed scope: expand the harness code/config only,
verify via local dry run, no GPU training attempted.

Found the training data generator (`src/shelfwise/training/simulation.py`) only ever produced
procurement/delivery-exception scenarios (12 case types) - the model has never been trained on
the decision domains that generate most of this app's real recommendations today (expiry
markdown, cold-chain escalation, price-integrity guardrails). Added three case types (`expiry
markdown decision`, `cold-chain temperature breach`, `price integrity mismatch`), each wired into
`_case_type`/`_risk_level`/`_expected_output`/`EVIDENCE_BY_CASE`, and registered the three new
`case_type` values in `dataset.py`'s `VALID_CASE_TYPES` strict allowlist (missed this the first
pass - the dry-run tests caught it immediately). Verified with a real
`build_shakedown_datasets()` dry run: all three appear in the case breakdown at the expected
round-robin frequency, not just present in source. 3 new assertions in
`tests/test_shakedown_pipeline.py`; training harness suite 13/13.

## Automatic connector poll scheduling implemented — 2026-07-14 (later still)

Wired up the piece flagged earlier today as a genuine gap: nothing ever called the already-built
`PollingConnector.pull()`. Investigating why surfaced the real blocker - Odoo/SAP/SYSPRO
connectors need real per-tenant credentials (`base_url`, API keys) to construct, and no storage
for those existed anywhere. Asked the user how to handle credentials rather than inventing a
security-sensitive design unilaterally; chosen: env vars, single-tenant, matching how
`LLM_ROUTINE_BASE_URL` already works (this deployment is genuinely single-tenant today).

Built:
- `src/shelfwise_backend/connector_poll_service.py` - `ConnectorPollService`, a lifespan-managed
  background loop (mirrors `WorkerLoopService`'s start/stop/status pattern) gated by
  `CONNECTOR_POLL_ENABLED` (off by default). Polls whichever of Odoo/SAP/SYSPRO has its full env
  credential set present; a partially-configured system is skipped, not polled broken. Each
  yielded record goes through `app.py`'s existing `_process_inbound_record` (injected as a
  callback to avoid a backend->app.py circular import), off the event loop thread via
  `asyncio.to_thread` so a slow poll can't stall other requests.
- `PostgresCursorStore` in `src/shelfwise_connectors/connectors/poll.py` - durable poll-position
  persistence (new RLS-protected `shelfwise_connector_cursors` table) so a restart resumes
  instead of re-fetching a system's entire history. Verified with a direct round trip against a
  throwaway Postgres container: set a cursor, construct a *fresh* store instance (simulating a
  process restart), confirm it reads back the same cursor.
- `GET /connectors/poll/status` route; added to the frontend's endpoint registry and confirmed
  rendering live (`connected`) in the Operations workspace via the Browser pane, not just assumed.
- `.env.example` documents all 9 new vars (`CONNECTOR_POLL_ENABLED` +
  `SHELFWISE_CONNECTOR_{ODOO,SAP,SYSPRO}_*`).

9 new tests (`tests/test_connector_poll_service.py`, `tests/test_connector_poll_status_api.py`,
plus a `PostgresCursorStore` assertion added to `test_postgres_schema_contract.py`). Full
verification: 613 passed (1 known-flaky async worker-loop test re-confirmed in isolation), Ruff
clean, capability manifest 201/201, frontend typecheck/build clean, Playwright E2E 3/3.

Explicitly not built: real multi-tenant encrypted credential storage - scoped out as its own
future decision, not something to bolt on under this env-var-based single-tenant design.

## Redis image CVE fix — 2026-07-14 (later still)

User flagged a Docker image scanner finding (CVE-2025-60876, medium 6.5) on the `redis:7-alpine`
image used by both compose files. Corrected an inaccurate first read of the CVE (initially
described as an `apk`/APKINDEX heap overflow) - it is actually a BusyBox `wget` HTTP
request-splitting bug. Confirmed no Alpine fix exists yet by re-pulling the exact same digest
already running (`6ab0b6e73817`, Alpine 3.21, busybox `1.37.0-r14`) - still unfixed. Real fix:
swapped both compose files to `redis:7-bookworm` (Debian-based, ships no BusyBox at all, so the
CVE is structurally impossible, not suppressed). `docker scout` shows the bookworm variant has
more total findings (1 critical + 2 high, all in `perl`, all also unfixed upstream) - noted
honestly rather than presented as a clean win, but none of those packages are ever invoked by the
`redis-server` process itself, same real-world exposure as what was replaced. Verified: both
compose files validate, `redis-cli ping` passes against the new image, and the full production
topology (Postgres/Redis/migrate/backend/frontend) came up healthy and passed the same
session/`/scenarios/golden` smoke this session's earlier readiness pass used.

## Candidate history and Playwright E2E implemented — 2026-07-14 (later same day)

The prior readiness pass below correctly identified candidate history/partitioning and browser
E2E as genuine gaps, not bugs - but on request, both are now actually implemented, not just
scoped:

- **Candidate history**: `src/shelfwise_backend/candidate_history.py` - append-only lifecycle
  transitions (observed, status_changed, suppressed, linked_decision) per candidate, memory +
  Postgres (RLS-protected `shelfwise_candidate_history`), `since`/`until` bounded queries. Wired
  into both `CandidateStore` implementations; new route `GET /candidates/{candidate_key}/history`;
  added to the frontend's `OPERATION_READ_ENDPOINTS` registry. 10 new tests (unit, API, and a real
  round-trip against a throwaway Postgres container via the established
  `MSYS_NO_PATHCONV=1 docker cp`/`psql` pattern). Capability manifest: 200.
- **Playwright E2E**: `frontend/playwright.config.ts` + `frontend/e2e/golden-path.spec.ts`. Three
  real tests, all verified passing against the actual running app (not asserted from code
  reading): console loads with chat input/approval-queue affordances; approving the seeded golden
  decision through the real UI clears the queue and logs the outcome (the exact flow
  `DEMO_RUNBOOK.md`'s three-minute story drives - confirmed by manually driving it once first via
  the Browser pane to get real selectors before writing the test, not guessing them); a direct
  chat question returns a real non-empty grounded answer. `npm run test:e2e` runs it locally;
  wired into CI right after the frontend build step with a report-artifact upload on failure. The
  Python executable path is resolved portably (prefers this repo's `.venv`, falls back to
  `python`/`python3` for CI where there is no venv) rather than hardcoded to one OS.
- Full verification after both: backend `605 passed, 6 skipped`, Ruff clean, capability manifest
  200/200, frontend `typecheck`/`build` clean, Playwright suite `3 passed`.

## Pre-testing readiness pass — 2026-07-14 (later same day)

Went through `IMPLEMENTATION_STATUS.md`'s "Still External / Not Claimable Yet" list item by item -
fixed what's actually fixable locally, verified the exact tooling needed tomorrow works, and left
alone what's a deliberate design posture or a genuinely large future feature (never rushed a
shallow version of either). Full detail is in `IMPLEMENTATION_STATUS.md`'s matching section;
summary:

- Confirmed both Docker images build clean for `linux/amd64` (`docker image inspect` verified
  `architecture=amd64`) - publishing to a public registry itself needs the user's own registry
  credentials, so that step is documented, not performed here.
- Actually brought up the full `docker-compose.production.yml` stack locally (Postgres, Redis,
  migrate, backend, frontend - all reached `Healthy`) and ran the exact CI smoke sequence by hand
  (session creation, `/scenarios/golden`, all response assertions) - passed clean against the real
  running stack, confirming the compose file and Nginx routing are genuinely correct right now.
  Cleaned up the stack and the host-mounted `persist/` directory it created afterward.
- Checked the configured MI300X droplet endpoints in `.env` - genuinely two distinct
  routine/strong ports and model IDs, but the droplet is not currently running (expected
  idle-cost behavior, not a bug) - confirmed via a direct, read-only reachability check.
- Validated the concurrency benchmark config offline (`--validate-config` → 11 agents, 4
  strategies, all valid) and added the missing 1/8/32 sweep loop to
  `docs/mi300x-recreate-runbook.md` (previously only had a `--peak 32` example).
- Re-verified every connector's catalog transport claim against its actual implementation class -
  all correct now (Lightspeed was the only mismatch, already fixed earlier today).
- Re-ran the training harness tests (7 passed) to confirm no regression from today's other
  changes.
- Confirmed Playwright/browser E2E is a genuine, currently-absent gap (no dependency, no script in
  `frontend/package.json`) - correctly left as a real future task rather than bolted on tonight.

## Digital twin plan audit: four real bugs found and fixed — 2026-07-14

Read `DIGITAL_TWIN_RESEARCH_AND_IMPLEMENTATION_PLAN.md` (all 44 sections, twice - once
categorizing narrative vs. technical claims, once tracing every specific technical claim into
the actual running code) and fixed every real bug that read-through surfaced, not just the ones
that were quick. Full detail, reproduction steps, and file/line references live in that document's
own dated audit entries; this is the summary for anyone picking this branch up next.

1. **Lightspeed connector capability mismatch.** `src/shelfwise_connectors/catalog.py` declared
   `transport="webhook_or_poll"` for Lightspeed; only a webhook receiver exists in
   `connectors/systems/lightspeed.py` (unlike SAP/Odoo/SYSPRO, which have real poll
   implementations). Corrected to `"webhook"`. Verified live via `GET /connectors/systems`.
2. **Twin projection hash was not actually deterministic across replay.** The hash
   `src/shelfwise_twin/service.py::snapshot()` computes for replay/recovery verification
   included `TwinEntity.created_at`, `TwinRelationship.valid_from`, and
   `TwinPropertyState.projected_at`/`confidence`/`freshness` - all stamped from wall-clock `now`
   at projection time. Replaying the identical event log at a different real time produced a
   different hash even with zero substantive state change, directly contradicting the "replay,
   recovery" claim the hash exists to support. Fixed by excluding those fields from the hashed
   canonical JSON (they remain present and correct in every API response that returns the
   actual objects - only the hash computation changed).
3. **Onboarding-created twin topology could not survive a real recovery scenario.**
   `POST /twin/onboarding` wrote entities/relationships straight into the twin projection store
   and never touched the durable event log, so `/twin/stores/{id}/bootstrap` (which replays only
   `operational_twin` events - the mechanism this document's Definition of Done means by "restart
   preserves the projection hash") could not reconstruct onboarded fixtures or the onboarded
   store's own display name/attributes; a real projected-state loss would have silently reverted
   them to generic defaults. Fixed by adding `src/shelfwise_twin/onboarding_store.py`
   (`OnboardingManifestRegistry`, memory + Postgres, mirroring the existing `calibration.py`
   pattern) - `TwinService.onboard()` now persists the manifest there, and
   `TwinService.bootstrap_events()` replays it before replaying events. New Postgres table
   `shelfwise_twin_onboarding_manifests` with RLS.
4. **Agentic chat reported the wrong model in its own response metadata.** `role="chat"` is not
   one of the routed agent roles in `src/shelfwise_backend/tools/model_runtime.py`, so it always
   falls through to the hybrid architecture's routine default - but
   `chat.py::build_chat_reply_with_meta` unconditionally set `meta["model"]` to the strong-model
   ID before either the agentic or non-agentic path had even run. Every agentic chat response (the
   primary path whenever `decisions`/`memory` are supplied - true in production) recorded the
   wrong model in its own audit trail. Fixed so both paths report whichever model the run that
   actually produced the answer used (`AgentRunResult.model_calls` for the agentic path,
   `InferenceResult.model` for the fallback).

Each fix has a dedicated regression test (`tests/test_twin_api.py`,
`tests/test_chat_model_metadata.py`) that reproduces the original failure and proves the fix,
not just an assertion that a symptom is gone. Capability manifest regenerated (199 capabilities)
for the new `onboarding_store.py` module. Full suite: **595 passed, 6 skipped**; Ruff clean.

No other bugs are known as of this pass. The rest of the plan document's findings (Section 6's
open "continuous synchronization" connector-scheduling gap, Section 9's onboarding manifest being
far simpler than its 13-file spec, Section 21's remaining Definition-of-Done gaps - historical
time-travel reconstruction, Store Twin UI-driven fidelity display, a consolidated onboarding
receipt pack - and Section 30's file-tree divergence) are real, precisely-located, but are feature
gaps and product/UI decisions, not bugs; they are recorded in the plan document as follow-up work,
not silently built mid-audit.

## Deployment reproducibility update — 2026-07-13

- The authoritative fresh-droplet path is `DROPLET_BOOTSTRAP.md` plus
  `scripts/bootstrap_mi300x_vllm.sh`. The script resolves `/opt/shelfwise` from its own location,
  validates host tools, ports, ROCm devices, secrets, and `VLLM_ALLOWED_CIDR` before downloading
  weights, and does not depend on the operator's current directory.
- After authenticated `/v1/models` checks pass for both tiers, it writes the secret-free
  `/root/shelfwise-mi300x-bootstrap.json`. Keep that file with the exact Git commit and the public
  application shakedown receipt; it is the deployment handoff proof for model identity, ports,
  allowlist, vLLM version, and readiness.
- A timeout now prints the correct Quick Start vLLM log for the failing port. The historical
  `docker start rocm` snippets remain recovery commands for an existing container only; do not
  use them as the fresh-droplet install path.

## Frontend/system bug audit pass (2026-07-12, this session)

Goal: act as a debugger, find and fix real bugs across frontend + backend, no redesign,
no hardcoded/cached answers (evaluation uses unseen variants).

Confirmed and fixed:

1. **Duplicate approval-queue notifications (the reported symptom).** `src/shelfwise_backend/app.py`
   `_demo_event` / `demo_recall` / `demo_inventory_exception` minted a fresh random `uuid4()` suffix
   on every call, so every click of a demo trigger (or every reload that replays it) created a brand
   new pending decision for the identical underlying scenario - the approval queue filled up with
   near-identical "Apply 20% markdown ... Selati Flour Low Fat" cards (verified live: 3 repeated
   `POST /scenarios/golden` calls produced 4 separate pending decisions before the fix). Fixed by deriving
   the event id deterministically from `(tenant, event_type, sku, day)` via a new
   `_demo_occurrence_suffix()` helper that reuses a still-pending decision's id (dedupe) but advances
   to a new occurrence once the prior one is resolved (approved/rejected) - so a new day's scan or a
   fresh incident after resolution is still a genuinely new decision, matching
   `tests/test_golden_cascade.py::test_demo_golden_read_does_not_reset_resolved_decision`. Verified
   live: repeated calls now collapse to exactly one decision per demo trigger type.
2. **Stale field names in the Products search receipt.** `frontend/src/App.tsx`'s "Search receipt"
   panel read `source_counts.synthetic_scanned` / `synthetic_scan_budget` / `synthetic_total_estimate`
   / `seed` / `synthetic_catalog` - all left over from the old CSV-seed + synthetic-catalog-blend
   design. The real `/products/search` response only ever returns `source_counts.generated_world`
   (the generated world is the whole catalogue now, no separate synthetic layer), so the panel always
   showed "0 rows scanned" / "0 seed matches · 0 catalogue matches" even when real results came back.
   Fixed to read the real field and reworded the two receipt rows honestly ("Generated-world scan" /
   "Query"). Updated `tests/test_frontend_attention_contracts.py` and
   `tests/test_frontend_product_contracts.py` to match the corrected copy/field name.
3. **"To order" workspace only ever showed 0-1 products despite the sidebar badge saying "16
   products".** `renderToOrder` in `frontend/src/App.tsx` built its list solely from
   `intel.store_intelligence.supplier_cover` (the single hero-SKU object), never from the real
   per-tenant `ops.productAttention.to_order` array the backend already returns (confirmed via
   `GET /products/attention`: `to_order` has 16 real rows, matching `totals.to_order_products`).
   Fixed to render `apiToOrderItems` (the real list) first, falling back to the single
   supplier-cover line only when that list is empty - mirroring the working pattern already used
   by "Sell first" (`apiSellFirstItems` over the single `batch` object). Verified live: "To order"
   now lists all 16 real products instead of one stale line.
4. **One store's catalogue mixed six competing SA retail chains' own private labels together.**
   `src/shelfwise_worldgen/catalog/brands.py`'s `PRIVATE_LABEL` pool appended *every* major SA
   supermarket chain's house brand (PnP, Checkers Housebrand, Shoprite, Woolworths, SPAR, Boxer) to
   every category's brand pool - so one store's shelf showed products from six competing retailers'
   own labels simultaneously (reported as "products from different shops... we didn't give the data
   much focus"). No real store stocks a competitor's private label. Fixed by splitting the pool into
   chain-neutral generics ("No Name", "Ritebrand") plus exactly one retail-chain house brand chosen
   deterministically per world seed (`house_brand_name(seed)` / `private_label_pool(seed)`), threaded
   through `pool()`, `generate_catalog()`, and `count_estimate()`. Verified: for the demo seed
   (20_260_710) the house brand is "Boxer"; a live `/products/attention` scan now shows only
   `Boxer SA (Pty) Ltd` as the chain-brand supplier, alongside real manufacturer brands (Clover,
   Tastic, Ace, etc.) and chain-neutral generics (No Name, House, Select, Ritebrand, Premium
   Choice) - never a mix of Woolworths+Shoprite+SPAR+PnP+Checkers at once. Updated
   `tests/test_catalog_worldgen.py`'s `count_estimate` calls for the new `(seed, scale)` signature.
5. **Deliveries workspace had no drill-down** - the one delivery-exception row showed only
   ordered/received/accepted/short-dated units and a "short" count, with no way to see ASN quantity,
   over-delivery, rejected units, or the supplier fill rate (all already computed by the backend's
   `delivery_reconciliation`, just never surfaced). Reported as "when there is an issue with delivery
   you can't click the thing you're supposed to click to see what is really happening... that last
   information you need to see." Fixed by making the row clickable (same `active`/`onSelect`
   pattern the Products workspace already uses) to reveal a "Reconciliation detail" panel with the
   remaining fields. Verified live: clicking the delivery row now expands ASN vs. receiving detail
   and the supplier fill rate (67% in the running demo).
6. Regenerated `capabilities/manifest.json` after each frontend/backend change (no route/tool shape
   changed - just the id-generation helper, workspace rendering, and worldgen brand-pool logic).

Historical snapshot from the 2026-07-12 frontend pass, superseded by the current verification
baseline below: 454 tests passed, 3 skipped; Ruff and TypeScript were clean; the running app was
manually driven in-browser with no console errors.

Not yet done / lower priority: full line-by-line review of the rest of `App.tsx` (3300+ lines) beyond
the workspaces actually exercised above; a wider audit could still turn up more latent issues if asked
to continue.

## CURRENT UPDATE — disposable-droplet recovery and frontend pass — 2026-07-12

Read this section before continuing. The worktree contained active application/frontend changes
when this recovery pass began. They were preserved, tested, and are intended to be saved on
`developers`; do not reset or discard them.

### New MI300X provisioning path

- `scripts/bootstrap_mi300x_vllm.sh` is the authoritative new-droplet command. It requires a
  user-supplied Hugging Face token with accepted Gemma licences and a vLLM API key, uses the
  provider's preinstalled Quick Start container when present (otherwise pulling the pinned
  official Gemma 4 ROCm vLLM image), starts E4B routine on `8000` and 31B strong on `8001`, and
  blocks until both authenticated `/v1/models` responses prove the intended models are loaded.
- `DROPLET_BOOTSTRAP.md` contains the exact clone, secret, firewall, application configuration,
  and Track 3 prescreen sequence. Do not use the historical `docker start rocm` commands for a
  newly created droplet; those only apply to the old pre-existing container.

### Recovery setup now implemented

- `scripts/session_capsule.py` creates a safe recovery capsule and archive. It captures Git HEAD,
  status, staged/unstaged binary diffs, untracked files, redacted environment metadata,
  Python/pip, Docker, ROCm/GPU, apt, and systemd diagnostics, PostgreSQL/Redis exports when
  configured, the complete `SHELFWISE_PERSIST_ROOT` contents except the capsule itself, training
  runs/adapters, harness runs, generated data, uploads, logs, reports, and SHA-256 checksums.
- The command has `create`, `verify`, and guarded `restore` subcommands. It never destroys a
  Droplet or deletes an existing restore target. `--strict` fails when configured DB exports fail.
- `src/shelfwise_runtime/paths.py` centralizes durable paths. Training, evaluation, benchmark
  reports, and full-system harness artifacts honor the persistence root when configured.
- Both Compose files bind Postgres, Redis, and `/app/persist` below
  `${SHELFWISE_PERSIST_ROOT:-./persist}` instead of relying only on anonymous Docker volumes.
  Redis AOF is enabled in the local Compose profile too.
- Accepted voice/image uploads are content-addressed into `UPLOAD_DIR` when configured; the API
  returns a safe `upload_ref`, never a machine path.
- `.env.example` documents `/workspace/persist` for durable state and `/scratch` for rebuildable
  Hugging Face, Torch, Triton, and temporary caches.

### Exact capsule commands on the Droplet

Run these before destroying or powering down the disposable environment. Do not run a destroy
command from the application; destruction remains a manual, separately approved operation.

```bash
export SHELFWISE_PERSIST_ROOT=/workspace/persist
export TRAINING_OUTPUT_DIR=/workspace/persist/training
export HARNESS_RUN_DIR=/workspace/persist/harness/runs
export TRACE_DIR=/workspace/persist/runtime/agent-traces
export EVENT_STORE_DIR=/workspace/persist/runtime/events
export UPLOAD_DIR=/workspace/persist/application-data/uploads
export LOG_DIR=/workspace/persist/logs
export HF_HOME=/scratch/huggingface
export TORCH_HOME=/scratch/torch
export TRITON_CACHE_DIR=/scratch/triton
export TMPDIR=/scratch/tmp

python scripts/session_capsule.py create \
  --repo /workspace/shelfwise \
  --root /workspace/persist \
  --strict \
  --archive /workspace/persist/capsules/shelfwise-session-$(date -u +%Y%m%dT%H%M%SZ).tar.gz
```

The command must exit successfully and print an empty `failures` list. Verify the capsule before
downloading it:

```bash
python scripts/session_capsule.py verify /workspace/persist/capsules/shelfwise-session-<timestamp>
sha256sum /workspace/persist/capsules/shelfwise-session-<timestamp>.tar.gz
```

Only after API/training shutdown, database dumps, Redis persistence, capsule creation, checksum
verification, download, and local checksum verification have succeeded may the Droplet be
destroyed. Restore into a new MI300X with:

```bash
python scripts/session_capsule.py restore shelfwise-session-<timestamp>.tar.gz \
  --target /workspace/recovery
```

Then restore Postgres from `databases/postgres.dump` with `pg_restore` and restore Redis by
placing `databases/redis.rdb` in the configured Redis data directory before starting Redis.
Inspect the restored manifest and rerun application health, database row-count, latest decision/
event ID, checkpoint readability, harness receipt, frontend connectivity, and AMD inference
checks before resuming training. Do not resume training after a failed recovery check.

### Frontend/application fixes in the current worktree

- Operations lists now use generated product names and per-product delivery reconciliation,
  rather than displaying only a hero SKU or a single delivery exception.
- The attachment control advertises only implemented image and voice endpoints. PDF is no longer
  offered as if it were supported.
- Attachment failures show a safe user-facing message instead of raw exception text.
- The backend persists accepted media when `UPLOAD_DIR` is configured.
- Demo trigger IDs deduplicate repeated pending clicks but create a new occurrence after a prior
  decision is resolved.
- Chat offline delivery answers and the live delivery tool use the same generated-world data.
- Current verification: `454 passed, 3 skipped`; Ruff clean; frontend typecheck and build pass.

### Save state before credit exhaustion

- Current branch is `developers`; only `main` and `developers` should remain.
- The implementation and handoff are saved and pushed in commit `c475d5b`. Only the existing
  untracked run artifacts remain; do not stage them unless intentionally packaging evidence.
- Before the next cloud run, create the capsule and keep the archive off the Droplet.
- Remaining external blockers are public `linux/amd64` image publication, actual AMD cloud
  startup/latency receipt, and final merge to `main` after those proofs. Do not claim these are
  complete from local tests.

### Concurrent uncommitted work requiring explicit follow-up

`src/shelfwise_backend/agentic_cascade.py` currently has an uncommitted partial change adding
expiry/price scenario imports and Critic/Executive schemas. It does not yet add the corresponding
agentic runner functions, routes, result builders, or regression tests, and Ruff currently reports
unused imports for those additions. Do not stage it as complete. The next implementation pass
must either finish both agentic conditional checks end-to-end and add live-required tests, or
intentionally revert only that partial change after confirming it is not needed.

## Active Objective — Track 3 Prescreen

Track 3 requires all of the following:

1. A Docker image is present in the GitHub repository.
2. Container images are publicly pullable at submission time and include a `linux/amd64`
   manifest. A local image or a private registry image is not sufficient.
3. The deployed application demonstrably uses AMD compute. The production path must use
   AMD vLLM (`provider=vllm_mi300x`), not Fireworks or offline fallback.
4. The container is ready within 60 seconds after images are built.
5. Every request returns within 30 seconds.
6. Model responses are in English.
7. Answers are generated for unseen variants; there is no question-to-answer cache or
   hardcoded answer table.

The repository-side implementation is complete and locally verified. The dual-tier cloud benchmark
now proves model execution on the AMD host, but Track 3 still needs public image pullability and a
public-origin deployment receipt proving items 2-4 together. Do not mark the objective complete
until that receipt exists.

## Current Git State

- Current branch: `developers`
- Remote branch: `origin/developers`
- `main` has not been merged from the current work.
- Latest committed change before this handoff update: `4ee1abc fix: report submission proof boundaries honestly`.
- Existing untracked run artifacts are intentional evidence and must not be deleted or staged
  casually: `reports/`, `full_capacity_v2.log`, `backend_verify.log`, `tmp/`, dated run folders,
  `shelfwise-gemma-final-adapter/`, and stress-run folders.
- This update also adds two new files that must be committed: `scripts/track3_prescreen.py`
  and `tests/test_track3_prescreen.py`.

## Implemented Track 3 Gates

### Docker

- Root `Dockerfile` builds the backend as a non-root `appuser` and contains a Docker
  `HEALTHCHECK` against `/health`.
- `frontend/Dockerfile` builds the production frontend image.
- `docker-compose.production.yml` defines Postgres, Redis, migration, backend, and frontend;
  only the frontend publishes a public port.
- `/submission/readiness` now reports `docker_image_required: required`.
- The evaluation harness enforces that same value.
- Local image build was completed successfully:
  - `amdactii-backend:latest` (~402 MB)
  - `amdactii-frontend:latest` (~75.7 MB)
- Those local images are **not submission-ready**: they are not publicly pullable and their
  pushed registry location has not yet been assigned. Do not claim this requirement is done.
- The judging VM is `linux/amd64`. Every published image must be built and pushed with an
  explicit `linux/amd64` platform and must expose an amd64 manifest.
- Local Compose configuration passed. Local Compose services were not left running.
- CI builds images first, then measures `docker compose ... up --build -d --wait` and fails if
  readiness takes 60 seconds or more.

### AMD inference

- Production `APP_ENV=production` rejects providers other than `vllm_mi300x` with HTTP 503.
- Production chat requires live inference and cannot use offline fallback.
- All four agentic demo routes force `LIVE_REQUIRED` in production even if a caller requests
  `live_required=false`.
- Production inference smoke also requires AMD vLLM.
- Default production model configuration is routine `google/gemma-4-E4B-it` and strong
  `google/gemma-4-31B-it`; distinct endpoints/model IDs are required for
  `ready_for_amd_demo=true`.
- The model contract is OpenAI-compatible vLLM with Gemma tool calling. The application does
  not run local models and must not claim local GPU evidence as AMD evidence.

### Latency

- `LLM_TIMEOUT_SECONDS` is bounded by the remaining request budget.
- `SHELFWISE_REQUEST_TIMEOUT_SECONDS` defaults to 120 seconds; its outer middleware deadline
  protects the service while per-call inference budgets fail closed earlier.
- HTTP middleware returns 504 at the whole-request deadline, including multi-call agentic
  requests.
- CI measures post-build production topology readiness under 60 seconds.
- No actual cloud request latency receipt exists yet; the AMD run below is mandatory.

### English output

- Chat prompts explicitly require English.
- Chat output rejects clearly non-Latin responses.
- Agentic JSON payloads are recursively checked for clearly non-English writing systems.
- This is an enforcement guard, not a substitute for the final live response receipt.

### Unseen inputs and caching

- Chat persistence is keyed by `(tenant_id, user_id, conversation_id, message_id)`.
- Replay is limited to the exact message ID for idempotent retries.
- A different message ID does not replay an earlier answer; regression coverage is in
  `tests/test_track3_contract.py`.
- No question-to-answer cache exists.
- Production responses include `X-ShelfWise-Replayed`, correlation, provider, model, and answer
  source headers.
- Generated-world facts are data snapshots, not cached model answers.

## New Cloud Prescreen Command

`scripts/track3_prescreen.py` is the authoritative end-to-end probe. It performs the following:

- polls `/health` for at most 60 seconds;
- checks `/inference/readiness` for AMD vLLM and Google Gemma 4 routine/strong models;
- creates a session through `/auth/session`;
- sends two fresh unseen chat questions with unique conversation/message IDs;
- allows each chat response the documented live-model request budget (130 seconds in the
  prescreen, above the 120-second application deadline);
- requires `X-ShelfWise-Provider: vllm_mi300x`, `X-ShelfWise-Answer-Source: model`, and
  `X-ShelfWise-Replayed: false`;
- requires English-compatible output and unique correlation IDs;
- writes a JSON receipt when `--output` is provided.

Run it only after the AMD endpoint and production application are live:

```bash
python scripts/track3_prescreen.py \
  --base-url https://<public-app-origin> \
  --startup-deadline 60 \
  --request-deadline 130 \
  --output reports/track3_prescreen_<timestamp>.json
```

Expected result is `"verdict": "PASS"`. A configuration/readiness response without this
receipt is not proof of cloud startup or response latency.

## Public Image Packaging — Required Before Submission

This is a separate blocker from local Docker build success. Choose a public registry and a
stable public image namespace before submission. Do not put registry credentials in the repo.
For GHCR, the package visibility must be changed to **public** after the first push.

Build and publish both production images explicitly for the judging VM architecture:

```bash
export IMAGE_NAMESPACE=ghcr.io/<public-owner>/shelfwise
export IMAGE_TAG=<immutable-submission-tag>

docker buildx build \
  --platform linux/amd64 \
  --tag "$IMAGE_NAMESPACE-backend:$IMAGE_TAG" \
  --push .

docker buildx build \
  --platform linux/amd64 \
  --tag "$IMAGE_NAMESPACE-frontend:$IMAGE_TAG" \
  --push ./frontend
```

Verify the manifest and public pullability from a clean environment before submitting:

```bash
docker buildx imagetools inspect "$IMAGE_NAMESPACE-backend:$IMAGE_TAG"
docker buildx imagetools inspect "$IMAGE_NAMESPACE-frontend:$IMAGE_TAG"
docker pull --platform linux/amd64 "$IMAGE_NAMESPACE-backend:$IMAGE_TAG"
docker pull --platform linux/amd64 "$IMAGE_NAMESPACE-frontend:$IMAGE_TAG"
docker image inspect "$IMAGE_NAMESPACE-backend:$IMAGE_TAG" \
  --format '{{.Architecture}}'
docker image inspect "$IMAGE_NAMESPACE-frontend:$IMAGE_TAG" \
  --format '{{.Architecture}}'
```

Required evidence to save in the handoff/submission folder:

- public backend image reference and immutable tag;
- public frontend image reference and immutable tag;
- `imagetools inspect` output showing `linux/amd64`;
- clean `docker pull --platform linux/amd64` output;
- registry visibility confirmed without credentials;
- the exact image references used by the production Compose deployment.

Do not use `--load` as the final publication step. `--load` only places a local image in the
builder; `--push` is required for a publicly pullable submission image. Do not use an untagged
`latest` image as the only submission reference; retain the immutable tag for the judges.

## Exact Resume Procedure

1. Read this section and run `git status --short`; preserve all untracked evidence.
2. Commit the current prescreen implementation and this handoff update on `developers`.
3. Assign and publish the public `linux/amd64` backend/frontend images using the packaging
   procedure above. Verify clean pulls before spending AMD cloud credits.
4. Confirm the AMD cloud endpoint is on. For the existing MI300X vLLM droplet, first check
   `/v1/models`; do not assume a previous IP/process is still alive.
5. Configure production with distinct Gemma tiers, for example:

```bash
export APP_ENV=production
export SHELFWISE_AUTH_MODE=jwt
export LLM_ROUTINE_BASE_URL=http://<routine-amd-endpoint>:8000
export LLM_STRONG_BASE_URL=http://<strong-amd-endpoint>:8000
export LLM_ROUTINE_MODEL=google/gemma-4-E4B-it
export LLM_STRONG_MODEL=google/gemma-4-31B-it
export LLM_COMPUTE_RESOURCE="AMD Developer Cloud"
export LLM_ACCELERATOR="AMD Instinct MI300X"
export LLM_TIMEOUT_SECONDS=25
```

6. Build once, then measure startup separately from image build:

```bash
docker compose -f docker-compose.production.yml build
started=$(date +%s)
docker compose -f docker-compose.production.yml up --build -d --wait
elapsed=$(( $(date +%s) - started ))
test "$elapsed" -lt 60
```

7. Run `scripts/track3_prescreen.py` against the public origin and retain its JSON receipt.
8. Run the live-required full-system harness and inspect row-level receipts. Fail on offline
   answers, reused decision IDs, HITL mismatches, empty model answers, or zero model-backed
   chat calls.
9. Verify the browser frontend against the same live backend, then record the demo while the
   AMD endpoint is warm.
10. Before merging, run `python -m pytest -q`, `python -m ruff check src tests scripts`,
   `npm run typecheck --if-present` from `frontend/`, and `git diff --check`.
11. Only after public image pulls, the cloud receipt, and demo proof are saved, merge `developers` into `main` and
    verify both local and remote branch state. Do not delete evidence folders or force-reset
    either branch.

## Current Verification Baseline (2026-07-13, Plan 001)

- Full Python suite: pending the Plan 001 verification run in this checkout.
- Real Postgres world integration: `3 passed` against an ephemeral `pgvector/pgvector:pg16`
  container on local port `55433`.
- Ruff: clean.
- Frontend TypeScript typecheck and production build: previously passed; not rerun to completion
  in Plan 001.
- Capability manifest: regenerated from deterministic discovery (`178 capabilities`,
  `sha256:0b9a617e2c1c48a4ded6a1a706f1b4c79c977b4b0d9e1189c0790bc8707ac147`).
- Focused Track 3 prescreen test is present in `tests/test_track3_prescreen.py`.
- Workflow contract: optional live proof uses an environment shell guard; step conditions no longer
  inspect secrets directly.
- Final hybrid cloud receipt: `reports/soak/mi300x_hybrid_concurrency_fixed/benchmark.json` records
  `1,045` successful model calls across 1/8/32 stages with both E4B routine and 31B strong model IDs.
- Final 15-minute sequential E4B soak: `reports/soak_15m_retry/manifest.json` records `158/158`
  model-backed chats, `1,520` unique decisions, zero HITL mismatches, and `381` expected learning
  movements. It is product validation, not dual-tier capacity proof.

## Remaining Risks / Do Not Claim As Done

- The AMD cloud endpoint may be powered off or unreachable; verify it before spending credits.
- Public `linux/amd64` image publication is not complete until a public registry namespace is
  chosen, both images are pushed, and clean unauthenticated pulls succeed.
- The local Docker image build passed, but local CPU build/start is not AMD evidence.
- Actual container readiness under 60 seconds and actual model responses under 30 seconds need
  the cloud receipt.
- AMD-SMI host GPU/VRAM telemetry is not available from the provider; never invent utilization.
- The final hybrid receipt measures 1/8/32 concurrency against the live AMD endpoint; it is
  benchmark evidence, not a claim of broad production capacity.
- Routine/strong routing and two distinct serving endpoints are evidenced by the final hybrid
  receipt; public-origin readiness and judge-facing deployment still require the prescreen receipt.
- Catalog-price and expiry-risk guardrail proof routes now exist as
  `/scenarios/catalog-price/agentic` and `/scenarios/expiry-risk/agentic`. Normal ingest still keeps the
  deterministic guardrail functions for uptime; only the explicit `/agentic` route receipts should
  be claimed as model-agent evidence.
- Historical sections below may mention old branches, counts, IPs, or deadlines. Treat them as
  archival evidence only; this current section controls the next actions.


## EXECUTION CHECKLIST — Postgres-backed world (2026-07-12) — goal: kill all hardcoded seed data

User's explicit goal: the app must genuinely pull from Postgres, not seed CSVs / hardcoded
literals. "It doesn't matter if we have risk, make sure we test and we fix. Just implement."
This checklist is written BEFORE implementation per instruction. Tick items as they land.
Full research/design context: `IMPLEMENTATION_PLAN.md` TASK 4.

- [x] 1. Docker Desktop started; real Postgres running standalone on `localhost:5433` (compose's
      own 5432 was already taken by another project) with the actual `schema.sql` +
      `init_app_role.sh` init scripts applied (not mocks — genuine `psql`-verified 19 tables +
      `shelfwise_app` role). Gotcha hit and resolved: Git Bash/MSYS mangles any `/`-leading
      docker arg into a Windows path — every `docker exec`/`docker run` touching container
      paths needs `MSYS_NO_PATHCONV=1` prefixed, or the bind mount/exec silently no-ops.
- [x] 2. New table `shelfwise_world_snapshot` (tenant_id PK, seed, policy, generated_at,
      payload jsonb) in `src/shelfwise_storage/schema.sql`, RLS policy, added to
      `TENANT_SCOPED_TABLES` in `src/shelfwise_storage/rls.py`. Verified live: re-applied
      schema.sql against the running container, `tests/test_database_schema.py` passes.
- [x] 3. New store module `src/shelfwise_worldgen/world_store.py`:
      `InMemoryWorldSnapshotStore` / `PostgresWorldSnapshotStore` / `create_world_snapshot_store()`,
      same shape as `shelfwise_inventory/store.py` (get/save/clear, tenant-scoped). Verified
      live: real save+get round-trip against Postgres on :5433, confirmed missing-tenant
      returns `None`.
- [x] 4. New population service `src/shelfwise_worldgen/populate.py`:
      `GenerationPolicy` dataclass (seed, catalog_scale, assortment_size, min_near_expiry,
      min_low_stock, min_delayed_orders, min_price_anomalies) + `DEMO_POLICY` preset;
      `populate_world(policy, tenant_id, store) -> PopulationReceipt` — generates products via
      `shelfwise_worldgen.catalog.sample.sample_assortment`, derives stock/sales/suppliers/sites
      deterministically from the same seed, runs a guarantee pass that SELECTS which generated
      SKUs satisfy each constraint (never hardcodes which SKU), records the selection
      transparently in the receipt, writes through the store interface. Verified live against
      Postgres on :5433: 200 real generated products persisted, guarantee pass selected 2
      near-expiry / 5 low-stock / 2 delayed-supplier / 2 price-anomaly SKUs from the generated
      set (not hardcoded), hero SKU chosen dynamically. Determinism re-confirmed (same seed →
      byte-identical receipt).
- [x] 5. New facts provider `src/shelfwise_backend/world_facts.py`: `WorldFactsProvider` with
      `get_scenario_facts(tenant_id, sku)`, `get_store_intelligence(tenant_id)`,
      `get_sourcing_candidates(tenant_id, sku, units_needed)`, `search_products(tenant_id, query,
      limit)`, `get_hero_sku(tenant_id)`. Lazy-populates a tenant's snapshot on first access
      (via `DEMO_POLICY`) if none exists yet, so zero-config flows keep working. Every method
      round-trips through the store (real query per call, no long-lived cache) — Postgres must
      genuinely be hit per request, not just at boot. Verified live against Postgres: hero-sku
      lazy population, scenario facts, full store_intelligence (batch split, delivery
      reconciliation, supplier cover, stock sourcing, learning summary), sourcing candidates
      (branches correctly fall through to supplier when they have no stock for that SKU), and
      product search all produced coherent, non-hardcoded, genuinely-computed results.
- [x] 6. Rewire call sites — ALL DONE, verified live against real Postgres:
      - [x] 6a. `mcp_surface.py` — all 8 tools now call `facts.get_scenario_facts`/
        `facts.get_sourcing_candidates`; `build_platform_tools` takes required `facts` +
        `tenant_id` params (moved tenant_id to build-time, not per-call). Also fixed two
        pre-existing hardcoded-input bugs found along the way: `get_reorder_policy` was
        ignoring the real scenario (hardcoded on_hand=20/avg_daily_demand=10/lead_time=3);
        `get_supplier_ranking`'s "backup supplier" was a hardcoded literal — both now derive
        real numbers via `facts`/`get_alternate_supplier`.
      - [x] 6b. `product_catalog.py` — fully rewritten: `_world_product_items` merges
        `facts.list_products`/`facts.list_stock`; dropped the old CSV+synthetic-generator
        blend entirely (the generated world already has hundreds of real products, no
        separate "synthetic filler" layer needed). `tenant_id` required, no default.
      - [x] 6c. `cascade.py` — all 5 cascades (golden/procurement/sales/cold-chain/
        critic-rejection) use `facts.get_scenario_facts`/`get_store_intelligence`; each has
        its own `_default_facts()` lazy singleton for callers that don't inject one.
      - [x] 6d. `agentic_cascade.py` — all 4 agentic cascades take `facts:
        WorldFactsProvider | None`, same lazy-default pattern. Found and fixed 3 real
        `F821 undefined name 'tenant_id'` bugs in `_build_result`/`_build_procurement_result`/
        `_build_sales_result` (leftover from the tenant-id threading refactor) — now all
        correctly use `event.tenant_id if event is not None else
        default_tenant_context().tenant_id`, matching the cold-chain one that was already
        correct.
      - [x] 6e. `app.py` — `world_snapshot_store`/`world_facts` module-level singletons
        wired alongside every other `create_*_store()`; readiness, `/data/seed/summary`,
        `/products/*`, `/tools/platform`, and the chat route all pass `facts=world_facts`.
        Route paths unchanged.
      - [x] Bonus: found and fixed stale evidence-source labels in `cascade.py` that still
        said `"stock.csv"`/`"sales.csv"`/`"products.csv"`/`"suppliers.csv"` in the decision
        evidence trail even after the data source changed — a judge reading the evidence
        would have seen literal CSV filenames and concluded nothing had changed. Now all say
        `"generated_world"`.
- [x] 7. `world_snapshot_store`/`world_facts` wired into `app.py` startup (see 6e above).
- [x] 8. Test fixes — 26 initially-broken tests (mostly hardcoded `"4011"` SKU literals in
      event payloads that no longer resolve in the generated world) fixed via a shared
      `tests/_world_test_support.py` helper (`demo_sku()`/`demo_facts()`) resolving a real
      generated SKU instead. Files touched: `test_tenant_auth.py`, `test_detective.py`,
      `test_connector_intake.py` (also fixed a stale hardcoded "30.00" price assertion that
      no longer matched the real generated catalogue price), `test_backend_observability_tools.py`
      (removed a stale `on_hand == 240` literal assertion), `test_golden_cascade.py`'s
      profit assertion (see the populate.py fix below). `test_product_catalog_api.py` had
      already been rewritten for the new API shape by earlier work; removed one now-dead
      `_synthetic_product` helper left over from the old CSV+synthetic-blend design.
      One real generation-logic gap found and fixed: the generated hero SKU had no
      guarantee its markdown would actually be profitable (the old CSV "planted story" had
      guaranteed this implicitly). Added `_prefer_profitable_markdown` to `populate.py` —
      reorders the near-expiry candidates so a genuinely profitable one (verified via the
      real `simulate_markdown` function, not a hardcoded guarantee) leads and becomes
      `hero_sku`. Two pre-existing, unrelated test failures (`test_default_tenant_context_matches_demo_tenant`
      and a couple in `test_mlops.py`) turned out to be a red herring from running single
      test files outside the full suite — `conftest.py` forces `SHELFWISE_TENANT_ID=sa_retail_demo`
      and only applies correctly when pytest's own conftest discovery runs, not in raw
      `python -c` reproductions; all pass in the full suite.
- [x] 9. Real Postgres verification — genuinely done, not skipped. Stood up a real Postgres
      16 container (`docker run pgvector/pgvector:pg16`, real `schema.sql` +
      `init_app_role.sh` init scripts, restricted `shelfwise_app` role, not the superuser)
      on `localhost:5433` since the docker-compose default port 5432 was already taken by
      an unrelated project. **Gotcha:** Git Bash/MSYS mangles any `/`-leading argument
      (including `-v host:/container/path` and `docker exec ... /path`) into a Windows path
      — every such command needs `MSYS_NO_PATHCONV=1` prefixed or the mount/exec silently
      no-ops with no error. Booted the real FastAPI backend with
      `SHELFWISE_STORE_BACKEND=postgres` + `SHELFWISE_AUTO_SCHEMA=false` (schema already
      applied) and drove real HTTP requests through it: `/data/seed/summary` (lazy-populated
      a 200-product world into Postgres on first hit), `/products/search`, and all 4
      deterministic cascades (`/scenarios/golden`, `/scenarios/procurement`, `/scenarios/sales`,
      `/scenarios/cold-chain`) — all produced genuine, non-hardcoded results. Confirmed via
      direct `psql` query that the decisions (6 rows) and the world snapshot (200 products)
      are real persisted rows in Postgres, not in-process state.
- [x] 10. Added `tests/test_postgres_world_integration.py` — 3 tests gated on
      `SHELFWISE_TEST_DATABASE_URL` (skip cleanly without it, verified both ways): a real
      `populate_world` round-trip through Postgres, `WorldFactsProvider` reading from a real
      connection, and tenant isolation between two snapshot rows. The fixture auto-forces
      `SHELFWISE_AUTO_SCHEMA=false` so it only needs the one env var to work against the
      restricted app role. Follow-up closed in this continuation: CI now boots an ephemeral
      `pgvector/pgvector:pg16` container with the real schema and restricted app role, then
      runs the test with `SHELFWISE_TEST_DATABASE_URL`.
- [x] 11. Full suite green: 444 passed, 3 skipped (the new Postgres integration tests
      without the env var) — zero failures. Ruff clean. Capability manifest regenerated
      (175 capabilities). Follow-up closed in this continuation: README, DEMO_RUNBOOK, and
      IMPLEMENTATION_STATUS now describe the generated-world/Postgres model instead of the old
      seeded-CSV framing.
- [x] 12. Commits landed incrementally per phase (schema+store+populate, facts provider,
      call-site rewiring, evidence-label fix, test fixes, integration test) — see git log
      on the `developers` branch. This entry is that final summary update.

**Bottom line: the app now genuinely pulls from Postgres.** No more hardcoded CSV seed data
or literal demo fixtures anywhere in the live request path — `load_seeded_scenario`/
`build_store_intelligence_demo` are no longer called from any production code path (only
`shelfwise_data`'s own internals/tests still reference them, which is fine — they're the
low-level building blocks the old CSV loader was built from, now superseded).
**Follow-ups closed in the 2026-07-12 continuation:** CI now runs the Postgres world
integration test against a real pgvector container, and README/DEMO_RUNBOOK/IMPLEMENTATION_STATUS
now describe the generated-world model instead of the old seeded-CSV framing.

## Coordination note (2026-07-11 ~12:00) — judge-readiness pass on main, doc-only

While the public-demo/dual-model branch (`codex/public-demo-dual-model-readiness`, PR #2)
was in progress, a docs-only judge-readiness pass landed directly on main. **No code files
were touched** — nothing in `src/`, `frontend/src/`, `tests/`, or `capabilities/` changed,
so PR #2 should merge without conflicts. What landed on main:

- `README.md`: rewrote the stale top section (now leads with the agentic MI300X system and
  an explicit "Built on AMD" proof section), fixed em-dash mojibake that rendered as garbage
  on GitHub, corrected Inference Strategy to state only MI300X/vLLM was used, refreshed
  Current Scope/Next lists. The `Connected API endpoints:` list and `## Smoke` heading are
  untouched (test contract).
- `DEMO_RUNBOOK.md`: Three-Minute Story now matches the recorded demo; Cloud Proof section
  now documents the real MI300X-only deployment and droplet restart runbook.
- `reports/soak_15min_20260711T042648Z/`: committed the compact soak receipts (summary,
  feature receipts, chat samples, cycles) that the README cites.
- `reports/SUBMISSION_EVIDENCE_REPORT.md` + `reports/ORIGINAL_PROBLEM_COVERAGE.md`:
  committed (README linked them but they were untracked = dead links).
- `submission/`: slide deck PDF + 16:9 cover image used in the hackathon form.
- GitHub repo description + topics set (amd, mi300x, vllm, rocm, gemma, agentic-ai, ...).


## Latest update — real multi-source stock sourcing decision (not a bare transfer number)

User's specific complaint, verbatim: chat was recommending "transfer 18 units now" with
no logic behind *where* that stock comes from. Correct - `plan_supplier_cover` (the old
function backing this) took a single caller-supplied `transfer_available_units` number
and just did `min(gap, that_number)`; there was never any real sourcing comparison.

Added `plan_stock_sourcing` (`src/shelfwise_decision_science/sourcing.py`, new, tested):
given a shortage and a set of candidate sources (nearby branches, the regional
distribution centre, approved suppliers), it filters to sources with any stock, ranks by
lead time then distance then cost, selects the best, and explains *why* in the
conclusion text (e.g. "chosen over branch store_09_midrand (4.00h away) for a faster
delivery"). If nothing has stock, it recommends a purchase order with a stated reason
instead of a transfer. If the best source only partially covers the shortage, it says so
and recommends a PO for the remainder rather than silently under-covering it. 7 new unit
tests cover the ranking, tie-break, partial-cover, and no-source-available paths.

Wired in two places: (1) a new read-only platform tool `get_stock_sourcing_options` so
live chat can call it for any SKU/shortage - chat's system prompt now explicitly forbids
recommending a stock transfer without calling this first; (2) an additive
`"stock_sourcing"` field in `build_store_intelligence_demo()` so the same reasoning
grounds answers even without a live tool call (existing `supplier_cover` field is
untouched, so nothing in the frontend UI card broke).

**Verified live against the real model** - asked "we are short on SKU 4011, where should
the replacement come from?": it named the specific branch (store_02_sandton), cited real
distance/lead-time figures (5.00 km, 2.00 hours), explained why that branch beat the
alternative, and correctly flagged a purchase order for the 12-unit uncovered remainder.
Confirmed both via curl and an actual browser round-trip, zero console errors.

415/415 tests pass, capability manifest regenerated, ruff clean. Commit `938c9e1`.
Backend restarted (no `--reload`, same gotcha as always) to pick this up before
verifying live.

**Known scope limit, honest for the deck**: the branch/DC/supplier network (distances,
lead times, stock levels) is deterministic seeded demo data for SKU 4011, same pattern as
every other demo fixture in this codebase (delivery reconciliation, supplier ranking,
etc.) - it is not a live multi-branch inventory feed. The *decision logic* is real and
general (works for any candidate set you hand it, has its own unit tests independent of
the demo data); the *data* behind today's demo is fixture data, same honesty bar as
everything else already flagged in "Known honest gaps" below.

## Prior update — chat is now genuinely agentic across the whole store + markdown formatting

User's ask: chat needs to read cleanly (not dense paragraphs) AND be able to talk about
"every little thing in our application" (stock, procurement, cold-chain, pricing,
approvals, learning), not just the one product/delivery slice it happened to be told
about. Two real changes, both live-verified, not cosmetic:

1. **Chat is now a real tool-calling agent**, not a single static-state completion.
   `build_chat_reply_with_meta` in `chat.py` now runs through the same
   `AgentOrchestrator` + read-only `PlatformToolRegistry` the production cascades use -
   11 tools: `get_stock`, `get_demand_forecast`, `get_expiry_risk`, `get_reorder_policy`,
   `get_supplier_ranking`, `get_cold_chain_status`, `check_price_integrity`,
   `simulate_markdown`, `list_open_decisions`, `explain_decision`, `get_thresholds`. The
   model decides which to call per question - verified live calling 2-4 tools in a single
   turn for a "give me a report" question (approvals, stock, delivery reconciliation,
   supplier cover all correctly cited with real numbers in one answer). Every answer is
   grounded the same way cascades are (`assert_conclusion_grounded_in_tool_results`) - a
   computed number a tool returns must be cited or the run is rejected. Tenant isolation
   carries through automatically (`trusted_overrides={"tenant_id": ...}` is already
   applied per tool call inside `AgentOrchestrator.run_messages`, using whatever
   `tenant_id` chat passes in - no new code needed there). Falls back to the original
   single-completion path when no decision/memory store is supplied (keeps
   `test_gateway_security.py`'s prompt-injection test working completely unchanged - it
   doesn't pass a store, so it exercises the old path on purpose) and to the offline
   reply when live inference is unavailable or fails.
2. **Chat renders real markdown now.** Added `react-markdown` + `remark-gfm` (both MIT,
   free) and switched `AssistantBubble` in `App.tsx` to render through them, with a new
   `.bubble .md` CSS block in `index.css` styling headings/bullets/bold/code/tables for
   the existing dark/light themes. System prompt in `chat.py` explicitly asks for
   headings + bullets + bold-the-key-numbers on multi-part answers, short paragraphs for
   single facts. Verified live in-browser: a "give me today's report" question rendered
   as real `<h3>`/`<ul>`/`<strong>` elements, not one text blob - screenshot confirms
   clean structured output, zero console errors.

408/408 tests pass, capability manifest regenerated, frontend `tsc --noEmit` clean.
Commit `e3a84f4`. Backend was restarted (no `--reload`, same gotcha as always) to pick
this up before verifying live.

**Known limitation, honest gap for the deck**: the model sometimes declines to call a
tool it lacks a required argument for (e.g. asked "how's the cold chain?" with no area
named, it said plainly it didn't have that data rather than guessing an `asset_id`) -
this is correct grounded behavior, not a bug, but means very vague questions get an
honest "I don't have that specific data" instead of a guess. Not fixed further given
remaining time - a real fix would mean giving tools sensible default-area resolution,
which is a bigger, separate task.

## Historical update — 15-min live soak test PASSED + a real chat bug found and fixed by screen-testing

Ran the actual `shelfwise_eval.full_system` harness for 15 real minutes against the live
droplet with `--live-required` (any offline chat fallback would hard-fail the whole run,
unlike the old v2 marker this file already flagged as invalid). Result: **PASSED, zero
failures.** 333 world cycles, 333/333 chat calls model-backed (0 offline, 0 errors), 4,618
decisions all unique, 2,934 approvals / 56 rejections with 0 HITL mismatches, 34/34 expected
learning movements landed. Artifact: `reports/soak_15min_20260711T042648Z/manifest.json`
(also `.log` next to it). This is the strongest evidence yet that the chat-scaling and
offline-fallback bugs fixed earlier this session hold up under sustained real load, not just
in isolated tests.

While screen-testing chat right after, found a real, user-visible bug: asking "deliveries
issue" returned the literal string `The tool result for the subject "deliveries issue" is
`null`.` - the live model dumping a raw null tool result instead of answering. Root cause:
`_new_chat_response` in `app.py` only ever gave chat the product-catalog search result plus
decisions/learning/traces - it had **zero visibility into delivery reconciliation, supplier
cover, or FEFO batch data**, even though that exact data (`build_store_intelligence_demo()`
from `shelfwise_data`) already powers the "Deliveries / To order / Sell first" sidebar tiles
the user was looking at when they asked the question. Fixed two ways: (1) added
`"store_intelligence": build_store_intelligence_demo()` to the chat state dict so real
answers are possible, (2) hardened the chat system prompt in `chat.py` to explicitly forbid
describing raw tool_results/state_json shape and require a natural-language answer, falling
back to whatever real state exists rather than describing an empty result. Verified live -
same question now returns "the order was for 50 units, but we only received 38... short 12
units, and the supplier fill rate was 76%..." - both via curl and an actual browser
round-trip, zero console errors. 408/408 tests pass, capability manifest regenerated. Commit
`909f42e`.

**This was found by actually using the product as a user would, not by reading code or
running the harness** - a reminder that live click-testing catches gaps that pass every
automated check (the harness above passed 100% right before this bug was found, because the
harness's own chat questions are template-generated product questions that happen to always
have a catalogue match).

## Prior update — agentic cascades are now clickable in the UI (not just curl-testable)

Found and closed a real gap: the 4 agentic routes below existed and worked, but were only
listed in a read-only catalog in the Operations workspace - no way to see one run without a
terminal, which would have forced the demo video to cut to curl output for the single most
impressive capability in the app. The "Gated operational endpoints" list's 4 `/agentic` rows
are now real buttons: click one, it shows a live "calling the live Gemma tool-calling
loop..." state, then the row's detail replaces with the actual result (conclusion, routed
action, real model-call count). Verified live in-browser for all four, zero console errors:
golden, procurement, sales, cold-chain all produced genuine results from the real MI300X
endpoint through an actual click, not a fixture.

While verifying this, found the running local backend (started earlier this session) was
serving stale code from before the sales/cold-chain routes existed (started without
`--reload`) - restarted it, confirmed all four resolve now. **If you restart the backend
again, remember it does NOT auto-reload** - `set -a && source .env && set +a && python -m
uvicorn shelfwise_backend.app:app --host 0.0.0.0 --port 8000 --app-dir src` from repo root.
Frontend: `npm run dev` in `frontend/`. Both currently running and healthy alongside the
live droplet as of this handoff.

Where to see it: Operations workspace (sidebar → "Operations") → scroll to "Gated
operational endpoints" → the four rows ending "(agentic) - click to run live".

## Prior update — 4 of 5 production cascades are now genuinely agentic

User goal: "fix all if you can." Extended the proven golden-cascade pattern to procurement,
sales, and cold-chain. All four now have a real Gemma tool-calling path, verified live:

- `POST /scenarios/procurement/agentic` — Critic calls `get_reorder_policy` +
  `get_supplier_ranking`, cites real reorder quantity (23.70 units) and measured supplier
  choice; Executive routes reorder/monitor.
- `POST /scenarios/sales/agentic` — Critic calls `check_price_integrity` against a deliberately
  mismatched till price (20% over catalogue, outside the deterministic cascade's own 15%
  tolerance); genuinely caught the exception (36.0 vs 30.00, delta 6.00) and routed to
  manager review.
- `POST /scenarios/cold-chain/agentic` — Critic calls `get_cold_chain_status` for a measured
  refrigeration alert; routes dispatch/monitor based on the real measured risk figure.

Each is additive - the original deterministic routes (`/scenarios/procurement`, `/scenarios/sales`,
`/scenarios/cold-chain`) are unchanged and still work. Each new route defaults `live_required`
so a broken endpoint 503s instead of silently faking success. 408/408 tests pass (12 new
tests: 3 cascades x offline-success/live_required-hardfail/ungrounded-rejection).

**Closed in the 2026-07-12 continuation**: the two smaller conditional checks now have explicit
agentic proof routes, `/scenarios/catalog-price/agentic` and `/scenarios/expiry-risk/agentic`, backed by
regression tests for real tool calls, live-required hard-fail behavior, and grounded conclusions.

While building this, fixed a real precision bug in the calculator-grounding check below
(it required citing bare echoed identifiers, e.g. a SKU digit, not just genuinely computed
values) - see that section for detail.

## Prior update — enforced calculator-grounded reasoning across every agent

User's explicit requirement: agents must use tools as their calculator for any math, and
must be able to genuinely explain the math (cite real figures), not just assert a verdict.
This was previously only a prompt instruction ("never invent numbers") with no verification.

Added `extract_salient_numbers`/`assert_conclusion_grounded_in_tool_results` in
`tool_calling.py`: after any agent run, checks that the final conclusion text actually cites
at least one real numeric value from each tool it called, raising `UngroundedAnswerError`
(a `ToolCallingError`, so existing failure handling already covers it) if not. Wired into
the golden cascade's Critic verdict and all 11 roles in `agent_role_coverage.py`. The shared
`guarded_system` text in `AgentOrchestrator.run` now tells every caller "tools are your
calculator... cite the specific figures," so this applies automatically to any future agent
wiring too, not just these two call sites.

**Verified live against the real MI300X endpoint: 11/11 agent roles pass with grounding
enforcement active** — every conclusion now genuinely cites real computed figures (e.g.
"incremental profit of 109.44 ZAR", "240 units on hand", "0.58 cold-chain risk", "41.04
units demand forecast"). 399/399 tests pass (2 new tests added: positive + negative
grounding cases). Commit: check `git log --oneline -1` on this branch.

## Prior update — frontend E2E verified, droplet is LIVE not off

Despite the prior note saying "user turned the droplet off," `/v1/models` and `/health` on
`165.245.130.225:8000` both returned 200 with `google/gemma-4-E4B-it` loaded when checked
just now. **It is live and billing right now** — either it was never actually stopped or it
was restarted without a status update reaching this file. Verify current state before
assuming either way.

**Frontend end-to-end against the live backend is now VERIFIED** (the biggest previously-
untested demo risk is closed): started `uvicorn shelfwise_backend.app:app` with `.env`
loaded + the Vite dev server, drove the actual UI in a browser.
- Chat: typed a real question in the UI, got a real answer; confirmed via direct curl that
  `/chat` responses carry `x-shelfwise-answer-source: model`, `x-shelfwise-model:
  google/gemma-4-E4B-it`, `x-shelfwise-provider: vllm_mi300x`, `x-shelfwise-replayed: false`.
- HITL: clicked Approve on one pending decision (confirmation dialog → "Yes, apply it" →
  real `POST /decisions/{id}/approve` → 200, UI updated to "Approved... 1 approval still
  waiting"), then Reject on the other (→ `POST /decisions/{id}/reject` → 200).
- Zero browser console errors throughout.
- Both servers were left running (not stopped) so the next session can go straight to
  recording. Backend log: `backend_verify.log` in repo root (gitignored, harmless to delete).

A Haiku-model read-only audit (to save tokens) compared the original plan docs
(CLAUDE.md, plot/domains/*.md, README.md, capability manifest) against actual code. Full
coverage matrix was reported in-session; headline findings:
- Confirms what was already known: only the golden cascade's Critic/Executive run through
  real Gemma reasoning; procurement/sales/cold-chain cascades are deterministic math only.
- Flagged that Postgres RLS policies would be bypassed if run under a superuser DB role —
  **verified this is NOT currently relevant**: `.env` has `SHELFWISE_STORE_BACKEND=memory`,
  so no Postgres/RLS is in the loop for the current demo deployment at all. Only matters if
  the Postgres profile is ever actually used — note as a known gap for that profile, don't
  chase it now.
- Batch/lot expiry is now represented in the generated-world snapshot: perishable SKU rows have
  two or three active lots with receipt, expiry, quantity, and source-confidence fields; FEFO reads
  those lots and preserves compatibility with earlier aggregate-only snapshots. The repeatable
  `scripts/fleet_scale_eval.py` proof streams 500,000 product-location-lot rows in 1,000-row chunks,
  retains only the top 200 exceptions, and writes a receipt. Persisting score history/deltas for a
  real retailer remains a production data-layer follow-up, not a gap in the demo's scale proof.
- Historical routing snapshot: dual-model routing was code-complete
  (`base_url_for_agent`/`api_key_for_agent`) but only one model endpoint was deployed
  (`dual_model_configured: false` confirmed live); the final hybrid receipt above supersedes it.

Two more commits landed this session on top of the prior handoff (chat multi-user identity,
dual-model routing config) — see updated commit log below.

## Critical correction — verified after the original handoff

The v2 run finished with an original `FULL_CAPACITY_V2_PASSED` marker, but current
revalidation correctly marks it **failed**: only 2 of 51 chats were model-backed and 49
silently used the offline fallback. The immutable correction receipt is
`reports/full_capacity_v2_revalidation.json`. The harness now requires every chat in a
`live_required` run to be model-backed, rejects offline answers and chat errors, and can
revalidate historical runs with `scripts/validate_full_system_artifact.py`.

Root cause: HITL resolution happened after the 15-minute rotation while `/chat` injected every
pending decision into its prompt. The store remains complete, but chat context now carries
aggregate counts plus bounded pending/resolved windows. `live_required` chat returns 503 instead
of falling back.

Infrastructure correction: the `rocm` container was found `Exited (137)` and the endpoint refused
connections. A container restart was issued, after which ports 22 and 8000 stopped responding.
Treat the droplet as **unreachable** until the cloud dashboard and `/v1/models` prove otherwise.

**Deadline: TODAY 2026-07-11, 6pm CET. Submission requires: repo + demo video + slide deck,
and the work MUST be merged to `main` before submitting. Do not leave the merge to the last hour.**

## Where we are

Branch: `codex/gemma-full-system-integration` (all work committed, working tree clean except
untracked run-artifact dirs: `20260710T*/`, `reports/`, `shelfwise-gemma-final-adapter/`,
`stress_run_*/`, `data/harness_runs/`, `full_capacity_v2.log`).

Latest commits (newest first):
- `6965473` route routine/strong agent tiers to independently configured model endpoints
- `45fec59` chat multi-user: conversation/message identity, idempotent replay, tenant isolation
  (this commit also swept in the full_system.py stricter live_required audit + revalidation
  script + HANDOFF.md, since they were pre-staged when committed - all content is real and
  tested, the commit message just under-describes scope; not worth rewriting history over)
- `5b30d15` tenant-isolation fix + full 11/11 tool coverage
- `561c50b` bound /chat state (unbounded-prompt scaling bug)
- `c615399` drop strict json_schema decoding (vLLM/Gemma whitespace-loop bug) + 11-role harness
- `afb7c8c` force tool_choice=required on opening agent call
- `c7fbdaf` wire golden cascade Critic/Executive through real Gemma tool calling
- earlier: merge of gpu-notebook-testing, docker-compose env_file fix

394/394 tests pass. `python -m pytest -q`, `python -m ruff check .`, and the production frontend
build pass. Capability manifest is in sync (`python scripts/compare_capability_manifests.py --write`
regenerates it after any route/tool/test change — the contract test fails when stale).

## Live infrastructure (BILLING: $1.99/hr — shut down when done!)

AMD Developer Cloud MI300X droplet: `165.245.130.225` (SSH worked earlier from this machine,
key `~/.ssh/id_ed25519`). Inside it, Docker container `rocm` previously ran vLLM 0.23.0 (ROCm)
serving `google/gemma-4-E4B-it` on port 8000 with the Gemma tool parser. `.env` points at that
endpoint, but configuration/readiness metadata is not proof that the process is currently live.

**Restart runbook if the droplet/pod restarts:**
```
ssh root@165.245.130.225
docker start rocm
docker exec rocm bash -c 'nohup vllm serve google/gemma-4-E4B-it --host 0.0.0.0 --port 8000 \
  --enable-auto-tool-choice --tool-call-parser gemma4 > /tmp/vllm_serve.log 2>&1 &'
# wait ~7 min (torch.compile) then: curl http://165.245.130.225:8000/v1/models
```
HF auth is already done inside the container (user keorapetswe; Gemma license accepted).
Model weights are cached in the container (~15GB). The Jupyter hackathon notebook portal
(notebooks.amd.com) is DOWN for maintenance — W7900 training shakedown blocked on that.

## Historical live verification record (superseded by Current Verification Baseline)

- `/inference/smoke`, `/chat`, `POST /scenarios/golden/agentic` all hit real MI300X Gemma.
- 11/11 agent roles + 11/11 platform tools genuinely exercised by real Gemma tool calls:
  `python -c` runner in `src/shelfwise_eval/agent_role_coverage.py` (needs `.env` sourced).
- Full-system world sim 15-min run v1 PASSED (145 cycles, 3152 decisions, unique IDs, zero
  HITL mismatches) but exposed the /chat unbounded-state bug (2/49 model answers) — FIXED.
- V2 completed 151 cycles and preserved decision/HITL/learning integrity, but its live-chat
  requirement failed under current rules: 2 model answers and 49 offline fallbacks. Its old pass
  marker is superseded by `reports/full_capacity_v2_revalidation.json`.

## 8 real bugs found by live testing this session (all fixed + regression-tested)

1. docker-compose loaded `.env.example` (blank creds) instead of `.env` → silent offline mode.
2. `tool_choice="auto"` → Gemma skipped tools entirely, then emitted degenerate output.
3. Raw `InferenceError` leaked through the orchestrator instead of typed failure.
4. Strict `json_schema` response_format → infinite whitespace loop on vLLM/Gemma-4 (NOT a
   token-budget issue; proven with max_tokens 800 vs 4000). Now: text mode + schema-in-prompt
   + post-hoc validation. NEVER re-enable strict json_schema against this endpoint.
5. `FinalAnswerValidationError` not caught → batch crash instead of per-role failure.
6. `/chat` sent unbounded decision/learning history → prompt growth → timeout → silent
   offline fallback after ~cycle 6 of a long run. Context is now bounded without deleting state.
7. **Tenant-isolation hole**: Gemma invented `tenant_id="default_tenant"` in tool args and
   the tool honored it. Now `trusted_overrides` in `PlatformToolRegistry.execute` forces the
   caller-authenticated tenant over any model-supplied value.
8. The v2 harness accepted one model answer as sufficient for a `live_required` run, allowing
   49 offline fallbacks to pass. It now requires model answers to equal chat calls and supports
   historical artifact revalidation.

## NEXT STEPS, in priority order (the plan we were executing)

1. ~~Restore/verify the droplet~~ DONE this session - it's live (`165.245.130.225:8000`,
   `google/gemma-4-E4B-it`). Just confirm it's still up before recording (`curl
   http://165.245.130.225:8000/v1/models`) since availability has flip-flopped already.
2. ~~Frontend end-to-end against the live backend~~ DONE this session - chat (real model
   answers, verified via response headers), HITL approve, and HITL reject all confirmed
   working through actual browser clicks against the live backend, zero console errors.
   Both servers were left running: backend on :8000 (`uvicorn`, `.env` loaded), frontend on
   :5173 (`npm run dev` / vite). If either died, restart: backend -
   `set -a && source .env && set +a && python -m uvicorn shelfwise_backend.app:app --host
   0.0.0.0 --port 8000 --app-dir src`; frontend - `npm run dev` in `frontend/`.
3. **Record the demo video now, while the droplet is hot and the app is verified working.**
   This is the top remaining priority - everything else is secondary to actually capturing it.
4. **Merge this branch to `main`** (required for submission).
5. Only if time remains, in priority order:
    a. ~~Deploy a second model on a second endpoint and set `LLM_STRONG_BASE_URL`/
       `LLM_STRONG_API_KEY`~~ DONE in the final hybrid receipt; retain the public-origin
       prescreen as the remaining deployment proof.
   b. ~~Wire the two smaller conditional checks (`run_catalog_price_check`,
      `run_expiry_risk_check`) through the agentic pattern too~~ DONE in this continuation via
      `/scenarios/catalog-price/agentic` and `/scenarios/expiry-risk/agentic`.
    c. ~~Run `shelfwise_benchmark` at 1/8/32 concurrency against the live endpoint for the
       architecture-comparison report.~~ DONE; receipt is
       `reports/soak/mi300x_hybrid_concurrency_fixed/benchmark.json`.

## Historical known gaps (archived; do not overclaim in the deck/video)

- UPDATE: golden, procurement, sales, cold-chain, catalog-price, and expiry-risk proof routes are
  now genuinely agentic (`/scenarios/{golden,procurement,sales,cold-chain,catalog-price,expiry-risk}/agentic`,
  `live_required` default). The original deterministic routes/functions are still present for the
  normal ingest path and should not be described as model agents unless an explicit `/agentic`
  route receipt proves it.
- Training matrix snapshot: E2B/12B W7900 shakedown was blocked (Jupyter portal down); at that
  time only E4B was live. The current dual-tier receipt above supersedes the serving-topology claim.
- Benchmark architecture comparison (shared/replicated/per-agent/hybrid) is built + tested
  offline but has no real cloud measurements yet.
- Historical serving snapshot: only google/gemma-4-E4B-it was deployed; the final hybrid receipt
  above now records separate E4B routine and 31B strong serving endpoints.
- Batch/lot expiry state and a repeatable 500k synthetic scoring proof are implemented in this
  worktree. Do not overclaim it as a production retailer data platform: score-history persistence,
  real-source ingest, and live operational scale measurements still need their own deployment proof.
- Postgres RLS policies exist in `schema.sql` but are irrelevant to the current demo
  deployment (`SHELFWISE_STORE_BACKEND=memory` - no Postgres in the loop at all); only
  matters if/when the Postgres profile is actually used in a future deployment.
- MI300X operator-side AMD-SMI telemetry: not collected (provider gives no host access);
  report as missing evidence, never estimated. vLLM /metrics IS available on the droplet.

## House rules (unchanged, binding)

No AI attribution anywhere (commits/PRs). Free-tier/open-source only. Cloud inference only
(MI300X/vLLM + Fireworks fallback) — never local models. MIT-clean deps. No temporary fixes.
Read `CLAUDE.md` for the full mandate (full MVP, not a demo slice).
