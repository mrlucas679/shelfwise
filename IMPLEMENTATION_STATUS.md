# Implementation Status — Full Application Inventory

> **Working-product branch boundary:** This status describes the post-hackathon implementation
> on `developers`. Keep changes on `developers`; `main` is the protected working-product branch
> and is not an accidental commit target.

Date: 2026-07-28 (supersedes the 2026-07-23 update below; that entry is kept as history)
Branch: `developers` · Final local gates: **943 passed / 21 environment-gated skips**;
Ruff clean; frontend `tsc --noEmit` clean; frontend production build clean;
capability manifest **248 capabilities**, contract-verified
(`sha256:457267630...`); Playwright golden path **11/11 passed** against isolated disposable
ShelfWise servers using frontend port 5187 and backend port 8017. The project `.venv` and
installed frontend toolchain ran these gates directly.
GitHub CI on implementation commit `e656d1d` passed **927 tests / 1 skip** with fresh
Postgres/Redis, the distributable wheel, the production Compose topology, and the deployment
shakedown. Live AMD-model proof was not run because that CI environment has no endpoint
credentials; this remains explicitly external rather than being inferred from the green build.

## 2026-07-28 one-command startup and self-serve connection completeness

- ✅ `scripts/start_shelfwise.py` provisions a shop, starts the Compose stack, waits for a
  real health signal, and prints the console URL and first-login credentials in one command.
  Re-running never regenerates existing secrets, which would otherwise make stored connector
  credentials undecryptable and lock the owner out. A real run proved the .env-preservation
  guard and the Docker-failure path; the successful-start path remains unproven because this
  machine's disk is full and its Docker engine is unresponsive. See HANDOFF.md.

- ✅ All nine supported source systems are now owner-connectable from the product with no
  developer step. Poll-based ERPs use stored credentials with a live connection test;
  Shopify, Square, Lightspeed, and Yoco use per-tenant signed webhook endpoints provisioned
  in the console, removing the operator-held shared ingest key from the connection path.
- ✅ Webhook endpoint secrets are encrypted at rest, returned exactly once, revocable, and
  tenant-isolated; delivery attribution comes from the endpoint identity, never the payload.

## 2026-07-28 product operations closure (plans 013-016)

- ✅ Guided onboarding now requires an explicit confirmation of the current product-policy
  templates per category; a superseded template version stops satisfying readiness instead of
  silently reusing an old acknowledgement.
- ✅ Decision queues are personalized by the verified workforce role through one server-owned
  assignment matrix; the complete tenant ledger remains available and audit visibility is intact.
- ✅ Monthly value reporting separates verified recovered amounts from estimated exposure.
  Only recorded actual amounts count as verified; modeled outcomes are never summed into them.
- ✅ Portable operations controls without a paid dependency: a stdlib health monitor with
  bounded incident receipts, plus release/rollback and POPIA operator documentation.

## 2026-07-28 guided onboarding and account readiness

- ✅ Resumable **Setup guide** for company, store, data source, optional devices, optional
  people, and final readiness review.
- ✅ Owner-only `GET /onboarding/status` derives progress from authoritative tenant-scoped
  stores; no client-provided completion state.
- ✅ Guided CSV preview/commit and encrypted ERP credential flows are available directly in
  onboarding; committed data or a configured connector satisfies the required data-source gate.
- ✅ Named work accounts include first name, surname, work position, email, password, and
  bounded operational role. A one-time platform key bootstraps the first opaque-ID owner;
  owners invite staff by email, change roles, deactivate/reactivate, resend invitations, and
  initiate recovery. Staff activate and choose their own password in the browser.
- ✅ Signed invitation/reset tokens are expiring and single-use, only digests are stored,
  account audit events contain identity IDs rather than PII, and role/password/activity changes
  atomically advance the account session version so earlier workforce JWTs stop working.
- ✅ Configured legacy-owner credentials migrate idempotently into the durable account store;
  steady-state fallback is disabled unless emergency recovery is explicitly enabled.
- ✅ Browser regressions cover full onboarding, connector connection, and device registration.
- ✅ Runtime endpoint precedence and strict company-profile write payload bugs found during
  real browser execution are fixed.
- ✅ Deployment dependency drift is contract-checked: every `pyproject.toml` runtime
  dependency must be present in `requirements.txt`, closing the clean-wheel import failure
  that GitHub CI exposed for Cryptography.
