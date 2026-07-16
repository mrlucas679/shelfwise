# Test suite critique (2026-07-15)

Per-file critique of all 113 files in `tests/`, done against the methodology in
[.claude/skills/veteran-test-audit/SKILL.md](.claude/skills/veteran-test-audit/SKILL.md): not
"does the assertion look right" but "what does this test's *arrangement* actually prove, and
does it match production config (`SHELFWISE_STORE_BACKEND=postgres`, `WORKER_ENABLED=true`,
`SHELFWISE_AUTH_MODE=jwt`)?"

This audit already produced two confirmed, fixed bugs before it was written up (see git history
same day): `InMemoryCandidateStore` silently delegating its history sub-store to a
Postgres-backed implementation via an env-sensitive factory, and `PostgresDecisionStore.upsert()`
never binding its RLS session to the record's own tenant. Both were invisible to the passing
suite because every test that touched them ran under the in-memory default. The rest of this
document is the same interrogation applied to the other 111 files.

## Dominant cross-cutting pattern

The overwhelming majority of `TestClient(app)`-based tests run under whatever the process
default resolves to: in-memory stores, no JWT enforced, worker disabled (synchronous cascade
path). Only a handful of files explicitly force production-like env vars:
`test_inventory_exception_workflow.py`, `test_recall_workflow.py`, `test_tenant_auth.py`,
`test_chat_conversations.py`, `test_chat_state_bounding.py` (partially), `test_tenant_profiles.py`
(partially), `test_track3_contract.py` (partially). Every other backend/API test file is, by
construction, only proven correct under the *non*-production topology. This is the same pattern
that produced both confirmed bugs, and it recurs in nearly every file below.

## Per-file critique

### Core / shared

- **`_world_test_support.py`**: Helper, not a test — resolves a SKU from
  `InMemoryWorldSnapshotStore`, so every consumer inherits an in-memory world regardless of
  `SHELFWISE_STORE_BACKEND`.
- **`conftest.py`**: The autouse fixture clears singletons imported from `shelfwise_backend.app`
  at collection time; whatever backend those resolved to at import time (in-memory vs Postgres)
  is what the *entire suite* runs against. Never pins or asserts the backend — a suite-wide blind
  spot baked into every test that doesn't override it individually.
- **`test_capability_contract.py`**: Very strong — validates the committed capability manifest
  byte-for-byte against fresh static discovery, with real waiver-expiry/overlong-window negative
  tests. Close to characterization testing done right.
- **`test_contracts_money.py`**: Pure Decimal arithmetic, deterministic, real boundary
  (half-up rounding at cent) and negative-path (currency mismatch) cases. Solid.
- **`test_context_assembler.py`**: Pure-function tests with real boundary/invalid-input cases
  (blank `decision_type`, oversized `max_chars`). No external dependency needed; arrangement is
  appropriate.
- **`test_database_schema.py`**: Useful static schema-text assertions (catches drift between
  `TENANT_SCOPED_TABLES` and `schema.sql`), but `apply_tenant_rls` is only checked via a
  `FakeConn` capturing SQL strings — the policies are never executed against a real Postgres
  session with two competing tenants, so a syntactically-valid-but-semantically-wrong policy
  would pass.
- **`test_decision_identity.py`**, **`test_decision_science_extended.py`**,
  **`test_store_intelligence.py`**, **`test_stock_sourcing.py`**, **`test_fleet_scoring.py`**,
  **`test_workload.py`**, **`test_contracts_money.py`**, **`test_twin_calibration.py`**,
  **`test_product_policies.py`**, **`test_context_assembler.py`**: pure decision-science / pure
  function tests with real Decimal arithmetic and genuine boundary/negative cases, nothing to
  fake. Solid as a class — no arrangement concerns.

### Store / backend-selection layer

- **`test_candidate_history.py`**, **`test_candidate_store.py`**: Every test constructs
  `InMemoryCandidateStore()`/`InMemoryCandidateHistoryStore()` **directly**, bypassing
  `create_candidate_store()` entirely. This is exactly the class the confirmed
  leaking-history-store bug lived in — this file structurally *cannot* catch that regression
  even after the fix, since it never goes through the factory.
