# HANDOFF — current continuation state as of 2026-07-12

> **Read this section first.** The historical notes below remain as evidence, but many of
> their branch names, counts, and deadlines are stale. The authoritative working branch is
> `developers`; only `main` and `developers` exist locally and on `origin`.

## Frontend/system bug audit pass (2026-07-12, this session)

Goal: act as a debugger, find and fix real bugs across frontend + backend, no redesign,
no hardcoded/cached answers (evaluation uses unseen variants).

Confirmed and fixed:

1. **Duplicate approval-queue notifications (the reported symptom).** `src/shelfwise_backend/app.py`
   `_demo_event` / `demo_recall` / `demo_inventory_exception` minted a fresh random `uuid4()` suffix
   on every call, so every click of a demo trigger (or every reload that replays it) created a brand
   new pending decision for the identical underlying scenario - the approval queue filled up with
   near-identical "Apply 20% markdown ... Selati Flour Low Fat" cards (verified live: 3 repeated
   `POST /demo/golden` calls produced 4 separate pending decisions before the fix). Fixed by deriving
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

Verified: 454 tests passed, 3 skipped (Postgres integration tests, no `SHELFWISE_TEST_DATABASE_URL`
set locally); `ruff check` clean; `tsc --noEmit` clean; manually drove the running dev app in-browser
(products search, approval queue open/approve/reject, chat send, sidebar workspaces, settings panel)
with no console errors.

Not yet done / lower priority: full line-by-line review of the rest of `App.tsx` (3300+ lines) beyond
the workspaces actually exercised above; a wider audit could still turn up more latent issues if asked
to continue.

## CURRENT UPDATE — disposable-droplet recovery and frontend pass — 2026-07-12

Read this section before continuing. The worktree contained active application/frontend changes
when this recovery pass began. They were preserved, tested, and are intended to be saved on
`developers`; do not reset or discard them.

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
  --archive /workspace/persist/capsules/shelfwise-session-$(date -u +%Y%m%dT%H%M%SZ).tar.zst
```

The command must exit successfully and print an empty `failures` list. Verify the capsule before
downloading it:

```bash
python scripts/session_capsule.py verify /workspace/persist/capsules/shelfwise-session-<timestamp>
sha256sum /workspace/persist/capsules/shelfwise-session-<timestamp>.tar.zst
```

Only after API/training shutdown, database dumps, Redis persistence, capsule creation, checksum
verification, download, and local checksum verification have succeeded may the Droplet be
destroyed. Restore into a new MI300X with:

```bash
python scripts/session_capsule.py restore shelfwise-session-<timestamp>.tar.zst \
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
- Tracked changes still need to be reviewed, staged, committed, and pushed together with this
  update. Do not stage existing untracked run artifacts unless intentionally packaging evidence.
- Before the next cloud run, create the capsule and keep the archive off the Droplet.
- Remaining external blockers are public `linux/amd64` image publication, actual AMD cloud
  startup/latency receipt, and final merge to `main` after those proofs. Do not claim these are
  complete from local tests.

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

The repository-side implementation is complete and locally verified. The only remaining
completion evidence is a live AMD cloud run proving items 2-4 against the actual deployment.
Do not mark the objective complete until that receipt exists.

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

- `LLM_TIMEOUT_SECONDS` is clamped to 29 seconds.
- `SHELFWISE_REQUEST_TIMEOUT_SECONDS` is clamped to 29 seconds.
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
- requires each chat response under 30 seconds;
- requires `X-ShelfWise-Provider: vllm_mi300x`, `X-ShelfWise-Answer-Source: model`, and
  `X-ShelfWise-Replayed: false`;
- requires English-compatible output and unique correlation IDs;
- writes a JSON receipt when `--output` is provided.

Run it only after the AMD endpoint and production application are live:

```powershell
python scripts/track3_prescreen.py `
  --base-url http://<public-app-origin> `
  --startup-deadline 60 `
  --request-deadline 29 `
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

## Current Verification Baseline

