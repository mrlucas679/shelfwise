# Plan 006: Break-the-Application Test Campaign

> **Working-product branch boundary:** implementation belongs on `developers`; `main` requires an
> explicit release decision.

**Source:** the forensic audit `reports/soak_audit_20260714.md` (2026-07-14). That audit proved
today's "passing" campaign never aimed at the places the system can actually break: the app was
only ever driven single-threaded in-process on the memory backend, 88% of route receipts were one
endpoint eating always-valid generated input, the two soak runs shared half their data, the
agentic path ran once per cascade, and two production defects (SLO arithmetic, zombie inference)
were found only by reading the wreckage. This plan converts every audit finding into an
executable fix, in dependency order, with acceptance criteria an implementing agent can verify
without asking questions.

**Prime directive for the campaign itself:** a test campaign that cannot produce a red result is
not a test campaign. Phase C must demonstrate at least one induced failure (fault injection
rejected correctly, a measured breaking point, or a recovered outage) or it has failed at its own
job.

**GPU boundary:** Phases A–C need no GPU and no money. Phase D is the only part that spends
droplet credit, and it must not start until A–C are green.

## Execution status (2026-07-14)

| Slice | Status | Verified boundary |
|---|---|---|
| A1 deadline-aware execution | LOCALLY COMPLETE | typed deadline exception, structured 503, no-deadline regression tests |
| A2 zombie-work bound | LOCALLY COMPLETE | outbound timeout is capped by remaining request budget |
| A3 SLO-fit cascades | LOCAL WORK COMPLETE; LIVE GATE PENDING | 400-token verdict cap, at least 40% conclusion-payload shrink, token receipts; D1 must prove <29s |
| B1 latency receipts | COMPLETE | per-request duration plus per-route p50/p95/p99/max manifest summary |
| B2 independent seeds | COMPLETE | fresh default ranges and explicit historical-seed reproduction |
| B3 scaled agentic probes | COMPLETE | invoked every N cycles, rotating cascade receipts and per-cascade totals |
| B4 fault injection / blackout | LOCAL WORK COMPLETE; LIVE BLACKOUT PENDING | six corruption types have no-side-effect tests; 10% memory campaign passes; fake blackout proves receipt logic |
| B5 reuse audit | COMPLETE | one deterministic/agentic convergence allowed; same-source reuse still fails |
| B6 dissenting autopilot | COMPLETE | deterministic dissent, terminal rejection learning, zero rejected write-backs |
| B7 adversarial chat | COMPLETE | off-catalog, multi-turn, hostile corpus; full bounded samples; post-hoc grounding receipts |
| C1-C4 deployed break campaign | COMPLETE | Real deployed-topology capacity ramp, saturation, chaos, and 32-thread race campaign completed; see `reports/break_campaign_20260715T000000Z.md`. |
| D1-D4 live GPU campaign | READY FOR EXTERNAL EXECUTION | Phase C is green. Execution requires an approved GPU rental, current credentials, and a public HTTPS origin; no application code remains to implement. |

Current local evidence: the full repository suite passed `649` tests with `6` skipped, Ruff passed,
and the
regenerated `201`-capability contract passed with fingerprint
`sha256:b175c582b30ef8eca824e2b435240b13a45a8f1b25d32721349dc9bf38e51928`.

---

## Phase A — Fix the two production defects (app code, no GPU)

### A1. Deadline-aware cascade execution

**Defect:** the orchestrator (`src/shelfwise_inference/orchestration.py::run_messages`) runs up
to 8 sequential model calls with no awareness of the request deadline. The 29s middleware
(`app.py::enforce_request_deadline`) kills the *client connection* while the cascade continues.
At the measured ~19 tokens/sec effective (audit §1), golden/cold-chain arithmetically cannot
finish, so every run 504s after burning full GPU cost.

**Change:**
1. Add `deadline: float | None = None` (a `time.monotonic()` timestamp) to
   `AgentOrchestrator.run(...)` and `run_messages(...)`.
