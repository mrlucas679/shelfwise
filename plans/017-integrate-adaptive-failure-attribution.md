# Plan 017 — Integrate adaptive failure attribution

State: DONE
Priority: P1
Scope: incremental observability only; no architecture migration

## Research principle being adapted

ShelfWise will adapt the useful operational principle from *Tracing Agentic Failure from
the Flow of Success*: learn the normal shape of verified successful executions, score later
steps for suspicious deviation, and retain bounded evidence identifying the earliest/highest
deviation. ShelfWise will not reproduce the paper's neural-CDE implementation or introduce a
second trajectory architecture. Its existing structured receipts provide a safer and smaller
extension point than model-hidden-state extraction.

## Existing execution map

| Stage | Existing component | Current responsibility | Integration decision |
|---|---|---|---|
| Task/event creation | `routes_scenarios.py`, connector/webhook routes, `Event` | Validate caller input and create a tenant/domain/correlation-scoped event | No contract change |
| Durable intake | `ingest_pipeline.record_pipeline_event` | Store event, project operational state, publish to the configured event bus | No ordering change |
| Dispatch | `CascadeDispatcher`, `CascadeWorker` | Select the existing deterministic cascade and preserve causality | No new dispatcher |
| Agent execution | `AgentOrchestrator`, `OpenAIModelRuntime` | Run bounded model/tool loops with live-required enforcement | Reuse model-call receipts |
| Tool calls | `PlatformToolRegistry`, `ToolExecution` | Validate read-only tool calls, enforce tenant overrides, record timing/results | Derive only bounded metadata; never copy raw tool results into attribution |
| Evidence | `EvidenceObject`, `TraceSpan` | Record agent conclusions, sources, confidence, and decision-science timing | Derive structured step representations |
| Decisions | `Decision`, `attach_decision_governance`, decision store | Enforce Critic/HITL policy and persist the canonical decision | Use the existing structural gate when deciding whether a run is a successful reference |
| Trace persistence | `record_cascade`, `TraceRegistry` | Store recent tenant/domain-scoped cascade traces | Extend this registry; do not add a trajectory store |
| Failed inference | `record_model_run`, model-run registry | Persist exact model/provider/status provenance, including failures | Add a bounded failed-run trace only when the feature is enabled |
| Verification/evaluation | `FullSystemReport`, capability tests, Playwright | Fail on missing receipts, route failures, identity reuse, offline live-required output | Add deterministic attribution contract and disabled-mode tests |
| User observability | `GET /trace/{correlation_id}`, `GET /traces` | Return current trace receipts to authenticated tenants | Return optional attribution inside the existing trace payload |

## Exact files to extend

| File | Small compatible extension |
|---|---|
| `src/shelfwise_backend/adaptive_attribution.py` | New missing capability only: structured representation, verified-success predicate, bounded one-class scoring, and feature configuration |
| `src/shelfwise_backend/trace.py` | Add optional attribution/private representation fields and a tenant/domain/family-scoped successful-reference reader |
| `src/shelfwise_backend/ingest_pipeline.py` | Invoke attribution immediately before the existing trace write when enabled |
| `src/shelfwise_backend/model_runs.py` | Convert an already-recorded failed model receipt into an attributed failed trace when enabled |
| `.env.example` | Document the disabled-by-default flag and bounded calibration settings |
| `tests/test_adaptive_attribution.py` | Prove disabled compatibility, warming/calibration, deviation location, isolation, failure capture, and training/replay safety |
| `capabilities/*` | Register the existing trace API coverage and new verified workflow through the established generated contract |

## Genuinely missing capability

ShelfWise has trace capture and exact model/tool provenance, but no component compares a new
execution with verified successful executions or identifies a suspicious step. The new module
will calculate that derived evidence. It will not persist a second copy of events, decisions,
model prompts, tool results, or trajectories.

## Compatibility and safety rules

- `SHELFWISE_ADAPTIVE_ATTRIBUTION_ENABLED` defaults to `false`.
- When disabled, `record_cascade`, model-run recording, trace storage, API responses, and
  frontend behavior follow their previous path byte-for-byte.
- Reference sets are isolated by tenant, data domain, and trajectory family.
- Only structurally verified successes enter the reference set.
- Operational-twin traces are never labeled as future-training candidates.
- Suspicious traces produce evidence for manual replay review only; they never trigger replay,
  write-back, promotion, or training.
- Representations contain bounded structural metadata and counts, never prompts, response text,
  raw tool arguments/results, secrets, or personal data.
- The existing trace-registry bound remains the memory bound for calibration evidence.

## Acceptance checks

- [x] Disabled flag preserves the prior trace payload and failed-model behavior.
- [x] Successful traces warm and calibrate a tenant/domain/family reference set.
- [x] A changed or failed step receives a deterministic score and precise suspected-step receipt.
- [x] Other tenants, data domains, and trajectory families cannot influence the score.
- [x] Failed model receipts remain inspectable by correlation ID without exposing raw errors.
- [x] No attribution path auto-replays, auto-trains, or writes to operational systems.
- [x] Operational traces are ineligible for training; simulation candidates still require review.
- [x] Full backend, lint, capability, frontend, and browser gates pass.