- **`test_candidate_history_api.py`**: Better — uses the process-level `candidate_store`
  singleton (factory-resolved) via a real route, but never asserts which backend was actually
  active, so a silent fallback to in-memory would pass unnoticed.
- **`test_store_backends.py`**: Exercises real factory functions, asserts in-memory defaults and
  `ValueError` on missing `DATABASE_URL`/`REDIS_URL` — proves the guard rails exist, never
  constructs a real Postgres/Redis-backed store.
- **`test_data_domain_state_isolation.py`**: Proves data-domain partitioning purely against
  `InMemoryCandidateStore`/`InMemoryOpenOrderStore`/`InMemoryLearningStore` — never checked
  against the Postgres equivalents that back production.
- **`test_open_orders.py`**, **`test_product_catalog_store.py`**, **`test_event_store.py`**,
  **`test_learning_tenant_scope.py`**, **`test_world_facts_batches.py`**,
  **`test_twin_scenarios.py`**, **`test_twin_api.py`** (restart-recovery tests): each only
  exercises the in-memory variant of its store for guarantees that matter for correctness
  (idempotency, late-event ordering, conflict rejection, tenant scoping). None has an equivalent
  test against its real Postgres-backed sibling class — precisely the gap class that produced
  both confirmed bugs this session.
- **`test_live_tool_boundary.py`**: Hand-written `_Memory`/`_Decisions` fakes prove the tool
  wrapper forwards scoping correctly, not that the real Postgres stores enforce the same scoping
  at the SQL/RLS layer.
- **`test_mlops.py`**: Solid outcome-based assertions, but every store used is the in-memory
  variant — a Postgres-specific filtering bug in `PostgresModelRunRegistry`/
  `PostgresTenantFactStore` would be invisible here.
- **`test_agent_role_coverage.py`**: One thin test against `InMemoryModelRunRegistry` — proves
  the recorder writes something, not that a real Postgres-backed registry accepts the same
  payload.

### Postgres-specific (the honest, correctly-gated files)

- **`test_postgres_schema_contract.py`**, **`test_postgres_world_integration.py`**: Correctly
  `skipif`-gated on `SHELFWISE_TEST_DATABASE_URL`, and content-wise genuinely good — real
  ON CONFLICT write paths, concurrent advisory-lock behavior, live tenant isolation. The risk is
  entirely environmental: if that env var is never set in CI, these thorough files silently
  contribute zero coverage. **Confirmed during this session**: they were skipped in the normal
  local run; running them for real (see below) is what surfaced both bugs.

### Auth / tenancy

- **`test_tenant_auth.py`**: Genuinely strong — real HS256 JWT signing, explicit IDOR checks
  (404 not 403 for cross-tenant reads), scoped assertions across two real tenant contexts in the
  same test. The model this skill describes. Only gap: never runs under
  `SHELFWISE_STORE_BACKEND=postgres`, so isolation is proven at the JWT layer, not against real
  RLS (which is exactly where the confirmed bug was).
- **`test_tenant_profiles.py`**: Same strength for the write-gate test, but CRUD tests run
  in-memory only — never checks Postgres persistence or a concurrent-upsert race between tenants.
- **`test_chat_conversations.py`**: One of the few files that explicitly flips
  `SHELFWISE_AUTH_MODE=jwt` to match production, plus real `ThreadPoolExecutor` concurrency for
  idempotency. Good.
- **`test_gateway_security.py`**: Solid, deliberate negative-path coverage (body-size limits,
  rate-limit identity spoofing, prompt fencing) using the real `TokenBucket`/spotlight
  implementations, not mocks.
- **`test_backend_observability_tools.py`**: Only tests the legacy `x-api-key` gate, never
  `SHELFWISE_AUTH_MODE=jwt` — the production auth mode is unverified for these routes.

### Agentic cascades

