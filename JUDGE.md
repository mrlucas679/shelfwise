# Judge Guide

## What This Project Shows

ShelfWise demonstrates a math-backed, agent-reviewed retail operations cascade. A seeded
load-shedding and payday scenario produces evidence-backed recommendations for reducing food waste,
while a store-intelligence slice handles batch age, delivery mismatch, supplier cover, and learning.

## Run Locally

```powershell
$env:PYTHONPATH="src"
python -m pytest -q
python -m uvicorn shelfwise_backend.app:app --host 127.0.0.1 --port 8000
```

Open:

```text
http://127.0.0.1:5173
http://localhost:8000/demo/golden
http://localhost:8000/demo/critic-rejection
```

## Run With Docker

```bash
docker compose up --build
```

Open:

```text
http://localhost:5173
http://localhost:8000/health
http://localhost:8000/readiness
http://localhost:8000/demo/golden
http://localhost:8000/demo/critic-rejection
http://localhost:8000/demo/procurement
http://localhost:8000/demo/sales
http://localhost:8000/demo/cold-chain
http://localhost:8000/products/attention
http://localhost:8000/products/search?q=amasi
http://localhost:8000/mlops/observability
http://localhost:8000/connectors/systems
```

## What To Look For

- The cascade emits Inventory, Demand, Expiry, Opportunity, Simulation, Critic, and Executive evidence.
- The UI leads with one recommended action, then reveals the agent chain only when requested.
- The header switch shows both the approval case and a Critic rejection case without crowding the
  main conversation.
- The side rail includes a decision log row so the audit trail is visible during the demo.
- The side rail shows numeric proof: FEFO sell-first units, normal units, delivery gap, supplier
  action, and outcome-learning signal.
- Product-scale lookup stays search-first in the workspace; the sidebar only shows bounded attention
  groups such as `Sell first`, while selected product cards reveal FEFO lot rotation one level
  deeper.
- The golden cascade reads the planted store scenario from `data/datasets/*.csv`, exposed through
  `/data/seed/summary`.
- Every recommendation includes sources and supporting data.
- The action is high-risk and remains `pending` for human approval.
- The action can be approved or rejected through the UI or HITL endpoints.
- The Critic rejection scenario blocks an unsupported supplier-switch claim and downgrades it to
  monitor until sourced backup-supplier evidence exists.
- Approval records a HITL-gated write-back task, measured outcome, and visible
  threshold-adjustment learning event.
- Approved actions create HITL-gated pending write-back tasks; no source system mutation is exposed.
- Trace spans show the deterministic decision-science tools behind the recommendation.
- `/mlops/observability` shows tenant decision, inference, connector, event, write-back, worker, and
  learning metrics.
- `/connectors/systems` and `/connectors/{system}/intake` show the provenance-backed connector layer
  for SAP, Odoo, SYSPRO, Shopify, Square, and Lightspeed-style payloads.
- The API has prompt-injection fencing, write-path rate limiting, JWT/API-key gates, upload sniffing,
  formula neutralization, and a 6 MB default app-level request body cap.
- `/demo/golden` exposes `store_intelligence` for batch split, delivery reconciliation, supplier
  cover, and learning summary.
- `/intelligence/*` endpoints let those same store skills run against new request payloads.
- `/inference/smoke` shows whether the app is offline, using Fireworks, or using MI300X/vLLM.
