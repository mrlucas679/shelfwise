# ShelfWise — Audit & Implementation Backlog

> **⚠️ HISTORICAL DOCUMENT (2026-07-08 audit) — superseded by `IMPLEMENTATION_STATUS.md`
> (2026-07-16).** Kept as the audit trail; do NOT treat its open/partial/not-started markers
> as current. Verified 2026-07-16: every item in the "Still open" paragraph below now exists
> in code (batch/lot model in `shelfwise_worldgen/populate.py`, context assembler, product-
> policy registry, candidate factory + fleet scoring, the wired reclaim loop with a
> budget-derived idle threshold), SEC-06 is closed (the frontend no longer reads any
> build-time API key; browser auth is the signed httponly cookie session), the Redis bus has
> MAXLEN trimming + XAUTOCLAIM recovery verified against real Redis, the worker
> nacks/dead-letters instead of acking failures, and auth is JWT fail-closed in production
> compose. For what is genuinely implemented, external-gated, or roadmap today, read
> `IMPLEMENTATION_STATUS.md`.

> **Working-product branch boundary:** This backlog is for post-hackathon implementation on
> `developers`. Keep implementation commits on `developers`; `main` is the protected
> working-product branch and requires an explicit release decision.

**One consolidated file.** It merges (a) a deep, read-only code audit run with the `improve` skill
against the repo's own build skills in `plot/skills/`, (b) the verified status of every item from
the former `Things that needs to be implemented.md` source (now consolidated into this historical
backlog), and (c) a single prioritized backlog that unifies both. Nothing from the former "Things"
file was dropped; each item was re-checked against the current code and given a verified status.

- **Audited at commit:** `c14c297` (branch `feature/chat-first-approval-ui`)
- **Audit date:** 2026-07-09
- **Verification baseline (green):** `PYTHONPATH=src python -m pytest -q` → **227 passed in ~2.6s**;
  `python -m ruff check src tests scripts` → **all checks passed**; `npm audit --omit=dev
  --audit-level=high` (in `frontend/`) → **0 vulnerabilities**. The repo has a fast, working
  verification gate — every plan below can cite it.
- **Effort scale:** S = hours · M = ~a day · L = multi-day (for the *fix*, including tests).

## 2026-07-14 implementation amendment

A full read-through of `DIGITAL_TWIN_RESEARCH_AND_IMPLEMENTATION_PLAN.md` against the running
system (every technical claim traced into actual code, not just categorized) found and fixed four
real bugs: a Lightspeed connector capability mismatch, a twin projection-hash determinism bug
(wall-clock fields breaking replay/recovery verification), onboarding-created twin topology not
surviving a real projected-state loss/rebuild (fixed with a new durable
`OnboardingManifestRegistry`), and agentic chat reporting the wrong model tier in its own response
metadata. Full detail and reproduction steps are in that document's dated audit entries and in
`HANDOFF.md`'s 2026-07-14 entry. Current verification baseline: **595 passed, 6 skipped**; Ruff
clean; capability manifest at 199 capabilities. No other bugs are known as of this pass.

## 2026-07-13 implementation amendment

The original audit remains the scope authority, but its verification baseline is historical. The
current working tree additionally implements and verifies:

- tenant-scoped candidate persistence with stable identities, suppression expiry, and HITL links;
- shipment-derived open-order state with Postgres/RLS support and reorder-noise suppression;
- a bounded context assembler with evidence score, missing-data list, token estimate, and manifest;
- product-family policy resolution, HITL SLA/workload metrics, and a deterministic 500,000-row fleet
  scoring receipt with zero LLM calls.

Current proof is local, not a public deployment claim. Remaining scope includes broader candidate
types, automatic procurement connector coverage, cursor-based frontend scale behavior, browser E2E,
and deployed Postgres/Redis/Nginx verification.

---

## 2026-07-09 implementation session — what got fixed

Everything below was implemented in real code with new/updated tests, verified green after every
step (`pytest`, `ruff`, the `shelfwise_eval` gate, and the frontend `npm run build`). Current
verification state: **282 tests passing, ruff clean, eval gate 28/28, frontend build clean.**
Frontend appearance unchanged (verified in browser preview — see Batch G note below).

**P0 correctness fixed (§1.2/§1.3):** C-01 (worker no longer acks failed events — proper
nack/retry/dead-letter on both bus backends), C-02 (journal keyed on `event.id`, not
`correlation_id`), C-03 (ingest publish-before-record with a `published` flag so a bus failure
self-heals on retry instead of orphaning), C-04 (locks added to `InMemoryJournal`/`TraceRegistry`),
C-05 (sales decision id no longer collides across events), C-06 (invalid JWT binds a sentinel
tenant, not the real default), C-07 (streamed body-size enforcement, not just `Content-Length`),
C-08 (compaction keeps a contiguous newest window), D-01 (Shopify/Lightspeed/Square mappers emit one
record per line/count, not just the first — plus the inbound-store dedup key fix that was silently
capping this at one record per payload), D-02/D-03 (multimodal crash-safety: malformed ISO dates,
oversized numbers, non-object VLM responses), D-04 (webhook dedup race fixed), D-05 (KeyError
crashes on missing mapper fields), D-06/D-07/D-09 (expiry-risk weights now sum to 1.0, payday
multiplier no longer bleeds into the cold-chain scenario, reorder-at-equality suggests ≥1 unit),
D-11 (inference client records and wraps malformed-response failures), D-12 (poll connector bounded
against a non-advancing cursor).

**Security fixed (§1.1, SEC-01 → SEC-09, 8 of 9):** RLS-bypass superuser role (new `shelfwise_app`
least-privilege Postgres role + a fail-closed boot check), fail-open auth in named deployments,
cross-tenant IDOR on decision read/approve/reject, unscoped `/events`/`/events/bus`/`/mlops/*`/
`/chat` reads, the `/mlops/accountability` caller-supplied-tenant param, the rate limiter trusting
an unverified `x-api-key` header, and unauthenticated demo/worldgen mutation. **SEC-06** (API key
inlined into the frontend bundle) is documented in `frontend/.env.example` but not re-architected —
it needs a real frontend auth flow, out of scope for a fix-in-place session.

**Tests added (§1.6):** T-01 (reject-flow), T-07 (`Money` arithmetic) plus dedicated tests for every
fix above (~55 new/updated test functions across the fixes).

**Scale substrate — Batch G shipped (§4 item 1, was NOT STARTED):** a real, tested, wired-in product
identity module — `src/shelfwise_catalog/` (`Product`, `ProductVariant`, `ProductIdentifier`, dual
in-memory/Postgres stores, RLS, schema.sql migration) plus `/catalog/products`,
`/catalog/products/{id}`, `/catalog/products/{id}/variants`, `/catalog/identifiers`,
`/catalog/resolve` endpoints. Proves the core ask: two different source-system codes (e.g. SAP
material id + POS barcode) for the same physical item resolve to the same variant, and a
conflicting remap is rejected (409), not silently merged. README, the frontend's own endpoint
registry (data rows in the existing "Read-only API coverage"/"Gated operational endpoints" lists),
and `pyproject.toml` packaging all updated. Frontend visual state verified unchanged in browser
preview after the change.