- **`test_agentic_golden_cascade.py`**, **`test_agentic_procurement_cascade.py`**,
  **`test_agentic_sales_cascade.py`**, **`test_agentic_cold_chain_cascade.py`**,
  **`test_agentic_guardrail_cascades.py`**: Consistently strong as a group — real tool-calling
  loops against scripted-but-real `_FakeRuntime`, decision/learning stores via the actual
  factories, deliberate negative tests (ungrounded conclusions rejected, no-real-numbers
  rejected). `test_agentic_golden_cascade.py`'s economics-wiring test caught a real key-mismatch
  bug by checking store state, not return values — a model test. Shared caveat: all offline
  (`ExecutionMode.OFFLINE_TEST`) except one `LIVE_REQUIRED`-rejection test each; no test proves
  any cascade against a real vLLM/Fireworks endpoint.
- **`test_agentic_http_errors.py`**: Both tests monkeypatch the cascade function itself to raise
  directly, rather than driving a real cascade to a real error — proves only the HTTP
  error-mapping layer.
- **`test_agentic_operational_twin.py`**: The positive test (`..._resolves_operational_twin_facts`)
  replaces the cascade function with a spy that's never actually called through — only the 422
  negative test exercises the real path.
- **`test_model_tool_calling.py`**: Strong, deliberate adversarial coverage (model inventing
  `tenant_id`, malformed JSON, ungrounded conclusions) — the outcome-over-mocking style this
  audit asks for.
- **`test_agent_orchestration.py`**: Excellent regression coverage for actual live-observed
  failure modes (json_schema+forced-tool-call collision, deadline math) against a fully scripted
  fake runtime — appropriately scoped to not need a real HTTP client.

### Golden / recall / inventory-exception / procurement HTTP routes

- **`test_inventory_exception_workflow.py`**, **`test_recall_workflow.py`**: Excellent — the
  only two files in the whole suite that explicitly drive the real `cascade_worker` under
  `WORKER_ENABLED=true` on its own thread, and specifically regression-test bugs found in
  production. The model this codebase's own stated methodology should look like everywhere.
- **`test_golden_cascade.py`**, **`test_audit_fixes.py`**: `TestClient(app)` with no env
  overrides — every HITL/economics/decision-list assertion here runs under default in-memory /
  synchronous mode, never proven against `WORKER_ENABLED=true` or Postgres. **Acted on below.**
- **`test_product_catalog_api.py`**, **`test_product_identity_catalog_api.py`**: Solid
  outcome-based HTTP tests (computed fields, real 404/409/422 paths), same in-memory/no-worker
  caveat.

### Connectors / gateway

- **`test_connectors.py`**: Strongest file in this group — a real `ThreadPoolExecutor`
  concurrency race test for writeback idempotency, plus a static cross-check pinning
  `PostgresInboundRecordStore`'s `ON CONFLICT` columns against `schema.sql`'s actual unique
  constraint, explicitly modeled on a real prior incident.
- **`test_connector_transports.py`**: Real HMAC signature verify (accept and reject), but
  dedupe/cursor state is entirely in-memory.
- **`test_connector_poll_service.py`**: Focused, real negative/partial-config cases, error
  propagation via `pytest.raises` not a call-count mock.
- **`test_connector_poll_status_api.py`**: Only asserts the *disabled* default state — no test
  exercises the endpoint once polling is actually running.
- **`test_connector_catalog.py`**, **`test_connector_intake.py`**: Default in-memory/no-auth
  `TestClient(app)`; dedup/cascade pipeline behavior never checked against Postgres or worker
  mode.
- **`test_system_connectors.py`**: Pure mapper unit tests, no I/O, deliberate malformed/negative
  cases — one of the stronger files.
- **`test_system_connector_classes.py`**: All connectors driven by hand-written async fakes that
  never exercise real HTTP/TLS/pagination — good for mapping logic, proves nothing about real
  SAP/Odoo/Syspro endpoint behavior.
- **`test_edge_gateway.py`**: Real HMAC negative test, but persistence into the twin store is
  only checked via an `accepted` count, not the actual stored content.

### Event bus / worker