2. At the top of each loop iteration, before issuing the next model call: if
   `deadline is not None and time.monotonic() + _MIN_CALL_BUDGET_S > deadline`, raise a new typed
   `CascadeDeadlineExceeded(AgentOrchestrationError)` carrying `completed_model_calls`,
   `elapsed_ms`, and the partial tool-execution receipt. `_MIN_CALL_BUDGET_S` starts at 4.0
   (audit-measured strong-tier fixed overhead ~3.1s).
3. Thread `deadline` from the route handlers through every `run_*_via_agents` signature in
   `src/shelfwise_backend/agentic_cascade.py` (compute it at route entry as
   `time.monotonic() + _request_timeout_seconds() - 1.0`).
4. Route handlers catch `CascadeDeadlineExceeded` and return **503** with a structured body:
   `{"detail": "cascade could not finish inside the response deadline", "completed_model_calls":
   N, "elapsed_ms": M}` — a typed fast failure instead of a middleware 504.

**Acceptance:**
- New tests in `tests/test_agent_orchestration.py`: a fake runtime whose calls each consume 10
  simulated seconds against a 15s deadline stops after call 1 and raises
  `CascadeDeadlineExceeded`; with no deadline the behavior is unchanged (all existing tests
  green).
- A route-level test (`tests/test_agentic_http_errors.py`) asserting the 503 body shape.
- No live-required test weakened: `live_required` semantics unchanged.

### A2. Bound zombie inference to zero

**Defect:** agentic routes are sync `def` handlers on the threadpool; threads cannot be
cancelled, so cascades run to completion on the GPU after the 504 (audit §6). Under concurrency
this self-amplifies.

**Change (pragmatic, no async rewrite):** the per-call HTTP timeout must never exceed the
remaining request budget. In the model runtime (`src/shelfwise_backend/tools/model_runtime.py`)
accept an optional `deadline` and set the outbound HTTP client timeout to
`min(LLM_TIMEOUT_SECONDS, max(remaining, 1.0))`. Combined with A1, no HTTP call — and therefore
no meaningful GPU generation on behalf of a dead client — can outlive the deadline by more than
the in-flight token.

**Acceptance:**
- Test: with 2.0s remaining budget, the fake transport receives `timeout <= 2.0`.
- Test: with no deadline, timeout equals the configured `LLM_TIMEOUT_SECONDS` (no behavior
  change).

### A3. SLO-fit the golden and cold-chain cascades

**Defect:** the two largest-payload cascades need >29s of sequential generation (audit §1).

**Change:**
1. Cap the final-verdict `max_tokens` for all cascades at 400 (currently up to 800/900) — the
   verdict is a decision + cited figures, not an essay.
2. Trim the evidence payload passed into the strong-tier prompt to the fields the grounding
   check actually needs (`conclusion`, `supporting_data` numbers, `risk_tier`) — drop verbose
   prose duplication before the critic/executive calls.
3. Emit a `token_budget` receipt on each agentic response (`prompt_tokens`, `completion_tokens`,
   `calls`) so SLO regressions become visible in every future run, not just timed-out ones.

**Acceptance:**
- Offline: prompt-size assertion tests showing golden/cold-chain strong-tier prompts shrink ≥40%
  versus the current fixture payloads; grounding assertions still pass on the trimmed evidence.
- Live (Phase D3): both cascades complete under 29s on the same droplet class that failed today.
- Honesty constraint: do NOT raise `SHELFWISE_REQUEST_TIMEOUT_SECONDS` — the 30s ceiling is a
  competition requirement, not a tunable.

---

## Phase B — Harness honesty upgrades (`src/shelfwise_eval/full_system.py`, no GPU)

### B1. Latency in every route receipt

Add `duration_ms: int` to `RouteReceipt` (captured around `self.client.request`), and a
`route_latency` summary (p50/p95/p99/max per route key) to the manifest totals. The audit's
"what is p95 for /chat?" must be answerable from every future run.

