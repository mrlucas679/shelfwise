# ShelfWise Engineering Audit Report — 2026-07-15

Full-system correctness audit conducted with the operating principle *"passing tests are not
evidence of correctness — verify via real execution paths."* Every claim below is backed by an
executed probe, a reproduced failure, or a read of the actual code path — not by suite greenness.
Bugs found during the audit were fixed in place and re-verified; nothing here is a TODO left for
later unless explicitly marked as a roadmap item.

Companion documents: [TEST_SUITE_CRITIQUE.md](TEST_SUITE_CRITIQUE.md) (per-file critique of all
113 test files), [.claude/skills/veteran-test-audit/SKILL.md](.claude/skills/veteran-test-audit/SKILL.md)
(the methodology).

---

## 1. What was verified by real execution (not mocks)

### 1.1 Redis Streams event bus — real Redis 7, 19 lifecycle probes, ALL PASS
The production queue (`RedisStreamsEventBus`) had only ever been tested against a hand-rolled
`FakeRedis` with call-count assertions. Probed against a real Redis server:
publish→consume→ack; nack→pending-history redelivery to the same consumer;
`times_delivered` incrementing on history reads (the subtle semantic the dead-letter path
depends on); dead-lettering past max_retries with the `:dead` stream carrying the event;
a dead consumer's pending message being invisible to the live consumer until `reclaim_stale`
(`XAUTOCLAIM`) claims it; approximate `MAXLEN` trimming; stream discovery via `SCAN`; `clear()`
removing dead streams too. **No defects.** The implementation's Redis semantics are correct.

### 1.2 Full production topology — first time ever exercised end-to-end
Real Postgres (least-privilege `shelfwise_app` role, RLS forced) + real Redis bus +
`WORKER_ENABLED=true` (live worker thread) + `SHELFWISE_AUTH_MODE=jwt`, simultaneously — the
exact `docker-compose.production.yml` configuration. Probes, ALL PASS:
- unauthenticated requests rejected (401);
- `/ingest` accepts, defers cascade to the queue, publishes to real Redis;
- the worker consumes off Redis, runs the cascade, persists a pending decision in Postgres;
- cross-tenant decision read and approve are 404s (both at the app layer and, independently,
  at the RLS layer — an unbound direct `decision_store.list()` is correctly blinded);
- owner approve succeeds and produces a real learning event;
- learning is tenant-scoped end to end (tenant B sees `[]`);
- duplicate ingest of the same event id dedupes;
- `/events/bus` listing is tenant-scoped.

### 1.3 Learning-record concurrency — bug found, fixed, proven
See §2.1. Fix verified by racing two genuinely concurrent approvals of the same decision
against real Postgres, 5/5 rounds: both callers 200, exactly one learning event each round.

### 1.4 Suite gates
Full test suite: **681 passed, 7 skipped** (skips are correctly env-gated live-Postgres/live-GPU
tests). Ruff clean. Capability manifest regenerated and contract-verified (9/9).
Frontend `tsc --noEmit` clean (verified earlier this session after the scenario-route rename).

---

## 2. Bugs found and fixed during this audit

