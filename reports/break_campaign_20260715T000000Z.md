# Phase C break campaign — completion report

Run dates: 2026-07-14 (partial + repairs) and 2026-07-15 (C1/C4 completion).
Topology: `docker-compose.production.yml` — Nginx (`http://localhost`) → uvicorn backend
(`APP_ENV=production`, JWT auth, `SHELFWISE_STORE_BACKEND=postgres`, `SHELFWISE_BUS_BACKEND=redis`)
→ Postgres (pgvector/pg16) → Redis (7-bookworm). LLM endpoints point at a dead local port, so
every model route must fail closed fast — that behavior is itself under test. No GPU spend.

## Stage results

| Stage | Result | Evidence |
| --- | --- | --- |
| Baseline authenticated HTTP shakedown | PASS | 3 cycles, real decisions approved/rejected, chat failed closed 503 in ~2.5s. `phase_c/shakedown_smoke_20260714.json` |
| C1 concurrency ramp 1→8→32→64→128 | PASS with measured boundary | Zero 5xx and zero transport errors at every step. `phase_c/ramp_20260715.json`, `phase_c/ramp_escalation_20260715.json` |
| C2 five-minute ingest saturation | PASS (run 2026-07-14) | 300.3s, 667 requests, 100% HTTP 200, p95 634ms. `break_campaign_20260714T232000Z.saturation.json` |
| C3 Redis 30s stop + backend restart | PASS at health boundary (run 2026-07-14) | Health 200 before/during/after; clean recovery. `break_campaign_20260714T203355Z.md` |
| C4 32-thread race hunts | PASS — after two real defects were found and fixed | `phase_c/races_20260715.json` |
| Data-loss check | PASS | 2,200 accepted ramp events == 2,200 rows in `shelfwise_events` |

## C1 measured capacity (the number the judges will ask about)

| Users | Requests | 200s | 429s | p50 ms | p95 ms | p99 ms | 5xx |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 107 | 107 | 0 | 507 | 896 | 1,159 | 0 |
| 8 | 387 | 387 | 0 | 1,117 | 2,071 | 2,364 | 0 |
| 32 | 548 | 548 | 0 | 3,540 | 4,707 | 5,372 | 0 |
| 64 | 4,623 | 568 | 4,055 | 380 | 4,460 | 6,571 | 0 |
| 128 | 5,563 | 590 | 4,973 | 943 | 3,747 | 8,805 | 0 |

- **Sustained accepted-write capacity: ~9–10 events/second** on this host (backend capped at
  2 CPUs by the compose file). Latency knee is at 32 users (p95 ≈ 4.7s, still all-200).
- **From 64 users up, the write rate limiter is the protective boundary**: everything above
  capacity is shed with fast 429s (p50 drops because 429s return instantly), and the stack
  never produced a single 5xx or dropped connection up to 128 concurrent users.
- Per the campaign's own rule, 429/503 shedding is correct behavior, not a failure. The
  "breaking point" is therefore a *protected saturation boundary*, not a crash: no crash
  point exists up to 128 users on this hardware.

## C4 races — what broke, and the fixes (the campaign's red results)

All three races run 32 simultaneous threads behind a barrier against the real server.

1. **HITL approve/reject the same decision — PASS.** Exactly one terminal state
   (`rejected`) won; zero write-back tasks minted for a rejected decision; zero 5xx. The
   `TaskWriteBackSink` lock fix held under a real server.
2. **Duplicate twin observations — PASS.** 1 projected, 31 deduplicated, zero 5xx.
3. **Duplicate connector intakes — FAILED twice, fixed, then PASS (1 accepted / 31 duplicate / zero 5xx).**
   - **Defect 1 (every intake 500'd, even sequentially):** `schema.sql` still declared the
     pre-widening 3-column dedup key while `PostgresInboundRecordStore`'s `ON CONFLICT`
     names 4 columns → `InvalidColumnReference` on every deployed-Postgres intake. The
     auto-schema path (`_ensure_schema`) had the widening migration; the production migrate
     path (`schema.sql`, used when `SHELFWISE_AUTO_SCHEMA=false`) never got it. Memory-backend
     tests structurally cannot catch this. Fixed by porting the identical migration into
     `schema.sql`; pinned by a new static drift test
     (`test_schema_sql_dedup_key_matches_the_postgres_store_on_conflict_columns`) and
     write-path coverage in `test_postgres_schema_contract.py`.
   - **Defect 2 (2/32 concurrent intakes 500'd):** the stored `id` primary key is a pure
     function of the dedup-key fields, and `ON CONFLICT` only arbitrates on its named
     constraint — two truly simultaneous inserts of one record can violate the pkey before
     the arbiter resolves (`UniqueViolation`). Fixed in `inbound_store.record()`: a
     concurrent duplicate is handled as a duplicate (rollback → select existing → one
     bounded retry), never a server error.

## Prior repairs carried into this run (2026-07-14)

- Migration container startup race (connection-refused before Postgres ready): bounded retry.
- Secure-cookie-over-HTTP harness bridge (in-memory bearer, never serialized).
- Stale destroyed-droplet LLM IP black-holing chat for 25s/call: repointed at a dead local
  port so fail-closed is instant; `.env` carries the Phase D reminder to restore the real IP.

## Completion boundary

Phase C is **complete**: capacity ramp, saturation, chaos, and race stages all executed
against the real deployed topology, red results were produced and fixed, and no crash point
was found up to 128 concurrent users. Gates green: full pytest suite, ruff, capability
manifest. **Phase D (live GPU session) is unblocked**, pending its separate GPU/credit
authorization and a fresh droplet whose IP replaces the dead-port LLM endpoints in `.env`.