**Acceptance:** manifest of a short offline run contains per-route percentiles; existing receipt
assertions unchanged.

### B2. Independent run seeds

Default `base_seed` becomes `int(run-stamp digits) mixed with the old default` unless
`--base-seed` is passed explicitly; the effective seed is already recorded in the manifest, so
reproduction stays one flag away. Two consecutive default runs must not share a single
(seed, scenario) cycle pair.

**Acceptance:** test that two configs built seconds apart produce disjoint seed ranges; test that
`--base-seed 20260710` still reproduces today's worlds exactly.

### B3. Agentic coverage that scales with duration

New `--agentic-every-n-cycles N` (default 25): during the world rotation, every N cycles run one
agentic cascade (rotating through all six) against the live model when `live_required`, recording
a receipt per execution. A 30-minute run should produce ~70 agentic executions, not 6. Keep the
existing end-of-run one-shot sweep as the minimum floor.

**Acceptance:** offline run with a fake orchestrator shows receipts scale with duration; totals
gain `agentic_executions_by_cascade`.

### B4. Fault injection — the actual "try to break it" machinery

New flags:
- `--fault-rate P` (default 0 for compatibility; the break campaign uses 0.10): P% of submitted
  world events are deliberately corrupted, drawn round-robin from: schema-invalid payload,
  oversized body (>6MB cap), wrong-tenant id, duplicate event id, stale timestamp, unparseable
  number. The harness asserts each is **rejected with the correct 4xx and produces zero
  decisions, zero twin writes, zero learning events** — a fault that slips through is a run
  failure.
- `--blackout-seconds S` (default 0): mid-rotation, repoint the inference base URL at a dead
  port for S seconds. Assert: during the window every live-required model route fails closed
  (503, zero offline answers); after the window, chat recovers within one cycle. Recovery time
  is recorded in the manifest.

**Acceptance:** offline tests for each corruption type; a memory-backend run with
`--fault-rate 0.1` finishes green *because* the faults were rejected; the manifest reports
`faults_injected`, `faults_correctly_rejected`, `blackout_recovery_cycles`.

### B5. Fix the `decision_reuse` false positive

`audit_full_system_integrity` flags the deterministic `/demo/procurement` and agentic
`/demo/procurement/agentic` converging on the same scenario-stable decision ID (by design).
Teach the audit that a reuse across a known deterministic/agentic source pair for the same
scenario is expected; genuine same-source reuse remains a failure.

**Acceptance:** regression test reproducing today's trail pattern passes; a true duplicate
(same source, same ID, two mints) still fails.

### B6. An autopilot that disagrees

The reviewer currently approves whatever the Critic approved — 2,542 rubber stamps prove
transition echo, not judgment handling. Add deterministic dissent: every Kth approvable decision
is rejected instead. Assert rejections produce no write-back task, learning records the
rejection outcome, and the decision is terminal.

**Acceptance:** offline run shows a rejected share within the configured band and zero write-back
tasks for rejected decisions.

### B7. Adversarial, multi-turn chat in the soak

Replace the single question template with a corpus: the existing template, off-catalog products,
multi-turn follow-ups (reusing `conversation_id`), and the hostile strings from
`tests/test_gateway_security.py` at volume. Retain full untruncated transcripts for a bounded
random sample (e.g. 50) and run `assert_conclusion_grounded_in_tool_results` post-hoc over the
agentic ones.

**Acceptance:** manifest gains `chat_corpus_breakdown`; hostile inputs produce sanitized, fenced
prompts (no run failure); grounding audit reports pass/fail counts.

---

## Phase C — Local full-stack break campaign (production compose, no GPU)

Run against `docker-compose.production.yml` (Nginx → uvicorn → **Postgres** → Redis) — the real
topology the audit proved has never seen concurrency. Offline-safe: no live model needed; model
routes fail closed by design and that behavior is itself under test.

### C1. HTTP mode for the harness + concurrent virtual users

