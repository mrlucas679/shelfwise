# Production-readiness report

Audit date: 2026-07-29
Assessment: application code ready for controlled deployment; external acceptance remains

## Ready in the repository

| Area | Evidence |
|---|---|
| Product workflows | Account setup, staff activation, onboarding, connection setup, chat, simulation, decisions, approval, and operations traces pass browser tests |
| API and domain logic | 951-test Python suite passes; tenant/domain boundaries and failure paths are covered |
| Build | Ruff, TypeScript, Vite production build, package parity, and capability drift gates pass |
| Data integrity | Canonical 41-table schema, tenant RLS strategy, idempotency constraints, migration service, and repository contracts audited |
| Secrets and trust boundaries | No committed production secret found; credentials encrypted or one-way verified; unsafe production defaults fail closed |
| Failure handling | User-facing failures are bounded; failed inference and post-model agentic failures retain safe correlated receipts |
| Human control | High-risk decisions and all replay/write-back actions remain approval-gated |
| Recovery/operations | Release, rollback, retention, health-monitor, startup, and safe session-capsule paths exist and are tested |
| Observability | Existing events, journals, model runs, evidence, decisions, traces, and optional adaptive attribution remain correlated |

## Deployment acceptance still required

| Boundary | Why it is not a code-completion claim | Required evidence |
|---|---|---|
| Exact-head GitHub Actions | Current local changes must be pushed before CI can test their commit | Green CI and capability workflows for the pushed SHA |
| Live AMD/Fireworks inference | No current public endpoint credential is available | Direct `/v1/chat/completions` output plus live-required ShelfWise receipt |
| W7900D/MI300X training and serving | Accelerator access is external | Preflight, training/evaluation, adapter compatibility, and generated serving receipts |
| Existing production database upgrade | Local Postgres URL was not supplied | Backup, migration job, readiness check, rollback/restore drill |
| Retailer integration pilot | Needs real ERP/POS/WMS sandbox and data owner | Signed connector acceptance and reconciliation report |
| POPIA/business acceptance | Requires accountable operator and legal review | Retention/privacy approval, incident drill, and named owners |
| Capacity/SLO baseline | Depends on target hosting and traffic | Load/soak results against the selected production topology |

## Adaptive-attribution rollout

Keep `SHELFWISE_ADAPTIVE_ATTRIBUTION_ENABLED=false` for the first deployment. Enable it in a
controlled simulation tenant, observe reference-set warming and false-positive behavior, and
approve any replay manually. It introduces no schema migration and can be disabled without
changing the legacy trace response. Persistence of aggregate reference profiles should be
considered only after measured restart/volume need.

## Release decision

The repository has no known unmitigated critical correctness, tenant-isolation, secret-handling,
or data-integrity defect from this pass. It is suitable for a controlled deployment after the
exact-head CI gate. It is not yet honest to call the product fully production-accepted until the
live inference, database-upgrade, retailer, legal, rollback, and capacity evidence above exists.