- ✅ Production migration drift is contract-checked for the durable encrypted edge-device
  registry; fresh Postgres CI no longer depends on module-local runtime DDL.

## 2026-07-23 technical-debt and readiness campaign

A day-long, book-grounded technical-debt and correctness campaign (see `HANDOFF.md` for the
full, dated record of every individual fix) closed real bugs across nine domains:
decision-science math, multi-tenant isolation/auth, agentic guardrails, inventory/procurement,
connectors, MLOps governance, the digital twin, chat streaming, and dependency/environment
integrity. Highlights, not exhaustive (see HANDOFF.md for the complete list with file:line
references and regression tests for each):

- **MLOps skill promotion now genuinely gates on a recorded evaluation result** (a real
  `EvaluationRecord` registry), not a client-supplied, unverified `measured_pass_rate` float -
  this was the single highest-severity finding of the whole campaign, since it meant any
  approver-role caller could previously promote any draft skill by asserting a perfect score
  with zero evaluation ever run.
- **Multi-tenant encrypted connector credentials** - a real subsystem (Fernet encryption,
  RLS-scoped Postgres storage, owner-only CRUD API) closing the "single-tenant only" gap the
  2026-07-14 connector-poll work had explicitly scoped out.
- **Twin scenario (what-if) predictions no longer contaminate operational reads** - running a
  scenario used to silently leak `PREDICTED`-lane values into `get_store()`/`fidelity()`'s
  reported "real" state.
- **Executive-conclusion grounding** across all 6 agentic cascades, correctly conditional so
  the existing adversarial critic-override tests still hold.
- **`/chat/stream` genuinely delivers incremental content** now, with the safety tradeoffs of
  true live-token capture explained and deliberately avoided rather than built unsafely.
- Three real, narrowly-scoped bugs fixed and regression-tested: a training eval gate using
  naive substring matching, a tenant-blind write-rate limiter (real cross-tenant DoS vector),
  and a CSV formula-neutralization routine that corrupted quoted fields containing commas.
- **This session's own dependency-drift bug** (see above) - `cryptography`/`psycopg-pool`
  present in `pyproject.toml` but never installed into the actual deployment `.venv`, caught
  only because this pass insisted on booting the real server rather than trusting test output
  alone.

The two application gaps previously called open are now reconciled. The receiving ledger no
longer claims to create, approve, or transmit purchase orders. Normalized operational
`STOCK_UPDATE` and POS `SALE` events now update the tenant inventory-position ledger through an
idempotent projection receipt: replay cannot double-decrement, overselling cannot make stock
negative, missing baselines and unsupported fractional units remain visible failure receipts,
and simulation events cannot enter the operational ledger. Live connector acceptance still
requires a real retailer sandbox and credentials; that external proof is not inferred locally.

**Every feature enabled by the supported application deployment profiles is implemented and
covered by the applicable local or CI gate.** The six capability records marked `partial` are
truthful external-proof boundaries: live provider inference and hardware-backed training/serving
must be run on the rented GPU environment. Deployment infrastructure still to be purchased (a
GPU rental, GPU-hours, cameras) is inventoried separately in the final appendix - procurement is
not an application feature, and classifying it as one misstates both.

## 2026-07-20 readiness verification update

- The full local backend regression suite passed: **761 passed, 16 environment-gated skips**;
  Ruff, capability-contract comparison, frontend typecheck, and frontend production build passed.
- All intelligence calculation POST routes now use the shared API-key write guard and bounded
  write-rate limiter. Edge-observation batch receipts now release failed projection claims, so a
  valid signed retry cannot be permanently misreported as a duplicate.
- Scenario mutations now require an ingest-capable tenant role; enabled production multimodal
  processing requires JWT protection plus the shared write guard/rate limiter; Postgres learning
  thresholds use a database-level maximum so concurrent approvals cannot regress an exposure.
- Production Compose defaults its explicit provider identity to `vllm_mi300x`; generic
  OpenAI-compatible routing remains available only when an operator deliberately selects it
  outside the AMD production profile.
- Session-capsule recovery accepts portable gzip archives only and rejects archive links and
  special-file members before extraction, so a recovery artifact cannot write beyond its target.
- Every supported Gemma 4 training profile and the active multimodal configuration use an
  immutable 40-character upstream revision; mutable branch names such as `main` fail validation.