Add `--base-url` (drive a real server over HTTP instead of TestClient) and `--virtual-users N`
(N worker threads each running the cycle loop with its own session). Ramp 1 → 8 → 32 → 64 and
record, per ramp step: route p95/p99 (from B1), error counts by status, Postgres pool behavior.
**The goal is to find the number where it breaks** — pool exhaustion, 5xx, or p95 collapse — and
record it as the measured capacity, not to stop at a green 32.

### C2. Sustained ingest saturation

One stage with the 80-event/cycle cap lifted: submit as fast as the stack accepts for 5 minutes
at 8 users. Pass criteria: zero 500s (429/503 shedding is acceptable and correct), zero data
loss (accepted events all queryable afterwards), RLS intact (cross-tenant probe mid-flood).

### C3. Chaos on the durable substrate

- `docker stop redis` mid-run for 30s: bus degrades, no crash, clean recovery, no lost decisions.
- `docker restart backend` mid-run: readiness gate holds traffic, twin projection hash identical
  after restart (the recovery guarantee built on 2026-07-14 morning, now tested under load).

### C4. Concurrency race hunts over HTTP

32 threads simultaneously: approve+reject the same decision (exactly one terminal state, one
write-back task — validates the `TaskWriteBackSink` lock fix under a real server); duplicate twin
observations (one projection); duplicate connector intakes (one record). Any double-mint is a
run failure.

**Phase C acceptance:** a written breaking-point report (`reports/break_campaign_<stamp>.md`)
containing the measured capacity number, every induced failure and its correct handling, and any
new defect filed. If nothing red appeared and no breaking point was found by 64 users, the
campaign is incomplete — escalate users until one is found.

---

## Phase D — Next live GPU session (spend credit only after A–C are green)

Est. 60–75 droplet-minutes total (~$2.50 at today's rate). Runbook order:

1. **D1 (5 min):** bootstrap + 15-second sanity (existing runbook), then verify A3: all six
   agentic cascades complete < 29s. If golden/cold-chain still exceed, stop — fix locally first,
   don't burn the session.
2. **D2 (15 min):** 15-minute soak with the Phase B harness: fresh seed, `--fault-rate 0.1`,
   `--blackout-seconds 30`, `--agentic-every-n-cycles 25`, full latency receipts, adversarial
   chat corpus, Postgres backend via the compose stack, 8 virtual users. This single run
   supersedes everything measured on 2026-07-14.
3. **D3 (10 min):** concurrency **through the app**: 1/4/8 concurrent users driving
   `/chat` and rotating `/demo/*/agentic` — the number the judges will actually ask about.
4. **D4 (30 min):** only if D2/D3 are clean: the 30-minute endurance run, fresh seed.

Artifacts: timestamped dirs (overwrite guard already enforces this), plus
`/root/shelfwise-mi300x-bootstrap.json` captured before droplet destruction.

---

## Execution order and estimates

| Item | Depends on | Effort | GPU |
|---|---|---|---|
| A1 deadline-aware orchestrator | — | M | no |
| A2 zombie-work bound | A1 | S | no |
| A3 SLO-fit cascades | A1 | M | verify in D1 |
| B1 latency receipts | — | S | no |
| B2 independent seeds | — | S | no |
| B3 scaled agentic probes | — | M | no |
| B4 fault injection + blackout | B1 | L | no |
| B5 harness false-positive fix | — | S | no |
| B6 dissenting autopilot | — | S | no |
| B7 adversarial chat corpus | B1 | M | no |
| C1 HTTP mode + ramp | B1, B4 | L | no |
| C2 ingest saturation | C1 | M | no |
| C3 chaos (Redis/backend) | C1 | M | no |
| C4 race hunts | C1 | M | no |
| D1–D4 live session | A*, B*, C* | ops | yes |

Global gates after every item: `python -m pytest -q` green, `ruff` clean,
`python scripts/compare_capability_manifests.py` OK (regenerate on any route/tool change), and
update this plan's status in `plans/README.md`.