- **`test_event_bus_bounds.py`**: Heavy reliance on a hand-rolled `FakeRedis` with pure
  call-count/argument assertions (`xadd_calls`, `group_calls`, `xautoclaim_calls`) — the GOOS
  anti-pattern of proving the code called the fake correctly, not that real Redis stream
  trimming/consumer-group semantics work. `RedisStreamsEventBus`'s actual runtime behavior
  against real Redis is unverified anywhere in the suite.
- **`test_worker_journal.py`**: One of the most thorough files — real `InMemoryEventBus`
  retry/dead-letter path, `WORKER_ENABLED=true` under a real async polling loop. Still entirely
  in-memory bus, so Redis consumer-group `XCLAIM`/`XACK` semantics remain unexercised despite
  `reclaim_stale` being explicitly about that.
- **`test_twin_projection_worker.py`**: `OneMessageBus`/`EmptyBus` fakes never reproduce real
  Redis-stream semantics (blocking reads, consumer groups, at-least-once redelivery).
- **`test_event_ingest.py`**: Strong on self-healing/retry-after-partial-failure, but the
  `WORKER_ENABLED=true` test only asserts the response is queued — never that a worker actually
  drains and processes it.

### Training / benchmark / shakedown

- **`test_training_evaluation_gate.py`**: Strong — actively asserts the prompt never contains the
  assistant's own reference answer (guards against echo/leakage), dry-run structurally cannot
  pass.
- **`test_training_profiles.py`**, **`test_training_dataset_domain.py`**,
  **`test_provenance_boundaries.py`**: Good, deliberate negative/boundary coverage using real
  file-based manifests, not mocks.
- **`test_benchmark_config.py`**: `test_config_rejects_stale_local_provider_rows` is exactly the
  "must fail closed" test this methodology wants.
- **`test_benchmark_adapters.py`**, **`test_benchmark_reporting.py`**: Solid parsing/negative-case
  coverage, no live GPU dependency needed.
- **`test_benchmark_runner.py`**: Fakes throughout — proves the runner's own concurrency/
  aggregation logic, never a real vLLM endpoint's actual latency/error shapes.
- **`test_gemma4_training_harness.py`**: `test_serving_check_reads_exported_adapter_metadata_without_model_load`
  unconditionally `pytest.skip`s when the local adapter artifact is absent — in CI, if that
  artifact is never materialized, this is a permanently-skipped test masquerading as coverage.
  **Needs a CI-artifact check, flagged below.**
- **`test_shakedown_pipeline.py`**: Dataset/report plumbing only, dry-run — never a real training
  step or live serving endpoint.
- **`test_shakedown_settings.py`**: `test_shakedown_passes_config_values_to_dataset_and_training`
  monkeypatches `build_shakedown_datasets`/`run_training`/`run_evaluation` entirely — pure
  kwargs-forwarding, proves wiring only.
- **`test_deployment_shakedown.py`**: Entirely `httpx.MockTransport`-driven — thorough on the
  shakedown script's own logic, but by construction can never catch a bug in the real deployed
  backend; it validates the auditor, not the audited.
- **`test_submission_evidence.py`**: Asserts against a frozen, already-committed
  `reports/soak_.../summary.json` — a snapshot assertion that would keep passing even if the
  pipeline that generates soak receipts is now broken, since the receipt is never regenerated.
- **`test_track3_prescreen.py`**: `_request` monkeypatched with five hardcoded canned responses
  popped in order — proves only the receipt-assembly logic given a scripted transcript, no real
  HTTP is ever sent.
- **`test_track3_contract.py`**: `test_track3_production_chat_fails_closed_without_live_endpoint`
  is a genuine real fail-closed check; the rest is pure-function tests in isolation.

### Synthetic data / world generation

- **`test_synthdata.py`**: `test_run_suite_scores_pass_rate_and_failures`'s fake `run_one` just
  echoes `scenario.expected` back verbatim — `run_suite`'s actual comparison/scoring logic is
  never exercised with a genuine mismatch, so it will always report `pass_rate == 1.0` regardless
  of whether the scoring logic works. **A tautology. Fixed below.**
