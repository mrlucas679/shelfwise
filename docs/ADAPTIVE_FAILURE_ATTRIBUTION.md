# Adaptive failure attribution

ShelfWise includes an optional, disabled-by-default monitor that compares an execution with
recent structurally verified successes and identifies suspicious step deviations. It adapts
the operational principle from [Tracing Agentic Failure from the Flow of
Success](https://arxiv.org/abs/2607.12747); it does not reproduce the paper's neural-CDE
architecture.

## Why this fits ShelfWise

ShelfWise already records the data needed for a safe incremental implementation:

1. `Event` provides tenant, data-domain, causation, and correlation identity.
2. deterministic cascades emit `TraceSpan` and `EvidenceObject` receipts;
3. agentic cascades add model and read-only tool-call telemetry;
4. `record_cascade` applies governance, persists the `Decision`, and records one
   `CascadeTrace`;
5. failed live model calls are already persisted as `ModelRun` receipts;
6. `/trace/{correlation_id}` and `/traces` are the authenticated observability surface.

The implementation extends those paths. It does not add an event stream, trajectory database,
agent hierarchy, replay engine, or training pipeline.

## Configuration

```dotenv
SHELFWISE_ADAPTIVE_ATTRIBUTION_ENABLED=false
SHELFWISE_ATTRIBUTION_MIN_SUCCESSES=5
SHELFWISE_ATTRIBUTION_ALPHA=0.05
```

The minimum reference count is bounded from 2 to 200. Alpha is bounded from 0.001 to 0.25.
Invalid enabled-mode settings fail closed.

## Representation and scoring

The monitor derives a bounded representation of at most 64 existing steps:

- span name, status, duration, and detail-field count;
- model role/status/provider/model, network/fallback flags, token counts, and latency;
- tool name and bounded result-size metadata, never raw arguments or results;
- evidence agent, source/fact counts, confidence, and review flag;
- decision action, status, risk tier, and Critic verdict.

References are scoped by tenant, data domain, and trajectory family. Only executions whose
server-owned receipts contain a valid decision, evidence, successful span/model statuses, and
the required agentic model/tool receipts qualify as successes. The monitor builds robust
per-position profiles from median values and majority categorical fields, calibrates an
empirical upper threshold from the reference scores, and returns the highest-scoring suspected
step.

This structured score is intentionally simpler than latent-state neural attribution. The
configured OpenAI-compatible providers do not expose stable hidden states, and adding a model
training/runtime dependency solely to imitate the paper would conflict with ShelfWise's
architecture and production boundaries.

## Safety and behavior

- With the flag disabled, the original trace-write path and response shape are unchanged.
- Attribution never copies prompts, response text, raw error bodies, tool arguments, tool
  results, credentials, or personal data.
- A suspicious trace recommends controlled replay review but cannot trigger replay.
- No trace triggers write-back, skill promotion, model routing, or training automatically.
- Operational-twin traces are always ineligible as training sources.
- A verified, non-anomalous world-simulation trace is only a review candidate; dataset review,
  provenance checks, and redaction remain mandatory.
- Calibration state is bounded by the existing in-process `TraceRegistry`. Durable cross-restart
  profile persistence is deliberately deferred until there is measured need; it must store
  aggregates, not duplicate trajectories.

## Observability

The existing trace endpoints return an `adaptive_attribution` object only when the feature is
enabled. The Operations workspace shows its state, reference count, and suspected step in the
existing Trace registry section.

States:

- `warming_up`: fewer than the configured verified references exist;
- `normal`: the trace is within the calibrated success profile;
- `suspicious`: the anomaly score exceeds the calibrated threshold.
- `failed_unscored`: execution failed before enough references existed for a score;
- `failed`: server verification failed even though its structural deviation stayed below the
  calibrated anomaly threshold.

The full-system simulation harness, backend contracts, and isolated Playwright flow exercise
the feature when explicitly enabled. Live provider validation still requires a configured
endpoint and is not inferred from deterministic tests.
