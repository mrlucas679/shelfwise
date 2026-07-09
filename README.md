# ShelfWise

AMD Developer Hackathon: ACT II project.

ShelfWise is an evidence-first operations brain for FMCG retail. The first demo slice
runs a seeded South African supply-chain scenario:

`scan -> inventory -> expiry risk -> demand -> opportunity -> simulation -> critic -> executive -> HITL`

The current implementation is a runnable MVP slice: a chat-first operations console backed by
deterministic store-intelligence tools and human approval.

## Quick Start

```powershell
python -m pip install -e ".[dev]"
$env:PYTHONPATH="src"
python -m pytest -q
python -m uvicorn shelfwise_backend.app:app --host 127.0.0.1 --port 8000
```

In another terminal:

```powershell
cd frontend
npm install
npm run dev -- --host 127.0.0.1 --port 5173
```

Then open the app:

- `GET http://localhost:8000/submission/readiness`
- `http://127.0.0.1:5173`

## Test everything in one notebook (GPU / remote Jupyter)

[`notebooks/01_shelfwise_full_test_harness.ipynb`](notebooks/01_shelfwise_full_test_harness.ipynb)
is a self-contained test harness — clone the repo, open the notebook, **Run All**, done. No
extra setup, no data to add: the seed CSVs, dependency lists, and full `src/` tree are all
already in this repo. It installs the project, runs lint, the full test suite, the golden-
scenario eval gate, an in-process API smoke test, and a real `uvicorn` server smoke test on an
actual port — and ends with one summary table so a failure anywhere is impossible to miss. An
optional last section exercises a real inference call through an AMD MI300X/vLLM (or Fireworks)
endpoint if `LLM_BASE_URL`/`LLM_API_KEY` are set in the environment first; everything else runs
fully offline/deterministic.

Connected API endpoints:

- `GET http://localhost:8000/catalog/products/{product_id}`
- `GET http://localhost:8000/catalog/resolve`
- `GET http://localhost:8000/cold-chain/feed`
- `GET http://localhost:8000/connectors/inbound-records`
- `GET http://localhost:8000/connectors/me`
- `GET http://localhost:8000/connectors/systems`
- `GET http://localhost:8000/data/seed/summary`
- `GET http://localhost:8000/decisions/{decision_id}`
- `GET http://localhost:8000/decisions`
- `GET http://localhost:8000/demo/critic-rejection`
- `GET http://localhost:8000/demo/worldgen-runs/{run_id}`
- `GET http://localhost:8000/demo/worldgen-runs`
- `GET http://localhost:8000/demo/worldgen/{scenario_id}`
- `GET http://localhost:8000/detective/root-cause-sql`
- `GET http://localhost:8000/detective/root-cause/{target_id}`
- `GET http://localhost:8000/events/bus`
- `GET http://localhost:8000/events`
- `GET http://localhost:8000/health`
- `GET http://localhost:8000/inference/config`
- `GET http://localhost:8000/inference/readiness`
- `GET http://localhost:8000/inference/smoke`
- `GET http://localhost:8000/learning`
- `GET http://localhost:8000/mlops/accountability`
- `GET http://localhost:8000/mlops/model-runs`
- `GET http://localhost:8000/mlops/observability`
- `GET http://localhost:8000/mlops/prompts`
- `GET http://localhost:8000/mlops/tenant-facts`
- `GET http://localhost:8000/products/attention`
- `GET http://localhost:8000/products/search`
- `GET http://localhost:8000/readiness`
- `GET http://localhost:8000/submission/readiness`
- `GET http://localhost:8000/tools/platform/audit`
- `GET http://localhost:8000/tools/platform`
- `GET http://localhost:8000/trace/{correlation_id}`
- `GET http://localhost:8000/traces`
- `GET http://localhost:8000/worker/runs`
- `GET http://localhost:8000/worker/status`
- `GET http://localhost:8000/writeback/tasks`
- `GET/POST http://localhost:8000/catalog/products/{product_id}/variants`
- `GET/POST http://localhost:8000/catalog/products`
- `GET/POST http://localhost:8000/demo/cold-chain`
- `GET/POST http://localhost:8000/demo/golden`
- `GET/POST http://localhost:8000/demo/procurement`
- `GET/POST http://localhost:8000/demo/sales`
- `GET/POST http://localhost:8000/tenants/me`
- `POST http://localhost:8000/catalog/identifiers`
- `POST http://localhost:8000/chat`
- `POST http://localhost:8000/connectors/{system}/intake`
- `POST http://localhost:8000/decisions/{decision_id}/approve`
- `POST http://localhost:8000/decisions/{decision_id}/reject`
- `POST http://localhost:8000/ingest`
- `POST http://localhost:8000/intelligence/deliveries/reconcile`
- `POST http://localhost:8000/intelligence/outcomes/summarize`
- `POST http://localhost:8000/intelligence/stock/fefo-split`
- `POST http://localhost:8000/intelligence/suppliers/cover-plan`
- `POST http://localhost:8000/mlops/consolidate-memory`
- `POST http://localhost:8000/scan/barcode`
- `POST http://localhost:8000/scan/image`
- `POST http://localhost:8000/scan/receipt`
- `POST http://localhost:8000/voice/in`
- `POST http://localhost:8000/voice/out`
- `POST http://localhost:8000/worker/process-one`

