# Change summary

Date: 2026-07-29
Scope: ShelfWise adaptive failure attribution and evidence-backed readiness only

## Product behavior added

- Derives a bounded structural representation from existing trace, model, tool, evidence, and
  decision receipts.
- Verifies successful references structurally and isolates them by tenant, data domain, and
  trajectory family.
- Scores suspicious deviations and reports the most likely step through the existing trace API.
- Captures safe correlated receipts for provider/model failures and later schema, tool,
  grounding, or deadline failures.
- Shows optional attribution state, reference count, and suspected step in the existing
  Operations trace registry.
- Registers the workflow in the existing capability manifest and exercises it in the full-system
  harness and browser suite.

## Compatibility and safety

- Disabled by default; legacy trace payload and failed-model behavior remain unchanged.
- No new event bus, trajectory database, API route, agent, inference provider, or deployment
  service.
- No raw prompts, responses, tool arguments/results, exception text, secrets, or personal data
  are stored in attribution representations.
- No automatic replay, training, promotion, decision, or operational write.
- Operational-twin traces are never eligible for training; simulation candidates require review.

## Debt and audit work

- Bounded failed-run lookup at the model-run registry/SQL layer by correlation ID.
- Removed one proven redundant full-system report assignment.
- Audited declared capabilities, empty/dead code, technical debt, literals/secrets,
  dependencies, schema/integrity, end-to-end paths, and production boundaries.
- Added the required evidence artifacts under `reports/` and completed Plan 017.

## Files most directly involved

- `src/shelfwise_backend/adaptive_attribution.py`
- `src/shelfwise_backend/trace.py`
- `src/shelfwise_backend/ingest_pipeline.py`
- `src/shelfwise_backend/model_runs.py`
- `src/shelfwise_backend/routes_scenarios.py`
- `src/shelfwise_mlops/registry.py`
- `src/shelfwise_eval/full_system.py`
- `frontend/src/App.tsx`
- `frontend/e2e/golden-path.spec.ts`
- `tests/test_adaptive_attribution.py`
- `docs/ADAPTIVE_FAILURE_ATTRIBUTION.md`
- `capabilities/manifest.json`

## Verification

Ruff, capability drift, 951 Python tests, frontend typecheck/build, 12 Chromium product flows,
dependency resolution, Node production audit, and schema/config contract tests pass locally.
Implementation commit `abe6924` also passed exact-SHA GitHub CI (971 tests, 1 skip, 12 browser
flows, fresh Postgres/Redis, wheel, Compose, production smoke, and shakedown) plus both capability
workflows. External proof boundaries are listed in `PRODUCTION_READINESS_REPORT.md`.