- **`test_catalog_worldgen.py`**: Solid boundary/invariant tests (EAN-13 check digit, barcode
  uniqueness across 1000 samples) driven off real generator functions with fixed seeds.
- **`test_worldgen_simulator.py`**: Broad and largely strong — determinism, event-lane coverage,
  no-answer-leakage checks, and a genuine regression test for a real production incident
  (3-decisions-from-841k-events bug) with a documented root cause. Runs with default (no) auth
  and in-memory stores throughout, though.
- **`test_full_system_harness.py`**: Real, substantial soak-test harness driving actual cascades
  through `TestClient`; genuinely strong, but `run_full_system` still defaults to in-memory/
  synchronous topology — no assertion anywhere forces the production config.
- **`test_full_system_artifact_validation.py`**: Validator tested against a fully synthetic fake
  artifact directory, never a real captured run — good negative-path coverage of the validator
  itself, but can't catch a bug in the actual artifact-producing pipeline.
- **`test_fleet_scale_shakedown.py`**: Appropriately isolated pure-streaming test; the
  shortfall-detection negative test deliberately monkeypatches a small source to prove a
  silently-stops-early bug class is caught.

### Multimodal / inference / serving

- **`test_multimodal.py`**: Good breadth, but every voice/scan/VLM path is disabled or
  monkeypatched — no test anywhere exercises a real STT/TTS/VLM endpoint, so a real-provider
  contract mismatch would never surface.
- **`test_forecast_tsfm.py`**: `FakeTsfm` covers agree/diverge branches, but there is no test for
  a TSFM call that times out, errors, or returns malformed JSON — only happy-path branches.
- **`test_inference_client.py`**, **`test_model_tool_calling.py`**: Hand-rolled `urlopen`
  monkeypatching rather than a real HTTP server, but deliberately test malformed JSON, wrong-shape
  200 responses, and HTTP-200-with-error-sentinel bodies — the failure-handling checklist item
  done right, within the scope of what a client unit test should prove.
- **`test_inference_config.py`**: Pure string/URL-construction, no external dependency to fake.
- **`test_inference_readiness.py`**: Only ever asserts env-var-driven config plumbing — never
  calls a live endpoint, so "ready" here only proves config shape.
- **`test_model_runtime_routing.py`**: `_FakeChatClient` proves the runtime computes/forwards the
  correct bounded timeout, never that the real HTTP client actually respects it against a
  slow/hanging server.
- **`test_serving_gate.py`**: Well-designed — explicitly distinguishes metadata-only from
  generated-inference-observed, with a dedicated test proving the gate isn't fooled by an
  echo-back transport.
- **`test_live_tool_boundary.py`**: (see Store section above)

### Infra / repo-contract text checks

- **`test_infra_config.py`**, **`test_workflow_contract.py`**, **`test_droplet_bootstrap_contract.py`**,
  **`test_frontend_attention_contracts.py`**, **`test_frontend_product_contracts.py`**: All pure
  text-grep/substring checks against docker-compose/Dockerfile/README/CI-workflow/App.tsx source.
  Real regression guards for specific past incidents (worth keeping), but none of them prove the
  described behavior actually runs — a config that satisfies the string pattern but is
  semantically broken, or a UI that no longer calls the matched literal correctly, would still
  pass.
- **`test_frontend_route_coverage.py`**: Better-designed than its siblings — diffs the frontend's
  route registry against the real backend `app.openapi()` schema, so a route added to one side
  and forgotten on the other is a genuine, non-tautological failure.

### Twin / calibration

- **`test_twin_calibration.py`**, **`test_twin_models.py`**, **`test_twin_projector.py`**: Small,
  precise pure-function/boundary tests (vacuous-completion rejected, raw-media rejected,
  injected fixed clock) — no I/O to fake, solid.
- **`test_twin_api.py`**: Restart-recovery tests genuinely wipe and replay from the event log —
  real coverage of a real failure mode, always against the in-memory store.

### Misc / operational tooling

- **`test_session_capsule.py`**: Uses a real git repo fixture, tests symlink resolution and secret
  redaction for real — strong, though non-git subprocess calls are stubbed.