## Smoke

```powershell
./scripts/smoke.ps1
```

## Demo

Use [DEMO_RUNBOOK.md](DEMO_RUNBOOK.md) for the local demo flow, judge story, and cloud proof checks.

## Container

```bash
docker compose up --build
```

## Current Scope

Built now:

- Money/source/evidence/decision contracts.
- Deterministic decision-science tools.
- Store-intelligence tools for FEFO batch splits, delivery reconciliation, supplier cover, and
  outcome learning, exposed as executable API endpoints.
- CSV-backed SA retail seed data under `data/datasets`, with validation and a loaded golden
  scenario consumed by the cascade.
- Golden cascade runner.
- Visible Critic rejection cascade that downgrades an unsupported supplier-switch claim to monitor.
- Procurement, sales, and cold-chain cascades with math-backed evidence and HITL policy.
- FastAPI health and demo endpoints.
- Event ingest, event log, trace log, detective root-cause, and worker processing endpoints.
- Product attention and search-first catalogue endpoints that keep the sidebar bounded, use a
  bounded synthetic scan budget for demo catalogue search, and push product/lot exploration into
  the workspace.
- HITL approve/reject endpoints.
- Memory and Postgres store backends with tenant-scoped RLS schema for business tables.
- Learning store that records approved outcomes, task-style write-back receipts, visible threshold
  adjustments, and governed tenant facts.
- Connector provenance layer with quarantine, per-system mappers, inbound record persistence, and
  read-only/pending-write posture.
- MLOps run/prompt registries, accountability reporting, observability snapshot, eval gate, and
  dormant fine-tune export path.
- Worker journal, plan validation, schedule overlap protection, and queue-backed cascade processing.
- Synthetic/worldgen and cold-chain resilience backends.
- Voice and scan backend routes with review-required candidates and upload sniffing.
- Security gateway for prompt fencing, rate limiting, API-key/JWT role gates, and app-level request
  body limits.
- Offline-safe OpenAI-compatible inference gateway for Fireworks/vLLM.
- React/Vite chat-first ops console with bounded attention sidebar, product/workflow workspaces,
  selectable product cards with FEFO lot drill-down, one executive answer, numeric proof rail,
  compact agent chain, drill-down evidence, decision log, inference routing, learning note, and
  HITL approval.
- Tests for contracts, cascades, stores, connectors, MLOps, worldgen, multimodal, and security paths.
- Backend and frontend Dockerfiles plus Compose services.
- GitHub Actions CI for backend lint/tests, ShelfWise eval, backend smoke, frontend build, and Compose
  validation.

Next:

- Live provider credential test against Fireworks and AMD Developer Cloud MI300X/vLLM.
- Docker build/run verification after Docker Desktop starts.
- Demo recording and public URL.

## Inference Strategy

ShelfWise keeps one OpenAI-compatible inference contract and uses both AMD program benefits:

- **Fireworks AI:** fastest managed endpoint for development and public-demo reliability.
- **AMD Developer Cloud:** direct MI300X/ROCm/vLLM endpoint for the "built on AMD" proof and benchmark.

Routine agents can use a smaller model. Critic, Executive, and Orchestrator are routed to the stronger
model tier because they review evidence, catch contradictions, and make the final recommendation.

## License

MIT
