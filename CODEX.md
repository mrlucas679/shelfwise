# ShelfWise Implementation Guide

> **Working-product branch boundary:** This guide describes the post-hackathon working product
> on `developers`. Keep all implementation commits on `developers`; `main` is the protected
> working-product branch and must not receive these changes accidentally.

This repository contains the running ShelfWise application, not only a blueprint. The executable
system is the Python package family under `src/shelfwise_*`, the FastAPI service in
`src/shelfwise_backend`, and the React/Vite console under `frontend/`. Files under `plot/` describe
intent and build procedures; they are useful design input but are not evidence that a feature runs.

## Product boundary

ShelfWise turns retail source data into a bounded, evidence-first operating workflow:

`source data -> canonical state -> candidate generation -> math/tool evidence -> Critic -> Executive -> HITL/task -> learning`

The current role split is Inventory, Demand, Expiry, Opportunity, Simulation, Procurement, Sales,
Cold Chain, Critic, Executive, and Orchestrator (11 agent capability IDs, verified against the
committed capability manifest). Do not add agents to solve a missing data, policy, or
deterministic-tool problem.
Keep routine work cheap and reserve stronger inference for evidence review, contradiction handling,
and high-risk decisions.

## Implementation rules

- Extend the existing workflow and store interfaces before introducing a parallel path.
- Treat every external payload, model response, connector record, and uploaded file as untrusted.
- Keep tenant identity derived from authenticated context; never trust caller-supplied tenant filters.
- Keep high-risk actions pending until an authenticated human approves them.
- Keep model output subordinate to deterministic tools, source references, and Critic rules.
- Preserve idempotency, decision identity, provenance, audit records, and failure receipts.
- Never expose raw exceptions, credentials, stack traces, or opaque tokens to users or reports.
- Do not add paid services or dependencies without an explicit decision and license check.
- Do not redesign the frontend. Add only the smallest UI contract or correctness change needed to
  expose a backend capability, and preserve existing loading, empty, error, and accessibility states.

## Build order for unfinished work

1. Tenant-safe durable state and connector correctness.
2. Product, variant, batch/lot, inventory-position, supplier, and open-order substrate.
3. Deterministic candidate generation, deduplication, suppression, and exception ranking.
4. Context assembly and compact evidence receipts for agent calls.
5. Worker batching, retry/dead-letter/reclaim scheduling, queue lag, and capacity receipts.
6. Usefulness/noise evaluation across many generated stores and scenarios.

The golden cascade is a regression fixture, not the product's scale ceiling. New capabilities must
work for many tenants, stores, products, and events through the same provider interfaces.

## Verification gates

From the repository root, run:

```text
python -m ruff check src tests scripts
python -m pytest -q
python scripts/compare_capability_manifests.py
cd frontend && npm run typecheck && npm run build
```

If a route or public capability changes, update the frontend endpoint registry and README contract,
then regenerate `capabilities/manifest.json`. For deployment work, validate Compose with ephemeral
test variables and run `scripts/deployment_shakedown.py` through the public origin. GPU inference
is required only for live-required model proof, never for ordinary CI.

## Evidence language

Use precise labels: `implemented`, `locally verified`, `deployed-topology verified`, and
`live-model verified` are different claims. A blueprint, unit test, in-process harness receipt, and
public deployment receipt must not be presented as interchangeable proof.