- The regenerated capability manifest records **214** wired capabilities. Its only six `partial`
  records are deliberately external-proof boundaries (live Fireworks/MI300X inference and actual
  training/serving execution), not missing backend routes, workers, or event consumers.
- GitHub Actions CI on the current `developers` branch passed its real Postgres/Redis, browser E2E, production-topology,
  deployment-shakedown, and Track 3 gates; the capability-contract workflow also passed.
- A fresh deterministic fleet-scale run processed **500,000 of 500,000 requested rows** in
  **22,928 ms** (**21,807.4 rows/s**), produced 41,442 threshold candidates and a bounded top-200
  queue with zero LLM calls. The reproducibility receipt is
  [`reports/fleet-scale-shakedown-20260720.json`](reports/fleet-scale-shakedown-20260720.json).
- A fresh 8-cycle, fault-injected full-system replay completed with **0 failures**: 577 accepted
  world events, all 63 injected malformed/duplicate/stale/tenant faults rejected, a verified
  retry-to-dead-letter worker path, 87 unique decisions, 84 learning events, and 26 feature
  receipts. This is in-process deterministic proof; it is distinct from the live-model gate.
- The external acceptance still required tomorrow is deliberately not relabeled as local proof:
  the MI300X bootstrap and public-HTTPS live-model run require the rented droplet, its narrow
  application-host CIDR, and real credentials. `DROPLET_BOOTSTRAP.md` and
  `docs/mi300x-recreate-runbook.md` are the authoritative operator sequence.

Legend: ✅ implemented and tested · 🗺️ deliberately sequenced roadmap (recorded decision,
not an oversight).

## 1. Conversational assistant (the product's primary surface) — ✅

- ✅ Chat-first React console; `/chat` with idempotent message replay, per-conversation
  advisory locking (verified 16-writer concurrent test against real Postgres), tenant/user
  isolation (404 across tenants and users).
- ✅ **Hierarchical conversation memory** (`conversation_memory.py`,
  `shelfwise_chat_memory_items`, RLS, memory+Postgres stores): rolling episode summaries of
  everything older than the recent window — idempotent by covered-message hash, corrections
  preserved verbatim, longer prefixes supersede — plus first-class OBJECTIVE and CORRECTION
  memory items with per-item provenance. Proven end-to-end: an 8-turn conversation still knows
  its turn-0 objective (`tests/test_conversation_assistant.py`).
- ✅ **Progressive skill discovery** (`shelfwise_mlops/skill_registry.py`,
  `shelfwise_skill_manifests`, RLS): validated versioned manifests (unknown tool/agent, missing
  evaluations, write-without-HITL all rejected), deterministic trigger-ranked role-filtered
  discovery, promoted-only surfacing; 8 read-only platform skills seeded at boot against the
  real tool surface (a manifest naming a nonexistent tool fails startup loudly).