- Full Python suite: `453 passed, 3 skipped`.
- Ruff: clean.
- Frontend TypeScript typecheck: passed in the prior verification pass.
- Capability manifest: regenerated and committed after route/test changes.
- Focused Track 3 prescreen test is present in `tests/test_track3_prescreen.py`.
- Live cloud timing and AMD response proof: **not yet run in this continuation**.

## Remaining Risks / Do Not Claim As Done

- The AMD cloud endpoint may be powered off or unreachable; verify it before spending credits.
- Public `linux/amd64` image publication is not complete until a public registry namespace is
  chosen, both images are pushed, and clean unauthenticated pulls succeed.
- The local Docker image build passed, but local CPU build/start is not AMD evidence.
- Actual container readiness under 60 seconds and actual model responses under 30 seconds need
  the cloud receipt.
- AMD-SMI host GPU/VRAM telemetry is not available from the provider; never invent utilization.
- The architecture benchmark is implemented and tested offline, but 1/8/32 concurrency against
  the live AMD endpoint is not yet measured in this current proof cycle.
- Routine/strong routing is implemented; actual deployment of two distinct serving endpoints
  still has to be confirmed by `/inference/readiness` and the prescreen receipt.
- Two smaller deterministic guardrail checks remain deterministic-only; do not describe every
  internal check as a model agent unless the route receipt proves it.
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
      deterministic cascades (`/demo/golden`, `/demo/procurement`, `/demo/sales`,
      `/demo/cold-chain`) — all produced genuine, non-hardcoded results. Confirmed via
      direct `psql` query that the decisions (6 rows) and the world snapshot (200 products)
      are real persisted rows in Postgres, not in-process state.
- [x] 10. Added `tests/test_postgres_world_integration.py` — 3 tests gated on
      `SHELFWISE_TEST_DATABASE_URL` (skip cleanly without it, verified both ways): a real
      `populate_world` round-trip through Postgres, `WorldFactsProvider` reading from a real
      connection, and tenant isolation between two snapshot rows. The fixture auto-forces
      `SHELFWISE_AUTO_SCHEMA=false` so it only needs the one env var to work against the
      restricted app role. **Not yet added to CI** (no Postgres service container in
      `ci.yml` for this specific test) — flagged as a follow-up, not done in this pass.
- [x] 11. Full suite green: 444 passed, 3 skipped (the new Postgres integration tests
      without the env var) — zero failures. Ruff clean. Capability manifest regenerated
      (175 capabilities). **README/DEMO_RUNBOOK not yet updated** for the new generated-world
      data model — still describes the old CSV-seed framing in places; genuine follow-up,
      not done in this pass given time spent on the harder correctness work above.
- [x] 12. Commits landed incrementally per phase (schema+store+populate, facts provider,
      call-site rewiring, evidence-label fix, test fixes, integration test) — see git log
      on the `developers` branch. This entry is that final summary update.

**Bottom line: the app now genuinely pulls from Postgres.** No more hardcoded CSV seed data
or literal demo fixtures anywhere in the live request path — `load_seeded_scenario`/
`build_store_intelligence_demo` are no longer called from any production code path (only
`shelfwise_data`'s own internals/tests still reference them, which is fine — they're the
low-level building blocks the old CSV loader was built from, now superseded).
**Two follow-ups explicitly NOT done, flagged honestly:** (a) CI has no Postgres service
container yet, so the new integration test only runs locally/manually; (b) README/
DEMO_RUNBOOK still need a pass to describe the generated-world model instead of the old
seeded-CSV framing.

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

## Prior update — 15-min live soak test PASSED + a real chat bug found and fixed by screen-testing

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

- `POST /demo/procurement/agentic` — Critic calls `get_reorder_policy` +
  `get_supplier_ranking`, cites real reorder quantity (23.70 units) and measured supplier
  choice; Executive routes reorder/monitor.
- `POST /demo/sales/agentic` — Critic calls `check_price_integrity` against a deliberately
  mismatched till price (20% over catalogue, outside the deterministic cascade's own 15%
  tolerance); genuinely caught the exception (36.0 vs 30.00, delta 6.00) and routed to
  manager review.