- **`test_seed_data.py`**: A single hardcoded-value assertion against the planted demo scenario —
  a snapshot/tautology risk that only catches accidental seed-data drift, not loading-logic bugs.
- **`test_resilience.py`**, **`test_candidate_factory.py`**: Pure deterministic-function tests, no
  external dependency, solid as far as they go.
- **`test_eval_harness.py`**: Reasonable single-call structural check against `report.checks`.
- **`test_csv_connector.py`**: Fine, one test uses manual try/except instead of `pytest.raises` —
  a weaker but valid idiom.
- **`test_detective.py`**: `root_cause_cte_sql()` is only checked for substring presence, never
  executed against real Postgres to confirm the recursive CTE returns correct lineage.

## Findings acted on this pass

1. **`tests/test_synthdata.py::test_run_suite_scores_pass_rate_and_failures`** — tautological
   fake (`run_one` echoes `scenario.expected` back verbatim, so `run_suite` can never observe a
   mismatch). Fixed to include a genuinely-failing scenario and assert it's actually detected.
2. **Golden / procurement / sales / cold-chain routes never proven under `WORKER_ENABLED=true`**
   — checked against the actual route code (`src/shelfwise_backend/app.py`) rather than assumed:
   these four routes call `run_golden_cascade()`/`run_procurement_cascade()`/`run_sales_cascade()`/
   `run_cold_chain_cascade()` directly and synchronously inside the request handler, then persist
   via `_record_cascade()`. They never go through `_record_pipeline_event()` (the event-bus/
   worker-deferrable path that `/scenarios/recall` and `/scenarios/inventory-exception` use) at
   all. So `WORKER_ENABLED` has no effect on them - this is a different, valid architecture for
   these four routes, not an untested configuration. **Not a bug; the flagged gap doesn't apply.**

## Findings flagged — resolution status (all closed 2026-07-15/16 except where noted)

- `test_gemma4_training_harness.py` adapter-metadata test silently no-oped without the local
  283MB artifact — **closed**: the serving-check logic now runs everywhere against a committed
  metadata-only fixture (`tests/fixtures/adapter_metadata/`); the artifact-gated test remains
  for machines that have the real export.
- `test_forecast_tsfm.py` had zero failure-handling coverage — **closed, and it found a real
  bug**: `forecast_demand_tsfm` had NO failure path at all; a TSFM timeout/connection
  error/malformed payload crashed the caller instead of degrading to the transparent baseline
  its own shadow-mode design promises. Fixed (baseline keeps control, failure on the evidence
  record, input-validation errors still raise) with four parametrized transport-failure tests.
- Redis semantics unverified — **closed**: `tests/test_redis_bus_contract.py` runs against real
  Redis, in CI too.
- Postgres store siblings with in-memory-only guarantees — **closed for the write-path
  invariants that matter**: late-event ordering + duplicate idempotency for
  `PostgresOpenOrderStore` and conflicting-identifier rejection + idempotent re-assert for
  `PostgresProductCatalogStore` now run against real Postgres in the gated contract file
  (learning-store race and event-store dedup were already covered there).
- `test_candidate_history.py`/`test_candidate_store.py` bypassing the factory — **closed**: two
  factory-wiring tests in `test_store_backends.py` pin memory→memory history pairing (with
  poisoned Postgres coordinates that would fail loudly on any leak) and the direct-construction
  purity contract.
- `test_connector_poll_status_api.py` only asserting the disabled state — **closed**: an
  enabled/real-run status test now covers the running payload.
- `test_agentic_http_errors.py` fabricating exceptions at the route boundary — **closed**: a
  scripted stubbornly-direct runtime now provokes the real ToolCallingError→AgenticCascadeError
  chain through the real orchestrator and cascade, asserting the same sanitized 503.
- `test_submission_evidence.py` asserts against a frozen historical receipt — **open by
  design**: the receipt records sha256 hashes of intentionally-untracked raw artifacts
  (`tracked is False` is asserted); regenerating it requires a live soak run, which is part of
  the droplet-recreation acceptance gate, not the unit suite.