- ✅ **Skill lifecycle over HTTP**: `GET /mlops/skills`, `POST /mlops/skills/{id}/promote`
  (gated on the manifest's own evaluation pass rate), `POST /mlops/skills/{id}/retire`
  (terminal). Approval-role gated.
- ✅ **Deterministic tier routing** (`conversation_routing.py`): routine/strong route computed
  from pre-inference facts, saved as an auditable `conversation-route-v1` receipt on every
  answer's metadata.
- ✅ **Bounded adaptive evidence planning** (`retrieval_planning.py`): intent/risk signals select
  live facts, decisions, learning, traces, conversation memory, and promoted skills before state
  is read. The `retrieval-plan-v1` receipt records counts, omissions, freshness, conflicts,
  inadequacy, and a hard maximum of one follow-up. Conflicting/inadequate evidence selects the
  strong tier; account/help questions do not load operational state.
- ✅ **Token-accounted context receipts** (`context_budget.py`): per-section token accounting
  validated against the 8,192-token allocation BEFORE network I/O; receipt on every answer.
- ✅ Grounded agentic chat: real read-only platform tools, conclusions must cite tool numbers,
  hostile text fenced/spotlighted, English-output enforcement, live-required fail-closed.
- ✅ SSE streaming (landed 2026-07-17, honestly): `POST /chat/stream` emits a truthful
  lifecycle envelope - `accepted` -> `answer` (the complete grounding-validated reply, one
  event, because that is when a validated answer actually exists) -> `done` (the same
  receipts as POST /chat), with `replayed` for idempotent duplicates - plus a real
  OpenAI-compatible token-delta parser (`stream_chat_deltas`: genuine wire chunks only,
  [DONE]-terminated, refuses offline providers so generation is never fabricated). Token
  deltas from the live endpoint slot into the same envelope as `delta` events.

## 2. Agent cascades and decision pipeline — ✅

- ✅ Deterministic cascades: golden markdown, procurement, sales/price-integrity, cold-chain,
  recall, inventory exception (4 exception types), catalog-price outlier, expiry-risk.
  Markdown candidate parameters owned by the product-policy layer (per family), not inline.
- ✅ Agentic Gemma tool-calling cascades (golden, procurement, sales, cold-chain + catalog-price
  and expiry-risk guardrails): real tool loops, ungrounded conclusions rejected, per-turn
  response_format vs forced-tool-call conflict solved, deadline math regression-tested.
- ✅ **Critic→Executive contract enforced in code**: a failed critic verdict always routes the
  safe action regardless of what the executive answers; override recorded on the decision
  (`critic_gate`). Guardrail cascades fail closed on executive downgrade.
- ✅ HITL: approve/reject with idempotent double-submit (both callers 200, exactly one learning
  event — race fixed at the DB with `ON CONFLICT`, proven under real concurrency), corrections,
  cross-tenant 404s, write-back task receipts with rollback policy.
- ✅ Learning memory: per-action-type routed metrics (incl. procurement, previously absent),
  threshold movement `FOR UPDATE`-locked, tenant/domain-scoped; economics
  (`incremental_profit_minor_units`) populated by every cascade type.
- ✅ Mined-skill playbooks wired to production: `GET /mlops/skills/mined` mines from real
  resolved-outcome history (trigger = scenario id, provenance = actual decisions);
  `POST .../activate` re-mines, activates, compiles to the validated `Plan` shape as a
  governed recommendation artifact.
- ✅ Governed plan execution (landed 2026-07-17): `PlanRunner` wired live with a capability
  registry carrying ONLY real capabilities — the HITL write-back task sink as the sole write
  (role-gated, journaled, compensation recorded) and twin-fidelity recompute as the read.
  `POST /mlops/plans/execute` runs a validated plan with the tenant forced to the caller's;
  unknown capabilities fail closed. Source-system mutation remains behind real connector
  credentials exactly as the rollback policy records.

## 3. Event pipeline, worker, and queues — ✅

- ✅ `/ingest`: durable-store-first with published-flag self-heal (bus failure between record
  and publish republishes on retry instead of fake "duplicate"), content-drift 409, tenant/
  domain-scoped dedup, stale-event rejection for operational domain.
- ✅ Redis Streams bus verified against real Redis (19-probe lifecycle + env-gated contract
  tests in CI): consumer groups, pending-history redelivery, `times_delivered` dead-lettering,
  `XAUTOCLAIM` reclaim, approximate MAXLEN trimming, per-tenant streams.
- ✅ Async worker: per-process consumer identity (multi-replica safe), budget-derived reclaim
  idle threshold (`stale_consumer_idle_ms()` — env can only raise it; sub-budget values clamp
  up), journaled runs, dead-letter path, honest 503 still-processing on drill routes.
- ✅ Twin projection worker wired as a lifespan service (`TWIN_PROJECTION_WORKER_ENABLED`,
  Redis-only by design — refuses the memory bus with the reason in `/health`).
- ✅ Full production topology proven end-to-end (real Postgres RLS role + real Redis + worker +
  JWT simultaneously): ingest → queue → worker → decision → cross-tenant 404 → approve →
  learning → dedup.

## 4. Storage, tenancy, and database — ✅

- ✅ Postgres backends for every store (decisions, events, learning, chat, chat memory, skill
  manifests, candidates + history, open orders, catalog, inventory positions, inbound records,
  cursors, writeback, journal, model runs, prompts, tenant facts/profiles, twin ×7, worldgen,
  world snapshots) — schema-contract tested against a real least-privilege server, including
  write paths (ON CONFLICT columns pinned to real constraints, late-event ordering,
  identifier-conflict rejection).
- ✅ RLS forced on all tenant tables; app role `NOSUPERUSER NOBYPASSRLS` with runtime refusal
  of superuser/BYPASSRLS connections; per-record tenant session binding on write paths
  (decision-store gap found and fixed 2026-07-15); live-verified isolation.
- ✅ Connection pooling (production default, per-checkout RLS rebind, `SHELFWISE_DB_POOL*`).
- ✅ Idempotent additive migrations; compose migrate job; double-apply verified.
- ✅ Retention + BRIN time-series indexes (landed 2026-07-17): opt-in age-floored
  simulation-history retention (`RETENTION_ENABLED`, 7-day floor, world_simulation domain
  ONLY - operational rows and pending decisions survive any age, scope proven on real
  Postgres), refusing the in-memory backend honestly; BRIN indexes on the three
  append-only time-series tables, provisioned from the real schema.
- 🗺️ Table partitioning / read replicas: scale work beyond the 10K-user target on record.

## 5. Connectors and integrations — ✅

- ✅ Nine system connectors (Odoo/SAP/SYSPRO/Dynamics Business Central polling;
  Square/Shopify/Lightspeed/Yoco webhook-style intake; CSV):
  HMAC webhook verification with retry-safe failure release; durable incremental poll cursors
  (restart-surviving, real-Postgres verified); completed ERP pagination scans clear their opaque
  continuation and restart from page one on the next interval,
  scheduled poll loop (`CONNECTOR_POLL_ENABLED`, env-tunable cadence with hot-loop floor),
  status API tested in enabled state, fractional-quantity-safe mappers, provenance-tracked inbound
  records, money minor-units. Dynamics preserves opaque OData continuation URLs; Yoco requires
  explicit SKU/quantity/location metadata and an exact minor-unit unit-price split before a
  succeeded payment can emit a sales event; malformed source timestamps are quarantined without
  emitting an event.
- ✅ Edge gateway: HMAC-signed device observations, body-size bounds, twin intake.

## 6. Digital twin — ✅ (software layer)

- ✅ Entities/relationships/append-only observations, idempotent projection, lane separation,
  scenario branches, calibration, fidelity scoring, durable onboarding manifests, stable
  replay/recovery projection hash, event-sourced rebuild — memory + Postgres, RLS.
- ✅ Operational-twin agentic grounding on golden/procurement/sales/cold-chain routes
  (`data_domain`/`store_id`; 422 naming missing facts). Catalog-price/expiry-risk drills 422
  the operational domain by contract (synthetic anomalies never projected onto real data).
- ✅ Fabricated-telemetry ban: operational events missing consumed fields fail closed
  (`_require_operational_context`); sim story physics are named constants unreachable from the
  operational domain.
- ✅ Fidelity re-validation software (landed 2026-07-17): `FidelityRevalidationService` — a
  recurring governed schedule (`SCHEDULES_ENABLED`, daily default, 60s hot-loop floor) that
  recomputes every onboarded store's fidelity through the journaled plan runner and files a
  governed manager task when a score drifts below review threshold; status on
  `/worker/schedules` and `/readiness`. "Multi-week" is now purely elapsed calendar time on a
  running deployment, not missing software.
- ✅ 2D Store Twin view (landed 2026-07-17): the operations workspace renders the REAL twin
  read model - onboarded entities grouped by type in an SVG topology map (labeled as
  topology, never a pretended floor plan) with the live per-dimension fidelity receipt.
  Browser-verified end to end: onboarding two fixtures through the real /twin/onboarding API
  rendered them immediately, with the honest empty state before onboarding and an
  abort-safe fetch (StrictMode double-mount bug found and fixed during verification).
  Camera/edge sensor feeds will enrich these same entities in place.

## 7. Inference and model operations — ✅

- ✅ Provider identity is explicit at deployment (`LLM_PROVIDER`): arbitrary OpenAI-compatible
  endpoints are reported as unverified hardware and cannot claim or pass the AMD/MI300X gate.
- ✅ Two-tier Gemma architecture (routine E4B :8000 / strong 31B :8001), bounded per-call and
  per-cascade deadlines derived from `SHELFWISE_REQUEST_TIMEOUT_SECONDS` (the retired 30s gate
  is structurally gone; sub-budget reclaim/override values are inexpressible), fail-closed
  LIVE_REQUIRED, guided-JSON per-turn selection, retry bounds, malformed/echo/sentinel response
  rejection, model-run recording, token budgets on every agentic response.
- ✅ TSFM shadow forecasting degrades to the transparent baseline on transport failure with the
  error on the evidence record (fixed 2026-07-16; four failure modes tested).
- ✅ Training harness: config/dataset/provenance-boundary/eval-gate/serving-check all tested
  (serving check runs everywhere via committed metadata fixture); 15 case types incl. expiry/
  cold-chain/price-integrity; twin data firewalled out of training.

## 8. Security and governance — ✅

- ✅ JWT auth (HS256) with signed httponly `SameSite=Strict` cookie sessions; browser flow via
  `/auth/session`; **no build-time API key in the bundle** (VITE_API_KEY fallback removed
  2026-07-16 — runtime-config file only, closing SEC-06); IDOR-verified cross-tenant 404s;
  rate limits; body-size caps; prompt fencing/spotlighting; parameterized SQL throughout;
  content-addressed uploads; secrets untracked; hardened containers (read-only rootfs,
  cap_drop ALL, non-root, no-new-privileges).
- ✅ Accepted risk on record: shared public showcase tenant (`SHELFWISE_PUBLIC_DEMO_SESSION`),
  rate-limited, isolated from real tenants.

## 9. Frontend console — ✅

- ✅ Chat-first UI (appearance LOCKED by owner decision 2026-07-08): conversations, approval
  queue, evidence, product/operations workspaces, voice input + attachment intake wired to real
  multimodal endpoints, runtime endpoint config, AbortController on every fetch, route registry
  contract-tested against the real OpenAPI schema (5 governance routes added 2026-07-16),
  Playwright E2E over the real stack in CI.
- ✅ Company-account login (landed 2026-07-17): `POST /auth/login` verifies the configured
  owner account with stdlib scrypt (honest 503 unconfigured, uniform 401 on failure with no
  field oracle, constant-shape comparison) and mints the exact owner-role JWT session cookie
  the platform already verifies everywhere.
- ✅ Per-person work accounts: first-owner setup, owner-issued invitations, activation, normal
  login, forced temporary-password replacement, forgot/reset, role changes, session-aware
  deactivation/reactivation, identity-only audit, and tenant-scoped storage. Personalized
  per-role queue routing remains a product refinement, not an account-creation gap.

## 10. Simulation, evaluation, and observability — ✅

- ✅ Generated world (policy-constrained, no planted stories, batch/lot-aware, EAN-13-valid,
  deterministic by seed) + `SHELFWISE_WORLD_MODE` seam (static default; continuous fails
  honestly toward the harness's world rotation, which is the continuous driver today).
- ✅ Full-system soak harness (world rotation, fault injection, blackout, autopilot dissent,
  live-required mode, artifact validation), fleet-scale scoring (500k rows @ ~18k rows/s),
  synthetic eval suite with genuinely falsifiable scoring (tautology fixed 2026-07-15),
  benchmark runner/adapters/reporting.
- ✅ Observability: `/health` (all lifespan services incl. twin worker), correlation IDs
  end-to-end, traces, decision economics, accountability joins, HITL SLA/workload, worker and
  bus stats, structured receipts on every chat answer and agentic run.

## Previously-stale documents corrected

`AUDIT_AND_IMPLEMENTATION_BACKLOG.md` (2026-07-08 audit) and the plan doc's gap lists carried
many items long since closed; both now carry dated banners/closures. The remaining honest
not-implemented claims in any document map exactly to the procurement appendix below -
none of them is an application feature.

## Appendix: deployment procurement (not application features)

Everything the application needs from the outside world, each with its receiving
software already live and its acceptance gate committed. These are purchases, not code:

1. **MI300X droplet** (billed cloud GPU; destroyed by owner 2026-07-15). Recreate per
   `docs/mi300x-recreate-runbook.md`, repoint `LLM_*_BASE_URL`, then run
   `scripts/track3_prescreen.py` + one live agentic cascade as acceptance. Every
   fail-closed behavior without it is proven; live token deltas slot into the shipped
   `/chat/stream` envelope unchanged.
2. **ROCm training-pod hours** (billed). The training harness, datasets, gates, and
   serving checks are all tested; the expanded matrix is compute time.
3. **Camera/edge sensors** (physical AMD Kria/Versal devices). The HMAC edge gateway
   that receives their observations and the topology view that displays them are both
   live software today.

Integrating any retail system beyond the nine implemented connectors is likewise
per-system contract work against that system's real API, undertaken when a real system
appears - never faked in advance.