- `POST /demo/cold-chain/agentic` — Critic calls `get_cold_chain_status` for a measured
  refrigeration alert; routes dispatch/monitor based on the real measured risk figure.

Each is additive - the original deterministic routes (`/demo/procurement`, `/demo/sales`,
`/demo/cold-chain`) are unchanged and still work. Each new route defaults `live_required`
so a broken endpoint 503s instead of silently faking success. 408/408 tests pass (12 new
tests: 3 cascades x offline-success/live_required-hardfail/ungrounded-rejection).

**Remaining deterministic-only**: the two smaller conditional checks
(`run_catalog_price_check`, `run_expiry_risk_check` in `cascade.py` - narrower guardrails,
not primary demo scenarios) were not converted. Diminishing returns given remaining time;
flag if there's time left after recording.

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
- Batch/lot-level expiry tracking and fleet-wide (500k SKU) scoring are not implemented —
  legitimate gaps, multi-day scope, not fixable before today's deadline. Mention honestly in
  the deck as roadmap, don't claim as done.
- Dual-model routing is code-complete (`base_url_for_agent`/`api_key_for_agent`,
  `dual_model_configured` flag) but only one model endpoint is actually deployed
  (`dual_model_configured: false` confirmed live) — see "two-model deployment" below.

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

## What is VERIFIED live (with receipts in commits/artifacts)

- `/inference/smoke`, `/chat`, `POST /demo/golden/agentic` all hit real MI300X Gemma.
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
   a. Deploy a second model on a second endpoint and set `LLM_STRONG_BASE_URL`/
      `LLM_STRONG_API_KEY` (routing code is ready, `dual_model_configured` flips true
      automatically once real credentials point at a second serving endpoint) - the one
      remaining gap between "routing is built" and "two models are actually running."
   b. Wire the two smaller conditional checks (`run_catalog_price_check`,
      `run_expiry_risk_check`) through the agentic pattern too - same recipe as
      golden/procurement/sales/cold-chain in `src/shelfwise_backend/agentic_cascade.py`.
   c. Run `shelfwise_benchmark` at 1/8/32 concurrency against the live endpoint for the
      architecture-comparison report.

## Known honest gaps (do not overclaim in the deck/video)

- UPDATE: golden, procurement, sales, and cold-chain cascades are now ALL genuinely
  agentic (`/demo/{golden,procurement,sales,cold-chain}/agentic`, `live_required` default).
  Only the two smaller conditional guardrail checks (`run_catalog_price_check`,
  `run_expiry_risk_check` in `cascade.py`) remain deterministic-only. The original
  deterministic routes are all still present and unchanged alongside the new agentic ones.
- Training matrix: E2B/12B W7900 shakedown blocked (Jupyter portal down). Only E4B is live.
- Benchmark architecture comparison (shared/replicated/per-agent/hybrid) is built + tested
  offline but has no real cloud measurements yet.
- Only one model is actually deployed/served (google/gemma-4-E4B-it). The routine/strong
  per-agent endpoint routing is real and tested (`dual_model_configured` flag), but it's
  currently pointed at the same single endpoint for both tiers - genuinely deploying two
  is unstarted infrastructure work, not just config.
- Batch/lot-level expiry tracking and fleet-wide (500k+ SKU) scoring described in the
  original blueprint are not implemented - real, multi-day scope, out of reach before the
  deadline. State this as roadmap in the deck, not as done.
- Postgres RLS policies exist in `schema.sql` but are irrelevant to the current demo
  deployment (`SHELFWISE_STORE_BACKEND=memory` - no Postgres in the loop at all); only
  matters if/when the Postgres profile is actually used in a future deployment.
- MI300X operator-side AMD-SMI telemetry: not collected (provider gives no host access);
  report as missing evidence, never estimated. vLLM /metrics IS available on the droplet.

## House rules (unchanged, binding)

No AI attribution anywhere (commits/PRs). Free-tier/open-source only. Cloud inference only
(MI300X/vLLM + Fireworks fallback) — never local models. MIT-clean deps. No temporary fixes.
Read `CLAUDE.md` for the full mandate (full MVP, not a demo slice).
