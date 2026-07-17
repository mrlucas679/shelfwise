# ShelfWise Implementation Plan

> **Working-product branch boundary:** This plan is executed on the post-hackathon `developers`
> branch. `main` remains the protected working-product branch; do not commit or merge plan work
> there unless an explicit release decision is recorded.

Written 2026-07-12 against `main` @ `01ccc9e` (PR #5 merged). This plan is self-contained:
an implementing agent should be able to execute it top-to-bottom without any other context.
Work the tasks IN ORDER. Do not start a task until the previous one's acceptance criteria pass.

> **2026-07-14 status note.** Substantial work has landed since this plan was written, including
> a Postgres-backed generated world (`WorldFactsProvider` + `shelfwise_world_snapshot`, memory and
> Postgres backends, real integration tests) that closely matches TASK 4's intent below. Before
> starting any task in this file, verify its acceptance criteria against the current code first
> (per Ground Rule 8) rather than assuming it is still pending — `IMPLEMENTATION_STATUS.md` and
> `HANDOFF.md` are the current source of truth for what's already built.

## Ground rules (read before every task)

1. **No AI attribution anywhere** — commits, PRs, comments. No "Co-Authored-By" AI trailers.
2. **Free/open-source only.** Never add a dependency that bills or needs a subscription.
   New deps must be MIT/Apache/BSD-licensed.
3. **Cloud inference only** — the model endpoint is AMD MI300X/vLLM. Never add local model
   inference. Never re-enable strict `json_schema` response_format against vLLM/Gemma
   (known infinite-whitespace bug; text mode + schema-in-prompt + post-hoc validation only).
4. **Repo contracts that WILL fail your build if ignored:**
   - Any new/changed backend route requires, in the same change: (a) an entry in
     `GATED_ENDPOINTS` or `OPERATION_READ_ENDPOINTS` in `frontend/src/App.tsx`, (b) a line in
     README.md's `Connected API endpoints:` list (kept sorted; test parses the section between
     that heading and `## Smoke`), (c) regenerated manifest:
     `python scripts/compare_capability_manifests.py --write`.
   - After ANY change: `$env:PYTHONPATH="src"; python -m pytest -q` (all tests must pass) and
     `python -m ruff check src tests scripts` (clean).
5. **Backend does not auto-reload.** After editing backend code, restart:
   kill the process on :8000, then
   `set -a && source .env && set +a && python -m uvicorn shelfwise_backend.app:app --host 0.0.0.0 --port 8000 --app-dir src` (bash)
   from repo root.
6. **Verify live, not just tests**, for anything user-visible: drive the actual UI/HTTP route
   and paste the evidence into the commit message or PR description.
7. **Commit incrementally** — one logical change per commit, never leave the tree broken.
8. If a task turns out to be already implemented (someone else may be working in parallel),
   verify it against the acceptance criteria, note it, and move on — do not redo it.

## Already DONE — do not re-implement (verified 2026-07-12 @ 01ccc9e)

- Production topology: Nginx public on 80, backend internal-only, 8000/8001 free for vLLM
  (`docker-compose.production.yml`), one-command deploy, CI production-topology smoke
  (`.github/workflows/ci.yml` boots the prod compose and curls through Nginx).
- Fail-closed auth: `APP_ENV=production` + `SHELFWISE_AUTH_MODE=jwt` in prod compose;
  `_reject_insecure_auth_in_named_deployments` (`src/shelfwise_backend/app.py:119-136`)
  raises at boot if a named deployment has auth off.
- Trace tenant isolation: `CascadeTrace.tenant_id` exists; `/traces`, `/trace/{id}`, and chat
  context are tenant-filtered.
- Postgres chat persistence: `create_chat_store()` factory honoring `SHELFWISE_STORE_BACKEND`
  (`src/shelfwise_backend/chat_store.py:247`), `PostgresChatConversationStore` wired.
- Recall + inventory-exception demos accept `TenantContext`; `/tools/platform/audit` scoped.
- Physical inventory position ledger: `shelfwise_inventory_positions` table (shelf/backroom/
  bin, task receipts), `/inventory/positions` GET/POST, tenant-scoped.

