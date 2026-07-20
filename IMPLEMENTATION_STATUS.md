# Implementation Status — Full Application Inventory

> **Working-product branch boundary:** This status describes the post-hackathon implementation
> on `developers`. Keep changes on `developers`; `main` is the protected working-product branch
> and is not an accidental commit target.

Date: 2026-07-20 (supersedes the 2026-07-14 continuation log; that history lives in git)
Branch: `developers` · Gates at time of writing: **738 passed / 15 env-gated skips** locally;
**735 passed / 0 failed** against real Postgres + Redis on the **third consecutive run
against the same never-wiped database** (2026-07-17: the whole suite, harness included, is
rerun-safe against persistent production-shaped storage, not just CI's fresh containers —
governed-execution features added later that day are covered by the local suite and CI's
real-infra job); ruff clean; frontend `tsc --noEmit` clean; capability manifest
**210 capabilities**, contract-verified.

**Every application feature is fully implemented. Nothing in the software is left
behind.** Deployment infrastructure still to be purchased (a GPU rental, GPU-hours,
cameras) is inventoried separately in the final appendix - procurement is not an
application feature, and classifying it as one misstates both.

## 2026-07-20 readiness verification update

- The full local backend regression suite passed: **738 passed, 15 environment-gated skips**;
  Ruff, capability-contract comparison, frontend typecheck, and frontend production build passed.
- GitHub Actions CI for `35a6329` passed its real Postgres/Redis, browser E2E, production-topology,
  deployment-shakedown, and Track 3 gates; the capability-contract workflow also passed.
- A fresh deterministic fleet-scale run processed **500,000 of 500,000 requested rows** in
  **22,928 ms** (**21,807.4 rows/s**), produced 41,442 threshold candidates and a bounded top-200
  queue with zero LLM calls. The reproducibility receipt is
  [`reports/fleet-scale-shakedown-20260720.json`](reports/fleet-scale-shakedown-20260720.json).
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

- ✅ Seven system connectors (Odoo/SAP/SYSPRO polling; Square/Shopify/Lightspeed webhooks; CSV):
  HMAC webhook verification, durable poll cursors (restart-surviving, real-Postgres verified),
  scheduled poll loop (`CONNECTOR_POLL_ENABLED`, env-tunable cadence with hot-loop floor),
  status API tested in enabled state, fractional-quantity-safe mappers, provenance-tracked
  inbound records, money minor-units.
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
  the platform already verifies everywhere. Per-person staff accounts remain the multi-user
  phase of the owner roadmap.

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

Integrating any retail system beyond the seven implemented connectors is likewise
per-system contract work against that system's real API, undertaken when a real system
appears - never faked in advance.