### 2.1 CRITICAL→fixed: double-approve race 500s the losing caller and can double-record learning
**Where:** `src/shelfwise_memory/__init__.py`, `PostgresLearningStore.record_approved_decision`
and `record_rejected_decision`.
**Root cause:** the decision store's terminal transition (`update … where status='pending'
returning payload`) deliberately lets the loser of a concurrent double-approve through with the
already-approved record (correct — approval is idempotent). Both callers therefore reach
learning recording inside the same race window. The learning store's idempotency was
check-then-insert: both see "no existing event," both INSERT; the
`(tenant_id, data_domain, decision_id)` primary key kills the loser with a unique violation →
an HTTP 500 for an approval that genuinely succeeded (classic double-click scenario).
**Fix:** the INSERT now carries `on conflict (tenant_id, data_domain, decision_id) do nothing
returning payload`; on conflict the winner's persisted row is re-read and returned. Both racers
compute an identical event from the same `previous_threshold` (the first-existing check bounds
the window to same-decision races), so returning the winner's row is exact.
**Proof:** 5/5 concurrent race rounds pass against real Postgres; permanent regression test
added (`tests/test_postgres_schema_contract.py::test_concurrent_double_approve_learning_never_500s_and_records_once`).

### 2.2 Fixed earlier this session, same audit thread (context for completeness)
- **RLS tenant-binding gap in `PostgresDecisionStore.upsert()`** — writes relied on ambient
  session tenant instead of binding the record's own tenant; the synchronous cascade path had
  no ambient binding → `InsufficientPrivilege` RLS rejection in production config. Fixed by
  binding the record's tenant explicitly (matching `PostgresEventStore`'s pattern).
- **`InMemoryCandidateStore` history-store leak** — its history sub-store defaulted through an
  env-sensitive factory, silently persisting "in-memory" candidate history to real Postgres
  under production env. Fixed by hard-wiring backend-matched history stores; dead factory
  removed.
- **Tautological synthdata eval test** — `run_suite`'s scorer had no test that could ever
  observe a mismatch; a falsifiable test was added and mutation-checked (breaking `_check`
  makes it fail).

---

## 3. Communication-path audit

| Path | Mechanism | Verdict (evidence) |
|---|---|---|
| FE→BE | fetch + AbortController (22 uses), same-origin nginx proxy | Sound; stale responses aborted; no dangerouslySetInnerHTML anywhere |
| BE→DB | psycopg per-call connections, RLS via `set_config` per connection, superuser/BYPASSRLS refused at connect | Correct (verified live); efficiency concern §6.1 |
| BE→queue | Redis Streams, consumer group, durable-store-first ingest with `published` flag self-heal | Verified live (§1.1, §1.2); lost/duplicate/out-of-order all handled: dedup by event id + payload equality → 409 on content drift, unpublished-but-recorded events republish on retry |
| Worker→DB | binds `event.tenant_id` ambient before every cascade/persist | Correct (read + live probe) |
| BE→LLM | bounded deadline budget per turn, retry bounds, fail-closed LIVE_REQUIRED, guarded JSON | Unit-verified with scripted runtimes; live vLLM endpoint currently absent (droplet destroyed) — see §8 gap |
| Auth | HS256 JWT, cookie `httponly+secure+samesite=strict`, per-tenant RLS backstop | Verified live incl. cross-tenant 404s |

Partial-failure behavior of `/ingest` deserves explicit mention because it is *right*: the event
records first, then publishes; a bus failure between the two leaves a recorded-but-unpublished
event that self-heals by publishing on the next identical submit instead of short-circuiting as
a fake "duplicate" (`_record_pipeline_event`, tested in `test_event_ingest.py`).

## 4. AI-agent layer audit

- Tool-call correctness: the orchestrator rejects model-invented `tenant_id`s, malformed JSON
  arguments, schema-violating final answers, and conclusions that cite no real tool numbers
  (`AgenticCascadeError`) — all covered by deliberate adversarial tests
  (`test_model_tool_calling.py`, `test_agentic_*_cascade.py`).
- Hallucination-driven state corruption: all tools registered to the cascade are read-only;
  writes happen only through the governed decision/HITL path. Verified by reading
  `build_platform_tools` — no write-capable tool is exposed to the model.
- Timeouts/budgets: per-turn deadline math and retry bounds are regression-tested against
  live-observed failure modes (json_schema + forced-tool-call collision; deadline exhaustion).
- Parallel-agent interference: decision identity is scenario-stable (occurrence-counter ids),
  so concurrent cascades for the same scenario upsert rather than duplicate-mint; verified in
  the topology probe via the dedup behavior.
- **Gap (no live endpoint):** no probe in this audit could exercise a real vLLM/Fireworks
  round-trip because the GPU droplet was destroyed. Everything above is proven at the
  orchestration layer only. §8 makes this a pre-deploy blocker.

## 5. Database audit

- Schema/store contract: every Postgres store class exercised against a real least-privilege
  server this session (schema-contract test + write-path probes). `ON CONFLICT` column lists
  match real unique constraints (24 verified earlier via live double-apply).
- Indexes: decision list (`tenant, updated_at desc`), learning events (`tenant, domain,
  created_at desc`), events, chat — all present in `schema.sql` for the actual query shapes
  read in the code.
- Locks: chat uses `pg_advisory_lock` per conversation (verified concurrent 16-writer test);
  learning threshold uses `select … for update`; no lock-ordering cycles found (each
  transaction touches one aggregate).
- RLS: forced on all 30+ tenant tables; the app role is created NOSUPERUSER NOBYPASSRLS and
  `connect()` refuses superuser/BYPASSRLS roles at runtime. Verified live: unbound sessions see
  nothing; wrong-tenant writes are rejected.
- Migrations: idempotent additive DDL (`create if not exists` / `add column if not exists`),
  re-runnable migrate job in compose (verified by double-apply earlier in the session).

## 6. Performance / scalability audit

### 6.1 HIGH (roadmap): connection-per-call to Postgres
`shelfwise_storage.connect()` opens a fresh TCP connection (+ role check + `set_config`) for
**every store operation**. At the demo/showcase tier this is fine; at sustained concurrency it
multiplies latency (~1–3ms overhead per op locally, far worse over a network) and collides with
Postgres `max_connections` (default 100) well before 1K concurrent users.
**Concrete fix (not yet applied — architectural change, needs its own tested change):** adopt
`psycopg_pool.ConnectionPool`, with `set_config('app.tenant_id', …)` moved to a
per-checkout reset (pool `configure`/`reset` hooks), keeping the RLS contract per-checkout
instead of per-connect. All 50+ `_connect()` call sites already funnel through one function, so
the change is centralized.

### 6.2 Readiness by tier
- **100 users:** ready (single backend container, worker enabled, verified topology).
- **1K users:** needs §6.1 (connection pool) and a second uvicorn worker/replica; Redis and
  Postgres themselves are nowhere near limits at this tier.
- **10K users:** needs horizontal backend replicas (stateless — JWT + shared Postgres/Redis
  make this safe; the worker consumer-group already supports multiple consumers, verified by
  the reclaim probe), plus Postgres connection budgeting (pgbouncer or pool sizing).
- **100K–1M users:** out of scope of any current claim. Requires: partitioned event streams
  (per-tenant streams already exist — sharding is natural), read replicas, moving hot decision
  lists behind a cache with invalidation, and a real load test. No claim of readiness is made.

## 7. Security audit

- **SQL injection:** all queries parameterized; the only two f-string SQL sites interpolate
  hardcoded identifiers (verified by reading both). The detective CTE is parameterized and
  RLS-scoped.
- **Auth/access control:** JWT verified live; IDOR probes (cross-tenant read/approve) return
  404; RLS is an independent second layer, verified to blind unbound sessions.
- **XSS/CSRF:** no `dangerouslySetInnerHTML`/`innerHTML`/`eval` in the frontend; session cookie
  is `httponly`, `secure`, `samesite=strict`.
- **Path traversal:** uploads are content-addressed (SHA-256 name, whitelisted alnum suffix).
- **Secrets:** `.env` untracked and gitignored; no secret values logged (grep-verified); prompt
  spotlighting/fencing covered by `test_gateway_security.py`.
- **Prompt injection:** adversarial-note scenarios are part of the synthetic eval suite;
  tool surface is read-only (§4), so injected instructions cannot mutate state without HITL.
- **ACCEPTED RISK (by design):** the public showcase session (`SHELFWISE_PUBLIC_DEMO_SESSION`)
  mints MANAGER-role tokens for one *shared* public tenant — anonymous visitors can affect each
  other's showcase state (rate-limited, isolated from real tenants). Documented as accepted;
  revisit before any real-customer launch on the same deployment.
- **Container posture:** backend runs read-only rootfs, cap_drop ALL, no-new-privileges,
  non-root, mem/pid limits (compose-verified).

## 8. Risk matrix and pre-deploy verdict

| # | Risk | Sev | Status |
|---|---|---|---|
| 1 | Double-approve race → 500/duplicate learning | Critical | **Fixed + regression-tested (§2.1)** |
| 2 | RLS tenant-binding gap in decision upsert | Critical | **Fixed earlier this session, live-verified** |
| 3 | In-memory candidate store leaking history to Postgres | High | **Fixed earlier this session** |
| 4 | No live-LLM round-trip verified since droplet destroyed | High | **Open — external infra.** Requires recreating the billed MI300X droplet (owner action; see `docs/mi300x-recreate-runbook.md`), then `scripts/track3_prescreen.py` + one live agentic cascade as acceptance. Fail-closed behavior without the endpoint is verified. |
| 5 | Connection-per-call Postgres access | High (≥1K users) | **Implemented 2026-07-15**: `psycopg_pool` in `shelfwise_storage.connect()` — tenant re-bound session-level per checkout, cleared at check-in, superuser check per physical connection, `SHELFWISE_DB_POOL[_MIN/_MAX]` sizing, legacy path behind `SHELFWISE_DB_POOL=false`. Proven: all Postgres contract tests + full production-topology probe green pooled; 400-request/20-thread burst held server connections at exactly pool max (10) with zero errors. |
| 6 | Redis semantics unverified by CI (probe was manual) | Medium | **Implemented 2026-07-15**: `tests/test_redis_bus_contract.py`, env-gated on `SHELFWISE_TEST_REDIS_URL` (5 tests, green against real Redis); CI boots a Redis service and sets the variable. |
| 7 | Public shared showcase tenant with MANAGER role | Medium | Accepted by design; revisit at launch |
| 8 | Request-deadline middleware cancels the await, not the threadpool work (zombie work until completion) | Medium | **Documented 2026-07-15** in `docs/mi300x-recreate-runbook.md` (“Request-Deadline Semantics”); state stays consistent because writes are individually transactional + idempotent; primary inference bound lives in the LLM client. |
| 9 | Postgres contract tests in CI | — | **Correction:** CI already boots a schema-seeded Postgres and sets `SHELFWISE_TEST_DATABASE_URL` (`.github/workflows/ci.yml` “Boot Postgres integration database”) — the original finding was wrong for CI (it was true only of local runs). Redis was the actual CI gap; closed under #6. |
| 10 | Text-grep infra tests can't detect semantically-broken config | Low | Documented in TEST_SUITE_CRITIQUE.md |
| 11 | Fixed worker consumer name shared across replicas → double-delivery of pending messages under horizontal scale | High (≥2 replicas) | **Fixed 2026-07-15**: `CascadeWorker` defaults to a per-process consumer identity (`worker-<host>-<pid>`); crashed processes' pending messages recover via the existing 30s `reclaim_stale` sweep. |
| 12 | Critic verdict advisory-only in the four routing cascades — a hallucinating executive could escalate past a failed critic | High | **Fixed 2026-07-15**: deterministic `_enforce_critic_verdict` gate in `agentic_cascade.py` forces the safe action whenever the critic failed, on all four builders (golden, procurement, sales, cold-chain — the two guardrail cascades already failed closed); every decision now carries an auditable `critic_gate` receipt; falsifiable disagreement test added. |

**Verdict:** the system as configured for its current deployment (single compose stack, showcase
tenant + JWT tenants) is **correct under its real production topology** — proven end to end,
pooled, with the agent-to-agent contract now deterministically enforced. Scale posture: ready at
100 users; ready at 1K–10K users with pool sizing (`SHELFWISE_DB_POOL_MAX`) and horizontal
replicas (per-process worker consumers + consumer-group reclaim make replicas safe; JWT +
shared Postgres/Redis keep the backend stateless). The single remaining deploy blocker is
external: no model endpoint exists (risk #4) — recreate the MI300X droplet per the runbook and
run the live acceptance gate.