---

## TASK 1 (small, do first): tenant-scope the last two unscoped routes

**Problem:** `/tools/platform` (`app.py:968`) and `/learning` (`app.py:1690`) do not take
`TenantContext`; `/learning` returns the whole learning store regardless of caller tenant.

**Steps:**
1. Read how `/tools/platform/audit` (`app.py:974`) injects `CURRENT_TENANT_DEP` and filters —
   copy that pattern.
2. `/tools/platform`: the tool catalog itself is tenant-neutral metadata (name/description/
   read_only) — scoping the route means requiring an authenticated tenant, not filtering rows.
   Add `ctx: TenantContext = CURRENT_TENANT_DEP` to the signature so the route participates in
   auth like its sibling.
3. `/learning`: filter both `events` and `thresholds` by `ctx.tenant_id`. Check the learning
   store interface (`src/shelfwise_memory/__init__.py`) for existing tenant filtering; if
   `list_events()` has no tenant parameter, add an optional `tenant_id` filter to BOTH the
   in-memory and Postgres implementations (rows already carry tenant ids in Postgres;
   in-memory events store whatever the cascade recorded — verify field name).
4. Check the two call sites of `learning_store.list_events()` in `app.py` chat context
   assembly — pass the tenant there too if not already.
5. Tests: extend `tests/test_tenant_auth.py` with: two tenants write learning events; each
   tenant's `/learning` shows only its own. Run the full suite; regenerate the capability
   manifest; update the README endpoints line only if the method/path signature changed
   (it shouldn't — same path, added dependency).

**Acceptance:** cross-tenant `/learning` read returns only caller-tenant rows; full suite
passes; ruff clean.

---

## TASK 2: frontend auth — finish the JWT/session story honestly

**Current state (verified):** `ensureBrowserSession()` in `frontend/src/App.tsx` calls
`/auth/session`; requests still authenticate via `x-api-key` only; a comment says company
login is future work. Multi-user isolation is API-tested but not exercisable through the UI.

**Goal:** a user can pick/enter an identity in the UI, the frontend sends
`Authorization: Bearer <jwt>` on every request, and two browser profiles demonstrably see
isolated conversations/decisions. Keep it minimal — this is session identity, not a full
account system (no registration, no password reset).

**Steps:**
1. Read `src/shelfwise_backend/tenant.py` (JWT encode/decode, `encode_hs256_token`) and the
   existing `/auth/session` route in `app.py` — understand what it returns today.
2. Backend: extend `/auth/session` (or add `POST /auth/login` if cleaner) to mint a signed
   JWT for a demo identity: body `{tenant_id, user_id, role}` gated appropriately —
   in production mode this must NOT allow arbitrary tenant minting without the API key;
   check how `SHELFWISE_AUTH_MODE=jwt` + `TENANT_AUTH_SECRET` are enforced and follow suit.
   Any new/changed route: README endpoints list + frontend catalog + manifest regen (rule 4).
3. Frontend: add a small session panel (sidebar footer area near the existing user chip
   `keo · Pro`): show current tenant/user/role; a "switch user" affordance with 2-3 preset
   demo identities (e.g. store manager @ sa_retail_demo, owner @ sa_retail_demo, manager @
   other_tenant). On selection: call the login route, store the JWT in memory + localStorage,
   and send `Authorization: Bearer <token>` in `fetchJson` (find the single fetch helper at
   ~`App.tsx:314` — add the header there once, do not scatter).
4. Keep `x-api-key` support unchanged (harness/tests use it).
5. Tests: backend test for the login route (valid mint, rejected when auth mode off in named
   deployment); frontend `npx tsc --noEmit` clean.
6. Live verification: run backend+frontend, switch identity in the UI, confirm `/chat`
   response headers show the right tenant isolation (create a conversation as tenant A,
   switch to tenant B, confirm `/chat/conversations` differs).

**Acceptance:** two identities switchable in the UI with visibly isolated data; suite passes;
manifest/README/frontend-catalog in sync.

---

## TASK 3: Playwright browser E2E suite (largest new piece)

**Current state:** no browser E2E at all. CI builds the frontend but never opens it.

**Steps:**
1. `cd frontend && npm install -D @playwright/test` (MIT). `npx playwright install chromium`.
   Config `frontend/playwright.config.ts`: baseURL `http://127.0.0.1:5173`, one chromium
   project, `webServer` entries able to reuse an externally started backend+frontend
   (`reuseExistingServer: true`).
2. Test files under `frontend/e2e/`. Write these specs, smallest first:
   - `smoke.spec.ts`: app loads, sidebar renders, zero console errors.
   - `chat.spec.ts`: send a message, assistant bubble appears with structured content;
     reload page, conversation persists (requires Postgres backend or accepts in-memory
     single-process persistence — assert on same-session persistence via conversation id).
   - `approvals.spec.ts`: open approval queue, approve one decision (confirm dialog), row
     moves to resolved; reject the other.
   - `agentic.spec.ts`: Operations workspace → click one "(agentic) - click to run live" row
     → row shows running state. IMPORTANT: when the live model endpoint is down this returns
     an error state — the test must accept EITHER `ok` (live) or a clean `error` state
     (offline), and assert the UI never shows a raw stack trace. Do not make CI depend on the
     GPU droplet being up.
   - `drills.spec.ts`: recall drill and inventory-exception drill run from the Operations
     workspace and produce visible results.
   - `isolation.spec.ts` (after TASK 2): two browser contexts with different identities; each
     sees only its own conversations.
   - `outage.spec.ts`: with backend stopped (or a blackholed API base), UI shows the friendly
     error card with Retry, not a blank screen.
3. CI: new job in `.github/workflows/ci.yml` — start backend (memory mode, no model
   endpoint), `npm run dev` or preview build, run `npx playwright test`. Keep it under ~5 min;
   upload the Playwright HTML report as an artifact on failure.
4. Do NOT point CI at the live MI300X endpoint. Everything must pass offline.

**Acceptance:** `npx playwright test` green locally against a running stack; CI job green;
no test depends on the GPU being up.

---

## TASK 4 (flagship): replace seed/demo data with a Postgres-backed generated world

This is the "it's not a demo" fix. Full research already done; findings below are verified.

**Architecture decisions already made (do not relitigate):**
- One-time deterministic population now, but built as a reusable world-population SERVICE so
  continuous simulation can be enabled later behind a flag (`SHELFWISE_WORLD_MODE=static`
  default; `continuous` reserved. The seam is implemented — `world_mode()` in
  `shelfwise_worldgen.populate`, static default, unknown modes rejected loudly, and
  `continuous` fails honestly pointing at the full-system harness's world rotation, which is
  the continuous driver that exists today).
- No hardcoded "planted story". Instead: `GenerationPolicy` with configurable constraints —
  guarantee ≥1 near-expiry SKU, ≥N low-stock items, ≥M delayed supplier orders, ≥K price
  anomalies — satisfied by SELECTING from generated data (a guarantee pass after generation),
  never by hardcoding which SKU plays which role.
- Every feature must consume the same data model — no demo-only paths left behind.

**Verified current state:**
- Seed CSVs: `data/datasets/{products,stock,sales,suppliers}.csv` — 4 products, hero SKU
  "4011". `load_seeded_scenario()` (`src/shelfwise_data/seed.py:149`) raises for unknown
  SKUs; `validate_seed_data()` (`seed.py:193`) asserts the planted story.
- `build_store_intelligence_demo()` (`src/shelfwise_data/store_intelligence.py:398`): zero
  params, all literals (lots, dates, delivery numbers, sourcing network store_02_sandton /
  dc_gauteng_central / supplier:dairyco). Feeds chat context (`app.py` chat state) and the
  sidebar tiles.
- Generator to reuse: `shelfwise_worldgen` — `generate_catalog(seed, scale)`
  (`src/shelfwise_worldgen/catalog/generate.py:20`), deterministic, >5k SKUs at hypermarket
  scale; `sample_assortment(seed, size, scale)` (`catalog/sample.py:34`); zero imports of
  `shelfwise_data`. Currently ephemeral only.
- Existing Postgres: 18 RLS-scoped tables in `src/shelfwise_storage/schema.sql`, including
  `shelfwise_products` / `shelfwise_product_variants` / `shelfwise_product_identifiers`
  (via `src/shelfwise_catalog/store.py`) — READ THESE FIRST; reuse for product identity,
  don't duplicate. Missing entirely: stock positions*, sales history, suppliers, branches/DCs.
  (*`shelfwise_inventory_positions` landed in PR #5 — check whether it can serve as the stock
  position table before creating a new one.)
- Call sites to rewire (complete inventory):
  - `src/shelfwise_backend/tools/mcp_surface.py` — 8 tools call `load_seeded_scenario(sku=sku)`
    (lines ~83, 136, 166, 184, 241, 267, 312, 367).
  - `src/shelfwise_backend/cascade.py` — direct calls at 65, 323, 554, 956, 1533; plus
    `build_store_intelligence_demo()` at 311, 1325, 1653.
  - `src/shelfwise_backend/agentic_cascade.py` — 185, 396, 598.
  - `src/shelfwise_backend/app.py` — readiness ~532, `/data/seed/summary` ~693, chat context.
  - `src/shelfwise_backend/product_catalog.py` — CSV + synthetic blend in
    `search_product_catalog`; `_fefo_by_sku` uses the demo literals.
  - NOTE: `cascade.py` and `mcp_surface.py` are two REDUNDANT paths over the same
    decision-science functions. Point both at ONE new facts provider; do not fix twice.
- Test blast radius: only `tests/test_seed_data.py` is tightly coupled to the CSV loader;
  a handful of cascade tests assert planted-story values; ~29 other files use "4011" merely
  as an arbitrary string and survive unchanged.
- WARNING: none of the Postgres classes have ever run against a live Postgres (no CI service,
  no connection-opening test). Budget for first-run SQL/RLS bugs.

**Phases (each is a separate commit):**

4a. **Schema.** Read `shelfwise_catalog/store.py` + `schema.sql` + the new
    `shelfwise_inventory_positions`. Add only what's missing — likely:
    `shelfwise_stock_positions` (or reuse inventory_positions), `shelfwise_sales_history`,
    `shelfwise_suppliers`, `shelfwise_supply_sites` (branches/DCs with distance_km,
    lead_time_hours, per-SKU availability). Every table: tenant_id + RLS policy following the
    existing pattern EXACTLY; add to `TENANT_SCOPED_TABLES` in `src/shelfwise_storage/rls.py`
    (test `tests/test_database_schema.py` enforces the 1:1 match).
4b. **World population service.** New module `src/shelfwise_worldgen/populate.py`:
    `GenerationPolicy` dataclass (seed, catalog_scale, assortment_size, constraints:
    min_near_expiry, min_low_stock, min_delayed_orders, min_price_anomalies) with presets
    (`demo`, `production`); `populate_world(policy, stores) -> PopulationReceipt` that
    generates via `sample_assortment`, derives stock/sales/suppliers/sites deterministically
    from the same seed, runs the guarantee pass (pick + adjust generated rows to satisfy
    constraints, recording WHICH skus were selected in the receipt), and writes through store
    interfaces (both in-memory and Postgres implementations) — never raw SQL in the service.
    CLI: `python -m shelfwise_worldgen.populate --policy demo`. Unit tests with in-memory
    stores: determinism (same seed → same world), constraints satisfied, receipt accuracy.
4c. **Facts provider.** New `src/shelfwise_backend/world_facts.py` (or extend
    `shelfwise_data` with a store-backed provider): one interface exposing
    `scenario_facts(sku)`, `store_intelligence(tenant_id)`, `sourcing_candidates(sku)`,
    `catalog_search(query)` reading from the stores. In-memory + Postgres both work via the
    same `SHELFWISE_STORE_BACKEND` seam. On `memory` backend with no population run, either
    auto-populate at startup from the `demo` policy (recommended — keeps every existing flow
    working with zero config) or fall back to current CSVs temporarily.
4d. **Rewire call sites** to the facts provider, in this order (test suite between each):
    mcp_surface tools → product_catalog → cascade.py → agentic_cascade.py → app.py
    (readiness, seed summary route becomes a world summary, chat context). Retire
    `build_store_intelligence_demo()` literals; keep pure calc functions
    (`split_stock_by_fefo`, `plan_supplier_cover`, `plan_stock_sourcing`, etc.) — they're
    sku-agnostic and stay as the math layer. Update `tests/test_seed_data.py` and
    planted-story assertions to assert against the population receipt instead of literals.
4e. **Live Postgres verification.** `docker compose up postgres` locally, run the populate
    CLI against it, boot backend with `SHELFWISE_STORE_BACKEND=postgres`, exercise: chat
    full-report question, one agentic cascade, `/products/search`, `/inventory/positions`.
    Fix what breaks (expect at least one SQL/RLS surprise). Add ONE real integration test
    gated by an env var (`SHELFWISE_TEST_DATABASE_URL`) + a CI postgres service container so
    this never regresses silently.
4f. **Docs.** README (data section: generated deterministic world, policy-constrained, no
    hardcoded fixtures), DEMO_RUNBOOK (populate step in the runbook), evidence report note.

**Acceptance for TASK 4 overall:** grep shows no remaining calls to
`load_seeded_scenario`/`build_store_intelligence_demo` outside `shelfwise_data` internals and
tests explicitly covering the legacy loaders; chat/cascades/tools answer for MANY skus, not
just 4011; the same flows work on both memory and postgres backends; full suite + ruff green;
capability manifest regenerated; live-verified evidence in commits.

---

## TASK 5 (ops, not code — requires a human with GPU access): deploy the 31B strong tier

Everything in code is ready (`LLM_STRONG_BASE_URL`/`LLM_STRONG_API_KEY`, per-role routing,
`dual_model_configured` flips automatically; prod compose already defaults :8000/:8001).
Remaining work is operational: start a second vLLM process serving `google/gemma-4-31B-it` on
port 8001 of the MI300X droplet (VRAM permitting; MI300X has 192GB — check with
`docker exec rocm rocm-smi` first), set the env vars, verify `/inference/readiness` shows
`dual_model_configured: true`, run one smoke per tier, and record model-run IDs. If VRAM or
time doesn't allow, leave as documented roadmap — do NOT fake it.

---

## TASK 6 (P2 backlog — only after 1–5; each is its own PR)

In value order:
1. Canonical lot/batch lineage + donation/transfer/quarantine/write-off outcome actions
   (extends the recall/exception workflows landed in PRs #3/#4; new lot table links to
   `shelfwise_inventory_positions`).
2. Open-PO awareness + candidate deduplication + suppression windows + SLA aging for
   procurement candidates.
3. Source reconciliation events (POS vs WMS vs ERP vs physical counts) — builds on the
   connector provenance layer.
4. Partial-delivery history + supplier SLA history + substitution eligibility (extends
   `shelfwise_suppliers` from TASK 4a).
5. Promotion calendar, margin floors, promo-adjusted demand baselines.
6. Refund/void feeds, omnichannel reservations, regulatory/staple tags, uncertain
   product-identity review queue.

Each: schema (RLS, TENANT_SCOPED_TABLES), store interface (memory + postgres), route(s)
(README/frontend/manifest contracts), tests, UI surface where user-visible.

## Global definition of done (every task)

```
$env:PYTHONPATH="src"
python -m ruff check src tests scripts        # clean
python -m pytest -q                           # all pass
python scripts/compare_capability_manifests.py --write   # if routes/tools changed
cd frontend && npx tsc --noEmit               # if frontend touched
```
Plus live verification evidence for user-visible changes, and HANDOFF.md updated with a short
coordination note describing what landed.