**Still open (not started this session — each is genuinely multi-day scope, listed here rather than
stubbed):** batch/lot/expiry model (§4 item 2), bulk/incremental ingestion (§4 item 3), typed hot
columns + table partitioning (§4 item 4/7), candidate generation + fleet-wide scoring (§4 item 5/10),
scale-ready worker runtime beyond the dead-letter fix (§4 item 6 — retry/dead-letter is done; batch
consume, backpressure, and pending-message recovery via `reclaim_stale` exist on the Redis bus but
aren't wired into a scheduled reclaim loop), context assembler (§4 item 8), product-policy registry
(§4 item 9), everything in P1/P2/P3 (§5). SEC-06's full fix, DEBT items (god-module refactors), and
the deps/DX cleanups (mypy, pre-commit, digest-pinned images) are also untouched. Treat §5 as still
current for prioritizing the next session.

---

## ⚠️ Standing constraint — the frontend look is LOCKED

The chat-first interface **is the product** and its current appearance is final by owner decision
(2026-07-08). **Every item in this document is additive capability or an internal refactor that
renders byte-identically — never a visual redesign, never a dashboard conversion.** Where a finding
touches `frontend/src/App.tsx` or `index.css`, the fix is component decomposition / correctness /
performance that preserves the exact rendered output. The `figma-design-system` skill is out of
scope for this repo per the owner. This constraint overrides any suggestion below that could be read
as "change the UI."

---

## How to read this file

1. **§1 Audit findings** — 64 vetted findings across the 9 audit categories, each with `file:line`
   evidence, impact, effort, fix-risk, and confidence. Grouped by category, most-severe first.
2. **§2 Skill-compliance matrix** — how the code measures against the repo's own 21 build skills +
   the 5 standing rules.
3. **§3 Blueprint-vs-code gap map** — which planned domains are genuinely implemented in `src/`.
4. **§4 "Things that needs to be implemented" — verified status** — all 29 backlog items, each
   marked NOT STARTED / PARTIAL / DONE with code evidence.
5. **§5 Unified prioritized backlog (P0–P3)** — the actionable synthesis: audit findings + Things
   items, de-duplicated, dependency-ordered, with machine-checkable done criteria.
6. **§6 Direction findings** — forward-looking options for the maintainer (not ranked against bugs).
7. **§7 AMD compute proof** — status of the "built on AMD" evidence.
8. **§8 Considered and rejected** — things that looked like findings but are by-design.

Every finding was vetted by re-opening the cited code. The audit fanned out across the whole repo;
what was **not** exhaustively read is listed at the end of each subsection's source lane and in §8.

---

# §1 — Audit findings (vetted)

## 1.1 Security (highest priority)

The five-agent lane found nine issues. The top one is severe: the shipped Postgres configuration
silently voids the entire tenant-isolation layer.

| # | Finding | Sev | Effort | Confidence | Evidence |
|---|---|---|---|---|---|
| SEC-01 | **Tenant RLS is bypassed by the superuser DB role.** `schema.sql` force-enables RLS on 15 tables but creates no non-superuser role and no GRANTs; the app connects as the DB-owner/superuser role, and Postgres does not enforce RLS (even `FORCE`) for superusers/`BYPASSRLS`. All `tenant_id = current_setting('app.tenant_id')` policies are inert — cross-tenant separation does not hold. | **Critical** | M | HIGH | `src/shelfwise_storage/schema.sql:195-296`; `docker-compose.yml:4-7,46`; `.env.example:10`; `src/shelfwise_storage/postgres.py:25-36`; `rls.py:23-38` |
| SEC-02 | **Auth is fail-open by default, including in shipped compose.** `_auth_mode()` defaults to `"off"`, which returns an OWNER tenant context and makes `write_path_guard` a no-op. Every state-changing endpoint (`/ingest`, `/connectors/*/intake`, approve/reject, `/worker/process-one`, `/tenants/me`) is reachable unauthenticated with full owner rights. | **Critical** | M | HIGH | `src/shelfwise_backend/app.py:158-159,174-177,184-185`; `tenant.py:37-43`; `docker-compose.yml:41-49`; `.env.example:7` |
| SEC-03 | **Decision approve/reject/read lack a tenant-ownership check (IDOR).** `GET /decisions/{id}` has no auth; approve/reject are role-gated but never compare `decision.tenant_id` to `ctx.tenant_id`, and the in-memory store keys decisions in one global dict. Any approver can approve/reject another tenant's HITL money decision. | High | S | HIGH | `app.py:868-873,876-910,946-954`; `src/shelfwise_action/store.py:52-90` |
| SEC-04 | **Unauthenticated, unscoped multi-tenant reads.** `/decisions`, `/events`, `/events/bus`, `/traces`, `/learning` return every tenant's rows (in-memory) or rely entirely on the RLS layer SEC-01 shows is bypassed. | High | M | HIGH | `app.py:849-851,381-392,403-405,854-859`; `event_store.py:28-33` |
| SEC-05 | **Accountability endpoint trusts a caller-supplied `tenant_id` query param** instead of the token tenant, and folds in a global `decision_store.list()`. Cross-tenant read of governance/economics data. | High | S | HIGH | `app.py:618-630`; `src/shelfwise_mlops/registry.py:93` |
| SEC-06 | **Write-path API key is inlined into the public frontend bundle.** `App.tsx` reads `VITE_API_KEY` and sends it as `x-api-key`; Vite bakes `VITE_*` into the built JS, so any real key ships to every browser. The write-path "key" is not a real control. | High | M | HIGH | `frontend/src/App.tsx:271-277`; `app.py:174-177`; `router.py:74-77` |
| SEC-07 | **Rate limiter keys on an unverified `x-api-key` header.** When `API_KEY` is unset the identity is attacker-chosen, so rotating the header yields a fresh token bucket (throttle bypass) and flooding distinct values evicts legitimate buckets (LRU, `max_keys=1024`). | Med | S | HIGH | `src/shelfwise_backend/security/gateway.py:44-86,101-107`; `app.py:174-177` |
| SEC-08 | **State-changing demo/worldgen endpoints are unauthenticated, unthrottled, and mutate via GET.** `/scenarios/golden|procurement|sales|cold-chain` expose GET handlers calling `decision_store.upsert`; `GET /scenarios/worldgen/{id}` runs a full ingest pipeline (up to 500 events) anonymously. GET mutation is cacheable/CSRF-reachable. | Med | S–M | HIGH | `app.py:704-764,767-846` |
| SEC-09 | **Weak default DB credentials committed + DB port published.** `docker-compose.yml` sets a well-known dev password and publishes `5432:5432`; the same default is embedded in `.env.example`. Repo is heading to public GitHub — the value is burned even after change. *(Value not reproduced per handling rule; rotate it.)* | Med | S | HIGH | `docker-compose.yml:4-9`; `.env.example:10`; `docker-compose.yml:46` |

**Sound by design (not findings):** JWT verification enforces `HS256`, rejects `alg:none`/empty
secret, constant-time compare, checks `exp` (`tenant.py:53-74`); `/detective/root-cause-sql` returns
a static parameterized CTE as text, never executed (`detective.py:83-118`); LLM-bound text is fenced
via `spotlight`/`fence_context` (`chat.py`); scan/voice uploads enforce content-type + size cap +
magic-byte sniffing (`router.py:85-157`); CORS is localhost-only with `allow_credentials=False`
(`app.py:74-83`); container runs non-root, read-only, `cap_drop: ALL` (`Dockerfile`, compose).

## 1.2 Correctness / Bugs — backend core

| # | Finding | Sev | Effort | Confidence | Evidence |
|---|---|---|---|---|---|
| C-01 | **Worker acks events even when the cascade fails, silently dropping them.** The `except` branch marks the run failed and still `XACK`s; with no dead-letter/reclaim the business event (sale, cold-chain alert) is lost. (Also backlog #6.) | High | M | HIGH | `src/shelfwise_backend/worker/worker.py:94-105`; `event_bus.py:119-120` |
| C-02 | **Run journal keyed on `correlation_id` conflates distinct events that share one.** Two different events with a shared correlation collide on `(run_id,"cascade")`; the second returns the first's cascade, re-persists the first decision, and is acked — silently no-op'd. | High | M | MED | `worker/worker.py:74-81`; `worker/journal.py:259-264`; caller at `app.py:1085` |
| C-03 | **Ingest records the event before publishing; a bus failure orphans it and dedup blocks retry.** `record()` returns False on duplicate id, so a mandatory retry short-circuits to `"duplicate"` and never publishes — a dual-write orphan (stored, never processed). | High | M | MED | `app.py:957-974`; `event_store.py:47-70` |
| C-04 | **`InMemoryJournal` and `TraceRegistry` mutate/iterate shared state with no lock**, unlike every sibling store; the background worker thread writes while API reads iterate → `RuntimeError: dict/deque changed size during iteration` or dropped records. | Med | S | MED | `worker/journal.py:32-87`; `trace.py:29-61`; contrast `event_store.py:16`, `event_bus.py:15` |
| C-05 | **Auto-approved sales decision on a fixed id gets frozen by the terminal-state guard.** A clean sale (or any `GET /scenarios/sales`) writes `dec_pos_sale_price_integrity` as `approved`; every later real price-exception computes the same id, and `upsert` returns the approved record — the PENDING exception is never surfaced for HITL. Distinct from the known "scenario-keyed id" debt: it silently *loses an exception*. | High | M | MED | `cascade.py:608,685-695`; `store.py:30-40`; `app.py:1036-1040,746-750` |
| C-06 | **Invalid/expired JWT binds the storage tenant to the real default tenant** instead of failing closed, so bad-token reads run under a real tenant's RLS context. (Overlaps SEC-02/04.) | Med | S | MED | `app.py:162-171,148-155` |
| C-07 | **Body-size guard only checks the `Content-Length` header**; chunked/header-omitted uploads skip the 6 MB cap entirely, then get fully read downstream. | Med | S | MED | `app.py:132-145` |
| C-08 | **`compact()` greedy-by-fit loop violates its "keep newest turns" contract** — a large recent turn is dropped while an older smaller turn survives, producing non-chronological history. | Low | S | LOW | `worker/compaction.py:26-32` |

## 1.3 Correctness / Bugs — domain packages + frontend

| # | Finding | Sev | Effort | Confidence | Evidence |
|---|---|---|---|---|---|
| D-01 | **Webhook sales/inventory mappers drop every line item except the first** (`line_items[0]` / `counts[0]`). Real multi-item orders undercount sales and inventory, feeding wrong quantities into demand/reorder/expiry math — silently, no error. | High | M | HIGH | `connectors/systems/shopify.py:26`; `lightspeed.py:26`; `square.py:26` |
| D-02 | **`normalize_for_speech` crashes on malformed ISO dates.** Regex accepts months 00–99; `_MONTHS[month-1]` `IndexError`s on 13–99. A spoken string like `2025-13-01` fails the whole `/voice/out` path. | Med | S | HIGH | `multimodal/text_normalize.py:111,190,129`; `speech_style.py:18`; `tts.py:37` |
| D-03 | **`scan_image` 500s instead of falling back when the VLM returns valid-but-non-object JSON.** The `try` guards only `json.loads`; `parsed.get(...)` on a list/number raises `AttributeError` outside it, and the response envelope access is unguarded. | Med | S | HIGH | `multimodal/vision.py:48-53` |
| D-04 | **Webhook dedup is check-then-act across two lock acquisitions** — concurrent retries of the same `event_id` can both pass `seen()` before either `mark()`s, so both emit a record (defeats idempotency). | Med | S | HIGH | `connectors/connectors/webhook.py:22-28,59-61` |
| D-05 | **Order mappers raise `KeyError` on missing `id` fields** despite guarding the empty-lines case; propagates as a 500 rather than a failed-validation record. | Med | S | HIGH | `shopify.py:30,41`; `square.py:30-31` |
| D-06 | **Expiry-risk weights sum to 1.10** (`velocity*0.75 + cold_chain*0.35`), so a moderately-high pair pins the composite to the clamp ceiling (1.0), collapsing distinct situations to identical maximal risk. Likely a typo in a convex blend. | Med | S | MED | `decision_science/expiry.py:38` |
| D-07 | **`forecast_demand` applies the 1.35 payday multiplier unconditionally** (no date/payday gate), inflating every forecast 35% → biased horizon units, reorder points, and expected-sold. May be an undocumented demo lever; there's no un-multiplied path. | Med | S | MED | `decision_science/forecasting.py:42` |
| D-08 | **Sales-line normalization: `int(quantity)` crashes on fractional/string qty; SALE event stores `unit_price` as a major-unit string, not integer minor units.** Any consumer treating it as ZAR cents (per the Money contract) is off by 100×. | Med | M | MED | `connectors/normalize.py:85,86,105-116` |
| D-09 | **`should_reorder` is true at exact equality but suggests zero units.** When `available == reorder_point`: `should_reorder=True`, `suggested_order_units=0` — "reorder now, order nothing." | Low | S | MED | `decision_science/inventory.py:98,109-110` |
| D-10 | **Frontend `send()` updates state in its catch path with no abort/unmount guard**, unlike the sibling `resolve()`; and assigning a new `chatCtrl` without aborting the prior leaks the in-flight request on rapid re-send. (Internal fix — no visual change.) | Low | S | HIGH | `frontend/src/App.tsx:2958-2959,2972-2976,3021,2915-2918` |
| D-11 | **Inference client only catches transport errors, not malformed 200 bodies**, so a non-JSON success raises a raw `ValueError` (unrecorded, unwrapped); and a network failure hard-fails instead of degrading to the deterministic OFFLINE provider the code already implements. | Med | S–M | MED | `shelfwise_inference/client.py:150-167` |
| D-12 | **`PollingConnector.pull` can loop forever on a non-advancing cursor** — `while True` breaks only on `next_cursor is None`; a source that keeps returning the same cursor spins the event loop. | Low | S | MED | `connectors/connectors/poll.py:43-55` |

## 1.4 Performance

| # | Finding | Sev | Effort | Confidence | Evidence |
|---|---|---|---|---|---|
| P-01 | **No DB connection pooling** — `connect()` opens a fresh psycopg connection (+ a `set_config` round-trip) on every call, across 12 modules / 83 call sites. One frontend load fans out ~27 endpoints, each ≥1 connect. Dominant per-request latency and a connection-exhaustion risk at scale. | High | M | HIGH | `src/shelfwise_storage/postgres.py:25-36` (+83 call sites) |
| P-02 | **Decision upsert is 3 connections/round-trips** (`get` → `insert…on conflict` → `get`); `_transition`/`annotate` are 2 each. Fold reads into the write with `returning`. | Med | S | HIGH | `src/shelfwise_action/store.py:111-148,174-203` |
| P-03 | **`/mlops/observability` fans into ~7 unbounded full-collection reads** then recomputes every metric in Python over the full lists — cost grows with total data, not what's shown. | Med | M | HIGH | `app.py:640-658`; `observability.py:10-48` |
| P-04 | **`PostgresDecisionStore.list()` has no LIMIT and no filter** and deep-copies every row; called by 5 endpoints (`/decisions`, observability, accountability, `/chat`, learning). Distinct from the tracked "frontend pagination" item — this is a literally unbounded backend query. | Med | S | HIGH | `store.py:150-159` |
| P-05 | **Product search regenerates the whole synthetic catalog and re-reads seed CSVs on every call**; a no-match query walks the entire `hypermarket`-scale catalog per request. | Med | M | MED | `backend/product_catalog.py:98,127-133,230-236`; `worldgen/catalog/generate.py:65-77` |
| P-06 | **Full Redis keyspace `SCAN` on every consume/stats/list/clear**; the worker loop scans the whole `shelfwise:events:*` keyspace 4×/sec while idle. | Med | M | MED | `event_bus.py:101-108,122-144,154`; `worker/service.py:50-56` |
| P-07 | **CI does not cache pip** (npm is cached, pip is not) — every run re-downloads all Python deps. *(Same fix as DX-03.)* | Low | S | HIGH | `.github/workflows/ci.yml:18-24` |
| P-08 | **Detective BFS uses a linear `_find_by_correlation` scan per unresolved parent**, degrading toward O(nodes×misses) over up to 500 events + all decisions, in the request path. | Low | S | MED | `backend/detective.py:166-169,188,58` |

## 1.5 Tech debt & architecture (measured against `clean-code-principles`)

| # | Finding | Principle | Effort | Confidence | Evidence |
|---|---|---|---|---|---|
| DEBT-01 | **Five near-identical cascade builders** (~1,100 lines) repeat the same skeleton and have already drifted; a decision-contract change needs 5 edits in lockstep. | DRY, SRP, Power-of-Ten #4 | L | HIGH | `cascade.py:58,322,552,724,966` |
| DEBT-02 | **`app.py` is a 1,220-line god module** — composition root + middleware + auth + ~45 routes across 12 domains + heavy business helpers; handlers hold business logic (contradicts the repo's own "handlers delegate" rule). | SRP, SoC, Power-of-Ten #9 | L | HIGH | `src/shelfwise_backend/app.py` (whole) |
| DEBT-03 | **Store scaffolding duplicated ~12×** — the `os.getenv` backend-dispatch block and `def _now()` are copy-pasted across 12 / 7 modules; factories return unions with no shared `Protocol`. | DRY, DIP, LSP/ISP | M | HIGH | 12 factory sites + `_now()` in 7 files (e.g. `action/store.py:264,93`) |
| DEBT-04 | **Frontend god components** — `WorkspaceScreen` (~810 lines, `switch` over 8 surfaces) and `App` (13 `useState`). Pure structural extraction; JSX/classNames move unchanged → **byte-identical render** (visual lock preserved). | SRP, "one component one job" | L | HIGH | `frontend/src/App.tsx:1685-2495,2569-2983,1167` |
| DEBT-05 | **`eval → backend.app` layering inversion** — the harness imports 15 live module-level singletons from the composition root, forcing `.clear()` on stores and cementing `app.py` as un-decomposable. | DIP, Power-of-Ten #9 | M | HIGH | `src/shelfwise_eval/harness.py:9-25` |
| DEBT-06 | **Broad `except Exception` around `Decimal()` parses** substitutes a default money value and hides shape errors, while sibling helpers in the *same file* correctly catch `(TypeError, ValueError)`. | Fail-Fast, Power-of-Ten #7 | S | HIGH | `cascade.py:1123`; `app.py:1038,1214`; `observability.py:206`; `worldgen/narrate.py:24` |
| DEBT-07 | **Duplicated GET/POST `/scenarios/*` handlers** — 8 handlers where 4 (`api_route`) would do. | DRY | S | HIGH | `app.py:704-760` |
| DEBT-08 | **`minor_units→amount` money conversion re-implemented** in 3 places instead of routing through `Money.amount`. | DRY, Abstraction | S | MED | `contracts/__init__.py:74`; `memory/__init__.py:316`; `connectors/normalize.py:111` |
| DEBT-09 | **Config read three ways** — a dataclass loader, a settings module, and 57 ad-hoc `os.getenv` calls across 21 modules; the same key read in many places. | DIP, Explicit>Implicit | M | MED | 57 `os.getenv` sites; `inference/config.py`; `multimodal/settings.py` |
| DEBT-10 | **Built-but-unwired MCP registration surface** — `register_platform_mcp` is exported and tested but never mounted in the running app. | YAGNI/Encapsulation | S | MED | `backend/tools/mcp_surface.py:162`; `tools/__init__.py:7` |
| DEBT-11 | **`index.css` is one 1,971-line stylesheet** (organizational only; no runtime cost). Investigate alongside DEBT-04; no dead-selector claim verified. | High Cohesion, SRP | M | LOW | `frontend/src/index.css` |

## 1.6 Test coverage

| # | Finding | Sev | Effort | Confidence | Evidence |
|---|---|---|---|---|---|
| T-01 | **HITL reject endpoint + reject transition are entirely untested** (the approve half is exhaustively covered). The demo's "reject" button has zero backing. | High | S | HIGH | `app.py:946-954`; `store.py:71-90` |
| T-02 | **Postgres/Redis backends have zero behavioral coverage** — only factory selection is tested; no round-trip ever runs the production persistence SQL. No `skipif`/`importorskip` exists, so these tests are simply absent, not skipped. | High | M | HIGH | `tests/test_store_backends.py:28-136`; `store.py:97-261` |
| T-03 | **Tenant RLS is verified only at the SQL-string level against a fake connection** — no test executes the policy against real Postgres to prove tenant A can't read tenant B. (The exact property SEC-01 breaks.) | High | M | HIGH | `tests/test_database_schema.py:62-78,28-44`; `rls.py:23-62` |
| T-04 | **Frontend "tests" are Python substring-scrapes of `App.tsx`**, not behavioral tests; they break on innocuous edits and pass while the UI is broken. | Med | M | HIGH | `tests/test_frontend_attention_contracts.py:29-64`; `test_frontend_route_coverage.py:10-139`; `frontend/package.json:6-24` |
| T-05 | **CI has no frontend behavioral tests, no frontend linter, and no backend type checker** (`mypy` absent everywhere). *(Overlaps DX-01, DX-03.)* | Med | S–M | HIGH | `.github/workflows/ci.yml:26-51`; `package.json:6-11` |
| T-06 | **Inference client's error/network/parse branches are untested** despite being offline-testable via a fake config + injected payload. | Med | S | HIGH | `shelfwise_inference/client.py:113-125,146-161,251-286` |
| T-07 | **`Money` arithmetic/rounding has no direct unit test** — the core money primitive is exercised only via golden magic numbers; a rounding or currency-mismatch regression is caught only by coincidence. | Med | S | MED | `contracts/__init__.py:63-106`; no `tests/test_contracts*.py` |
| T-08 | **No characterization tests around the 800-line cascade's gate boundaries** — threshold flips that preserve the golden numbers ship undetected. | Med | M | MED | `cascade.py:240,471,583` |

## 1.7 Dependencies, DX & Docs

| # | Finding | Cat | Effort | Confidence | Evidence |
|---|---|---|---|---|---|
| DEPS-01 | **Two hand-synced manifests; the runtime image ships dev tooling + tests.** `requirements.txt` folds pytest+ruff into runtime; `Dockerfile` installs it and `COPY tests`. Drift risk + larger attack surface. | Deps | S | HIGH | `pyproject.toml:7-24`; `requirements.txt:1-9`; `Dockerfile:6,8` |
| DEPS-02 | **Container base images pinned to floating tags, not digests** — builds not reproducible. Low urgency. | Deps | S | HIGH | `docker-compose.yml:3,19`; `Dockerfile:1`; `frontend/Dockerfile:1,11` |
| DX-01 | **No Python static type checking anywhere** despite a fully annotated backend; `IMPLEMENTATION_STATUS.md:73` records "mypy is not installed." Frontend type-checks (`tsc --noEmit`), backend has no equivalent gate. | DX | M | HIGH | no `[tool.mypy]` in `pyproject.toml`; `ci.yml:26-54` |
| DX-02 | **`.env.example` omits the `API_KEY` write-path control** and `SHELFWISE_MAX_BODY_BYTES`, `INFERENCE_ZAR_PER_1K`, `COLD_CHAIN_*`, `TENANT_ID`; there is **no** `frontend/.env.example` for the `VITE_*` vars. The primary auth control is undocumented. | DX | S | HIGH | `app.py:124,175,1166`; `cold_chain_demo.py:101-113`; `App.tsx:269,272`; `vite.config.ts:6-7` |
| DX-03 | **No pre-commit hooks; CI caches npm but not pip.** *(pip half = P-07.)* | DX | S | HIGH | no `.pre-commit-config.yaml`; `ci.yml:18-24,41-43` |
| DX-04 | **Makefile uses POSIX-only inline `PYTHONPATH=src`**, incompatible with the repo's documented Windows/PowerShell workflow — `make test/eval/run` fail under PowerShell. | DX | S | MED | `Makefile:4,7,10,13`; `README.md:16` |
| DOCS-01 | **Stale scope-freeze language still lives inline** in `CODEX.md:441` (`Supplier/Logistics/Risk/Sales \| Stub/roadmap \| Do not build`) and `plot/PLOT.md:155`, directly contradicting the 2026-07-07 `CLAUDE.md` mandate. An executor reading the tables gets an authoritative "do not build." | Docs | S | HIGH | `CLAUDE.md:3-6,29-34`; `CODEX.md:441`; `plot/PLOT.md:155` |
| DOCS-02 | **Quick Start omits the dependency-install step** in README, DEMO_RUNBOOK, and JUDGE — a fresh clone (or a judge) fails at import before step 1. | Docs | S | HIGH | `README.md:15-19`; `DEMO_RUNBOOK.md:5-13`; `JUDGE.md:11-15` |

---

# §2 — Skill-compliance matrix

How the shipped code measures against the repo's own build skills (`plot/skills/`) and the 5
standing rules. "Skill honored" = the code follows the skill's procedure; deviations link to the
finding.

| Repo skill / rule | Verdict | Evidence / deviation |
|---|---|---|
| **clean-code-principles** (25 principles + Power-of-Ten) | ⚠️ Partial | Strong: Money-as-int, typed contracts, evidence grounding, fail-fast at Pydantic boundaries. Violations: DEBT-01/02/03/06/09 (DRY/SRP/god-modules/broad-except/config), C-04 (guard shared mutable state). |
| **pydantic-contracts** | ⚠️ Divergent | Contracts exist and match the Event/EvidenceObject/Decision *shape* and the validate-then-retry guard, **but are hand-rolled `@dataclass`es, not Pydantic v2 models** (`contracts/__init__.py`). Functionally equivalent; deviates from the skill's stated tech. IDs are uuid4-hex, not ULID. |
| **fastapi-service** | ⚠️ Partial | App factory + typed routes + settings present. Deviations: `/chat` **simulates** streaming (`_chunk_words`) rather than streaming real tokens (`chat.py:86-96`); handlers hold business logic (DEBT-02); module-level singletons instead of lifespan-scoped clients. |
| **redis-streams-bus** | ⚠️ Partial | Stream-per-tenant + consumer group + `BUSYGROUP` handling present. **Missing the skill's mandatory `MAXLEN`/`XTRIM` trimming** (memory-bound) and `XAUTOCLAIM` pending recovery; full-keyspace scans (P-06); ack-on-failure (C-01). |
| **inference-client** | ✅ Mostly | One OpenAI-compatible client, env-switched Fireworks⇄vLLM, offline fallback, structured-output path. Gaps: malformed-body handling + fallback-on-failure (D-11); untested branches (T-06). |
| **postgres-pgvector-rag** | ⚠️ Partial | Relational schema + RLS + recursive-CTE detective present. **No pgvector/embeddings/top-k retrieval implemented** (the "grounded retrieval" half); RLS is inert in shipped config (SEC-01). |
| **fastmcp-tools** | ⚠️ Partial | Tool surface + risk tiers + idempotent mocked write-back exist; `register_platform_mcp` is built but **not mounted** (DEBT-10). Deferred-load/top-k tool search not wired. |
| **react-ops-console** | ⚠️ Partial | Chat-first console with evidence records, HITL approve/reject, decision log — all present and **locked as final**. Deviation: **no WebSocket client** (polling instead) and **no Recharts** (README correctly makes no such claim). This is acceptable given the visual lock; noted for accuracy only. |
| **seed-sa-retail-data** | ✅ Yes | Four SA-retail CSVs, planted yoghurt SKU 4011 story, ingestion→Event mapping, ZAR. Matches the skill. |
| **golden-scenario-eval** | ⚠️ Partial | Golden storyline encoded, per-agent + end-to-end assertions, visible critic rejection + learning moment. **Token/cost ceiling assertion not enforced** in the eval gate; failure-path/boundary coverage thin (T-06/T-08). |
| **vllm-rocm-deploy** | ✅ Proven live | User stood up vLLM serving Qwen2.5-14B on an MI300-class ROCm node (see §7). Compose keeps GPU separate per skill. Credit-hygiene (never leave GPU up) still applies. |
| **docker-compose-multi** | ⚠️ Partial | backend+frontend+postgres+redis compose exists, non-root hardened. Deviations: images tag-pinned not digest-pinned (DEPS-02); weak default DB creds + published port (SEC-09); runtime image ships tests+dev deps (DEPS-01). |
| **openspec-driven** | ❌ Not adopted | No `openspec/` directory; changes are ad-hoc commits, not spec-proposals. Optional; not blocking. |
| **git-commit-hygiene** | ✅ Yes | History reads as human, Conventional Commits, **no AI attribution** anywhere in the log. Compliant. |
| **get-shit-done** | ✅ Yes | Vertical golden slice works end-to-end; the "demo = done" discipline is visible in the tested cascade. |
| **context-window-discipline** | n/a | A build-process skill for agents, not a code property. |
| **frontend-craft** | ⚠️ Partial | Four-state discipline mostly present; god-component structure (DEBT-04) and unguarded catch (D-10) violate "one component one job" / streaming cleanup. Fixes are internal-only (visual lock). |
| **browser-verify** | ⚠️ Process | Tooling available; frontend has no behavioral test layer (T-04/T-05). |
| **deploy-demo** | ◻️ Pending | Free-tier hosting path documented; live public URL still "Next" in README. |
| **submission-deliverables** | ⚠️ Partial | README/JUDGE/DEMO_RUNBOOK exist; proof-package artifacts (harness-evidence, scale matrix, deck) not assembled (backlog #25/#29). |
| **Rule: no AI attribution** | ✅ Honored | Clean git log/authorship. |
| **Rule: free-tier / MIT-only** | ✅ Honored | Deps are MIT-clean; `npm audit` clean; no paid services. |
| **Rule: cloud inference only** | ✅ Honored | Fireworks/vLLM via one client; no laptop models. |
| **Rule: no secrets in repo** | ⚠️ Watch | `.env.example` placeholders are empty (good), **but** a weak default DB password is committed (SEC-09), and a real `VITE_API_KEY` would ship in the bundle (SEC-06). |
| **Rule: no temporary fixes** | ⚠️ Watch | Several "demo slice" stopgaps remain load-bearing: scenario-keyed routing (`sku=="4011"`), in-memory singletons as the default path, fixed-id sales decision (C-05). Tracked below. |

---

# §3 — Blueprint-vs-code gap map

Which planned domains (`plot/domains/`) are genuinely running code in `src/`, verified by finding
the files + a test, not by trusting doc claims.

| Domain | Verdict | Code evidence | What's missing |
|---|---|---|---|
| 01 contracts | ✅ Implemented | `contracts/__init__.py` (Money/SourceRef/Event/EvidenceObject/Decision) | Hand-rolled dataclasses not Pydantic v2; IDs uuid4 not ULID |
| 02 backend | ✅ Implemented | `app.py`, `cascade.py`, `worker/`, `chat.py`, `detective.py`, bus/store | Router scenario-keyed (`sku=="4011"`); real per-SKU orchestration missing |
| 03 data | ✅ Implemented | `data/seed.py`, `csv_connector.py`, `context.py`, `store_intelligence.py` | 4 planted products only (by design at this stage) |
| 04 frontend (+ design system) | ✅ Implemented (locked) | `frontend/src/App.tsx`, `theme.ts` | Scale IA (virtualization/cursor paging) — additive only, no visual change |
| 05 infra | ✅ Implemented | `docker-compose.yml`, `Dockerfile`, `frontend/Dockerfile` | Image digest pinning, secret hygiene (SEC-09, DEPS-02) |
| 06 eval-demo | ✅ Implemented | `shelfwise_eval/harness.py`, `__main__.py` | Scale/usefulness scenarios (backlog #16/#24) |
| 07 connectors | ✅ Implemented | `connectors/` (canonical, provenance, validation, identity, normalize, inbound, writeback, gateway) | Bulk delta ingestion (#3); mapper bugs D-01/D-05/D-08 |
| 08 per-system connectors | ✅ Implemented | `connectors/systems/{sap,odoo,syspro,shopify,square,lightspeed}.py` | Open-PO/fill-rate depth; multi-line mapping (D-01) |
| 09 synthetic-data | ✅ Implemented | `synthdata/generators.py`, `schema.py`, `eval_at_scale.py` | Throughput/scale-load harness (#23) |
| 09b scenario-simulator | ✅ Implemented | `worldgen/world.py`, `play.py`, `scenarios.py`, `drills.py` | — |
| 09c catalog/taxonomy | ⚠️ Partial | `worldgen/catalog/` (generate, taxonomy, grammar, gs1, brands) | **50k–500k SKU target unmet**: hypermarket caps ≈ 24,675 (`generate.py`) |
| 09d cold-chain-resilience | ✅ Implemented | `resilience/` (thermal, diagnose, telemetry, valuation, simulate, alerts, feed, ingest) | — |
| 10 multitenant hosting/scaling | ⚠️ Partial | `storage/rls.py`, `tenant_profiles.py`, `backend/tenant.py`, RLS schema, queue worker | RLS inert in shipped config (SEC-01); partitioning/retention (#7) |
| 11 mlops/finetuning | ✅ Implemented | `mlops/` (registry, gate, skills, cost, accountability, finetune, routing, facts, memory_consolidation) | `finetune.py` is a ~50-line export stub |
| 12 demo/judge/runbook | ✅ Docs | `JUDGE.md`, `DEMO_RUNBOOK.md` | Quick Start install step (DOCS-02) |
| 13 decision-science | ✅ Implemented | `decision_science/` (forecasting, forecast_tsfm, expiry, cold_chain, inventory, anomaly, optimization, simulation, relations, tools) | Weight/gate bugs D-06/D-07/D-09 |
| 14 voice/multimodal | ✅ Implemented | `multimodal/` (stt, tts, vision, router, voice_intake, speech_style, text_normalize) | Crash-safety D-02/D-03 |

**Bottom line:** the vertical spine is genuinely built and tested end-to-end. The gaps are (a) the
scale substrate the "Things" file targets, and (b) the correctness/security fixes above — not missing
domains.

---

# §4 — "Things that needs to be implemented" — verified status

Every one of the 29 items, re-checked against the code. This is the "Things" file's own backlog with
a verified status column; nothing is dropped, and it flows into §5.

| # | Item (priority in Things file) | Status | Evidence / gap |
|---|---|---|---|
| 1 | Product master + variant model (P0) | **NOT STARTED** | `ProductMaster` (`connectors/canonical.py`) is thin; none of the 9 tables exist; variant/pack model lives only in synthetic `worldgen/catalog/model.py` |
| 2 | Batch/lot/expiry model (P0) | **PARTIAL** | FEFO logic + `batch_split` (single seeded SKU); GS1 lot/expiry parse. No `inventory_positions`/`inventory_batches`/`expiry_observations` tables; `InventoryState` has no `batch_id`; schema has no batch grain |
| 3 | Bulk + incremental ingestion (P0) | **PARTIAL** | Idempotent upsert by `(tenant, source, raw_hash)`; quarantine. No bulk/streaming CSV, no high-water-mark delta, no COPY bulk-load; single-event ingest |
| 4 | Production event/state architecture (P0) | **PARTIAL** | Typed `tenant_id`/`event_type`/`event_ts`; `product_state` current table. `sku`/`location_id`/`batch_id` still in JSONB; **no PARTITION BY**; no materialized feature snapshots |
| 5 | Candidate generation before agent reasoning (P0) | **PARTIAL** | `product_attention_queue()` ranks + bounds. Runs over 4 seed products; no fleet-wide scoring, none of the 11 candidate types, no pending-HITL suppression |
| 6 | Scale-ready worker runtime (P0) | **NOT STARTED** | `CascadeWorker` is single-event, `count 1`, **acks on failure** (C-01). No batch consume, retry/backoff, dead-letter, pending recovery, backpressure |
| 7 | Partitioning, indexes, retention (P0) | **PARTIAL** | Many btree indexes. No `PARTITION BY`, no BRIN, no retention/archive, no partition+RLS migration tests |
| 8 | Context assembler (P0) | **PARTIAL** | `build_context()` dict helper only. No `ContextAssembler`, no token estimate / missing-data list / source ranking / context manifest in trace |
| 9 | Product-policy registry (P1) | **NOT STARTED** | Product logic hardcoded in cascades; `mlops/registry.py` is model/prompt, not product policy |
| 10 | Fleet-wide scoring jobs (P1) | **NOT STARTED** | Generic `Scheduler` exists; no expiry/stockout/overstock/supplier/cold-chain/missing-data scoring over product-location-batch state |
| 11 | Demand/forecast feature store (P1) | **PARTIAL** | Baseline + TSFM shadow guardrail. No `daily_product_sales`/feature/forecast tables, no stockout adjustment, no promo/holiday calendars |
| 12 | Identity resolution workflow (P1) | **PARTIAL** | Exact match + GS1 parse. No fuzzy/brand/pack match, no confidence, no human review queue, no merge/split audit |
| 13 | Evidence quality scoring (P1) | **PARTIAL** | `avg_source_quality`, per-inbound `source_quality`, evidence `confidence`. No composite recommendation-level score, no "thin evidence → monitor" rule |
| 14 | Exception queue + HITL workload (P1) | **PARTIAL** | Bounded attention queue + writeback tasks + approve/reject. No SLA clock, dedup, bulk approve, escalation, role workload cap, "why not shown" |
| 15 | Observability for scale (P1) | **PARTIAL** | Decisions/tokens/cost/connector/events/worker/recovered metrics. Missing queue lag, oldest-pending age, throughput, dead-letter, scoring duration, candidate/rec counts, HITL backlog, avg evidence quality, cost-per-1k, stale-by-source |
| 16 | Scale evaluation harness (P1) | **NOT STARTED** | Golden/critic/catalog/reconcile checks only. No 500k scenario, no throughput/memory/latency/candidate-reduction/false-neg metrics |
| 17 | Frontend IA for 500k (P1) | **PARTIAL** | Attention views + server-side `/products/search` (capped 50). No cursor paging, no virtualized batch/lot tables, no data-quality queue. **Additive only — visual lock** |
| 18 | Model routing + cost controls (P2) | **PARTIAL** | `choose_model_route` + cost economics + `estimated_cost`. No per-decision token budget enforcement, no per-tenant/day cap, no provider-fallback trace |
| 19 | Data governance + tenant controls (P2) | **PARTIAL** | FORCE RLS (inert — SEC-01), JWT+roles, connector allow-list, stack-trace suppression. No identity-merge/policy-change audit logs, no explicit PII minimization/retention |
| 20 | Reconciliation + correction loops (P2) | **PARTIAL** | Delivery-vs-supplier reconcile only. No POS-vs-inventory, WMS-vs-POS, batch-vs-shelf-life, source-lag/missing/dup-feed detection, correction events |
| 21 | Multi-tenant/multi-store scheduling (P2) | **PARTIAL** | `Scheduler` (interval, overlap skip). No per-tenant/store windows, priority, max-concurrent, backoff, replay/pause, job history/receipts |
| 22 | API pagination + query contracts (P2) | **PARTIAL** | `limit` + caps on list endpoints. No cursor pagination, no sort/filter contracts; observability builds totals from full in-memory lists (P-03/P-04) |
| 23 | Load testing + capacity reports (P2) | **NOT STARTED** | No `scale_profile_500k.json`, load-test script, or capacity report |
| 24 | Usefulness + noise evaluation (P2) | **PARTIAL** | `observe_adversarial` (injection/citation) + critic-rejection/HITL-resolution rates. No precision/duplicate/suppression/false-urgency/useful-actions-per-day metrics; none of the 8 usefulness scenarios |
| 25 | Hackathon proof-package alignment (P2) | **PARTIAL** | README/JUDGE/DEMO_RUNBOOK/IMPLEMENTATION_STATUS + demo endpoints. No `harness-evidence.md`, scale/readiness matrix, or connector-capability matrix; 5-slide deck not in repo |
| 26 | Existing-doc contradiction cleanup (P2) | **PARTIAL (mostly done)** | `IMPLEMENTATION_STATUS.md` distinguishes blueprint-vs-running-code. Stale inline rows remain (DOCS-01); participant-guide→artifact checklist not present |
| 27 | Harness receipt artifact (P3) | **PARTIAL** | `cascade_runs`/`cascade_steps` journal + decision verdict/HITL + `TraceSpan`. No single consolidated receipt (context manifest, candidate/evidence-quality score, model route, token estimate, eval result) |
| 28 | 10-primitives implementation matrix (P3) | **NOT STARTED** | No primitives-matrix artifact |
| 29 | Public proof package (P3) | **NOT STARTED** | Individual demo endpoints exist; no assembled package (matrix, benchmark report, 500k run summary, identity/batch diagrams, cost-per-decision) |

---

# §5 — Unified prioritized backlog (P0 → P3)

The actionable synthesis. It merges the audit findings (§1) with the Things items (§4),
de-duplicated and dependency-ordered. **P0 leads with the fixes that make the current app correct and
safe** (cheaper, higher-confidence, and prerequisites for trusting anything at scale), then the
scale substrate the Things file targets. Each item lists a machine-checkable done criterion. Frontend
items are additive/internal — **rendering preserved**.

### P0 — correctness & safety (do first; small, high-confidence, unblock trust)

1. **Fix the RLS bypass + fail-open auth** (SEC-01, SEC-02, C-06). Create a `NOSUPERUSER NOBYPASSRLS`
   app role with least-privilege GRANTs; connect as it; add a boot assertion that refuses to start if
   the role is superuser/bypasses RLS. Default `SHELFWISE_AUTH_MODE=jwt` (fail closed off-local).
   *Done when:* a new integration test proves tenant B cannot read tenant A's rows through the app,
   and the app refuses to boot as a superuser role.
2. **Stop the worker dropping failed events** (C-01, backlog #6 first slice). Don't `XACK` on failure;
   add a retry counter + dead-letter stream; requeue on the in-memory bus. *Done when:* a test forces
   a handler exception and asserts the message is redelivered/dead-lettered, not acked.
3. **Add tenant-ownership checks to decision endpoints** (SEC-03, SEC-04, SEC-05). Require the tenant
   dependency on `GET /decisions/{id}`, `/decisions`, `/events*`, `/traces`, `/learning`,
   `/mlops/accountability`; compare `decision.tenant_id` to the token tenant on approve/reject; derive
   tenant from token, not query param. *Done when:* cross-tenant approve/read returns 403/empty in tests.
4. **Gate/secure demo & worldgen write endpoints** (SEC-08). Attach `write_path_guard`+rate-limit,
   make mutation POST-only, cap worldgen work/request. *Done when:* anonymous `GET /scenarios/*` no longer
   mutates state (test asserts 405/401).
5. **Fix the sales-decision terminal-state freeze** (C-05). Derive the sales decision id per event
   (as cold-chain does), or don't auto-approve into a terminal shared id. *Done when:* a clean sale
   followed by a price-exception sale yields a distinct PENDING decision (test).
6. **Fix the connector mapper data-loss + crashes** (D-01, D-05, D-08). Map every line/count to its own
   `InboundRecord` with a per-line `source_object_id`; `.get()` with fallbacks; coerce quantity via
   `Decimal`; standardize event `unit_price` on integer minor units. *Done when:* a 3-line order test
   produces 3 sales records with correct cents.
7. **Harden the ingest write ordering + body guard** (C-03, C-07). Publish-then-record (or outbox);
   enforce a streamed byte ceiling, not just `Content-Length`. *Done when:* a simulated bus failure
   leaves the event replayable, and a chunked over-limit body is rejected (tests).
8. **Add locks to `InMemoryJournal` + `TraceRegistry`** (C-04). Mirror the sibling stores' `threading.Lock`.
   *Done when:* a concurrent put/list stress test raises no `RuntimeError`.
9. **Move the write-path key off the browser** (SEC-06, DX-02). Authorize writes with the tenant JWT;
   reserve `API_KEY` for server-to-server; never expose it via `VITE_`. Document all env vars incl. a
   `frontend/.env.example`. *Done when:* the built bundle contains no API secret (grep) and writes
   authorize via JWT.
10. **Crash-safety for multimodal + inference + poll** (D-02, D-03, D-11, D-12). Guard month/day
    ranges; broaden `scan_image` fallback; wrap malformed-200 bodies as `InferenceError` (+ decide
    offline fallback); break the poll loop on a non-advancing cursor. *Done when:* fuzzed inputs
    return safe fallbacks, not 500s (tests).
11. **Verify/fix the decision-science coefficients** (D-06, D-07, D-09). Confirm intent of the 1.10
    expiry weight sum and the always-on 1.35 payday multiplier; make `should_reorder` consistent with
    `suggested_order_units`. *Done when:* documented weights sum to 1.0 (or the >1 is justified in a
    comment + test), and reorder-at-equality suggests ≥1 unit or flips to strict `<`.
12. **Test the reject path, money math, Postgres/Redis round-trips, and RLS enforcement** (T-01, T-02,
    T-03, T-06, T-07). Add a `@pytest.mark.postgres` tier (testcontainers or CI service) running the
    store contract + real RLS isolation; add `test_contracts_money`; add inference-branch tests.
    *Done when:* those tests exist and pass in CI.

### P0 — scale substrate foundations (from the Things file; needed before fleet scoring)

13. **Product master + variant + identity model** (#1, #12). The 9 normalized tables; internal
    SKU/GTIN/barcode/PLU/supplier-code/alias; primary-vs-variant; pack hierarchy; fuzzy+exact search;
    human review for uncertain merges. *Done when:* a migration + round-trip test resolves "milk"
    variants to stable ids across two source systems.
14. **Batch/lot/expiry model** (#2). `inventory_positions`, `inventory_batches`, `expiry_observations`,
    `waste_events`, `stock_adjustments`; FEFO selection per batch; remaining-shelf-life; cross-system
    expiry-conflict. *Done when:* "which milk batch to discount first" is answerable from the schema.
15. **Bulk + incremental ingestion + typed hot columns + partitioning** (#3, #4, #7). Bulk import
    (COPY), high-water-mark delta, idempotent upserts, quarantine, promote `sku`/`location_id`/
    `batch_id`/`event_type`/`event_ts` out of JSONB, `PARTITION BY` events/inbound by date, BRIN on
    append-only, retention/archive. *Done when:* a 100k-row import runs without per-row inserts and
    partition+RLS migration tests pass.

### P1 — the "decision factory" (scoring & prioritization the Things file centers on)

16. **Candidate generation + fleet-wide scoring jobs** (#5, #10). Deterministic scoring over
    product-location-batch state for the 11 candidate types; rank by exposure/urgency/confidence;
    top-N per store/day; suppress pending-HITL; persist scores + deltas. *Done when:* a scoring run
    over N products emits a bounded ranked candidate set, no LLM per product.
17. **Context assembler** (#8). First-class `ContextAssembler` emitting a compact cited bundle + token
    estimate + missing-data list + confidence + manifest into the trace. *Done when:* a decision's
    trace shows exactly the facts fed to the model.
18. **Evidence-quality scoring + product-policy registry** (#13, #9). Composite score
    (coverage/freshness/agreement/missing-fields/confidence); "thin → monitor", "missing expiry →
    data-completion task"; move product logic into configurable policies. *Done when:* a thin-evidence
    candidate downgrades to monitor in a test.
19. **Demand/forecast feature store + reconciliation** (#11, #20). Feature tables, stockout-adjusted
    sales, promo/holiday calendars, TSFM shadow-test-before-switch; POS-vs-inventory, WMS-vs-POS,
    batch-vs-shelf-life reconciliation + correction events. *Done when:* forecasts read persisted
    features and reconciliation emits provenance-tagged corrections.
20. **Exception queue + HITL workload + scale observability** (#14, #15). SLA clock, dedup, bulk
    approve (low-risk only), escalation, role workload caps, "why not shown"; scale metrics (queue
    lag, oldest-pending age, throughput, dead-letter, scoring duration, candidate/rec counts, HITL
    backlog, avg evidence quality, cost-per-1k, stale-by-source). *Done when:* the observability
    snapshot reports all listed metrics.
21. **Frontend scale IA — additive, rendering preserved** (#17, DEBT-04). Behind the existing
    chat-first UI: server-side cursor pagination, virtualized batch/lot tables, data-quality queue —
    delivered by decomposing `App.tsx` into per-surface components that render byte-identically.
    *Done when:* the new surfaces load 100k rows without unbounded client arrays and screenshots match
    the current UI pixel-for-pixel.

### P2 — hardening, cost, evaluation, performance

22. **Performance pass** (P-01…P-08). Connection pool (with per-checkout RLS reset), `returning`-based
    upserts, SQL-aggregate observability, bounded decision `list()`, catalog/seed memoization, Redis
    stream registry (no keyspace scan), detective index, CI pip cache. *Done when:* a load test shows
    the connect-per-query and unbounded-list hot paths gone.
23. **Model routing + cost controls + governance** (#18, #19). Per-decision token budget, per-tenant/day
    cost cap, provider-fallback trace; identity-merge/policy-change audit logs, PII minimization,
    retention. *Done when:* a decision over budget is blocked and audited.
24. **Scale + usefulness evaluation + load testing** (#16, #23, #24, T-08). 500k scenario, capacity
    report, the 8 usefulness scenarios (10k low-risk → small ranked queue), precision/suppression/
    false-urgency metrics, cascade gate-boundary characterization tests. *Done when:* the eval proves
    "10k low-risk products → a small ranked work queue, not 10k alerts."
25. **DX/tooling** (DX-01, DX-03, DX-04, DEPS-01, DEPS-02, T-04, T-05). Add `mypy src` (non-strict →
    ratchet) + a Vitest frontend behavioral tier + pre-commit; single dependency manifest + multi-stage
    runtime image (no tests/dev deps); digest-pin images; make the Makefile Windows-safe. *Done when:*
    CI runs mypy + frontend tests and the runtime image excludes test/dev tooling.

### P2/P3 — tech-debt refactors (quality; do behind characterization tests)

26. **Decompose the god modules** (DEBT-01, DEBT-02, DEBT-05, DEBT-07). Extract per-domain routers +
    a `cascade_service`; collapse the 5 cascade builders behind an `EvidenceBuilder` + shared
    envelope; merge GET/POST demo handlers; give `eval` a `create_app()` container instead of importing
    singletons. *Done when:* `/scenarios/*` responses are byte-identical (captured characterization tests)
    and `app.py`/`cascade.py` shrink materially.
27. **De-duplicate infrastructure** (DEBT-03, DEBT-06, DEBT-08, DEBT-09, DEBT-10). One backend-selection
    factory + `now_iso()` + `Store` protocol; narrow the broad `Decimal` excepts; route money conversion
    through `Money`; one settings object at the composition root; wire or quarantine the MCP surface.
    *Done when:* the `_now()`/factory duplication is gone and ruff/tests stay green.
28. **Retire the "temporary fixes"** (no-temporary-fixes rule). Replace scenario-keyed routing
    (`sku=="4011"`, `supplier=="dairyco"`) with the policy registry (item 18) as scenarios grow; make
    Postgres/Redis the default runtime path with the in-memory mode explicitly test-only.

### P3 — proof package (Track 3 judging)

29. **Doc cleanup + proof artifacts** (#25, #26, #27, #28, #29, DOCS-01, DOCS-02). Fix the stale
    scope-freeze rows in place; add the `pip install` Quick Start step; assemble `harness-evidence.md`,
    the scale/readiness matrix, connector-capability matrix, harness receipt, 10-primitives matrix,
    5-slide deck outline, and the AMD compute proof (§7). *Done when:* a judge can see usefulness +
    AMD proof from the repo without reading code.

---

# §6 — Direction findings (options, not defects)

Forward-looking, grounded in the repo. These are for the maintainer to weigh, not ranked against bugs.

- **The MCP surface is one route away from being real** (DEBT-10). `register_platform_mcp` is built,
  tested, and idempotent but unmounted. Exposing the ShelfWise tools as an actual MCP endpoint is a
  cheap, differentiating "agent-native platform" story for judges — grounded in existing code, not
  net-new. *Effort: S–M (spike).*
- **Real token streaming on `/chat`** (fastapi-service deviation). The frontend and contracts already
  assume streaming; `/chat` fakes it with `_chunk_words`. Wiring the inference client's token stream
  through would make the "watch it think" demo genuinely live with little new surface. *Effort: M.*
- **pgvector grounded retrieval is specified but absent** (postgres-pgvector-rag). The schema and
  decision log exist; adding the embeddings + top-k retrieval the skill describes turns the decision
  log into a "have we seen this before" memory — the learning story the demo already gestures at.
  *Effort: M–L (design/spike first).*
- **The synthetic worldgen is the simulator the mandate asks for** (09b/09c). It already emits
  canonical events; pointing the fleet-scoring jobs (item 16) at a continuously-generating worldgen
  store — rather than static seed — is the "robotics-sim for retail" deliverable, reachable from
  existing code. *Effort: L; sequence after P0/P1.*

---

# §7 — AMD compute proof (status)

Evidence the "built on AMD" requirement is satisfiable **now**: during this session the owner stood
up vLLM (`0.16.1.dev0`, torch `2.9.1`, ROCm 7.2.1, `gfx1100`) serving `Qwen/Qwen2.5-14B-Instruct`
with `--served-model-name shelfwise-routine shelfwise-strong`, and confirmed `/v1/models` responds
over the OpenAI-compatible API. This is exactly the `inference-client` + `vllm-rocm-deploy` path the
repo is designed around — point `LLM_BASE_URL` at that endpoint and the same client that uses
Fireworks runs on AMD.

**To convert this into judgeable proof (backlog #25/#29):** capture (a) a tokens/sec benchmark from
that node, (b) a recorded trace of a ShelfWise cascade routed to the vLLM endpoint, and (c) a short
`harness-evidence.md` / deck slide with the `rocm-smi` product line + model list. **Credit hygiene:**
shut the GPU down between benchmark/recording runs.

> **Security note (not a repo finding):** the pasted terminal log contained a live vLLM API key in
> plaintext. It's an ephemeral hackathon node, but rotate that key if the node stays up, and keep it
> out of any committed file or screenshot.

---

# §8 — Considered and rejected (so they aren't re-audited)

- **Double-approve of a decision** — actually idempotent (terminal-state guard + learning-store
  dedup). Not a bug.
- **Money float leakage** — none; `Money` is integer minor-units throughout. (The *duplication* of the
  conversion is DEBT-08; the arithmetic is correct.)
- **Shared-singleton order dependence in tests** — handled by the autouse reset in `conftest.py:25-49`.
  A strength, not a finding.
- **README claims WebSocket/Recharts but package.json lacks them** — investigated and **false**: the
  README makes no such claim; the code uses neither. No missing-dependency gap.
- **`smoke.py` / `smoke.ps1` duplication** — resolved; `smoke.ps1` now delegates to `smoke.py`.
- **In-memory stores/bus, mocked write-back, CSV/mock connectors, fixed demo clock, deterministic
  seeded scenario** — all intentional by design; the scale backlog (§4/§5) is the real substrate work.
- **Scenario-keyed cascade routing (`sku=="4011"`)** — known tracked debt, folded into P2/P3 item 28,
  not re-reported as a fresh bug.
- **Modular monolith vs microservices** — a locked architectural decision, not a finding.

---

*Generated by a read-only `improve` audit. No source code was modified. The frontend's appearance is
locked; every item here is additive capability, a correctness/security fix, or an internal refactor
that preserves the exact rendered UI.*
