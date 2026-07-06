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
```

## What To Look For

- The cascade emits Inventory, Demand, Expiry, Opportunity, Simulation, Critic, and Executive evidence.
- The UI leads with one recommended action, then reveals the agent chain only when requested.
- The side rail shows numeric proof: FEFO sell-first units, normal units, delivery gap, supplier
  action, and outcome-learning signal.
- The golden cascade reads the planted store scenario from `data/datasets/*.csv`, exposed through
  `/data/seed/summary`.
- Every recommendation includes sources and supporting data.
- The action is high-risk and remains `pending` for human approval.
- The action can be approved or rejected through the UI or HITL endpoints.
- Approval records a mocked write-back receipt, measured outcome, and visible threshold-adjustment
  learning event.
- Trace spans show the deterministic decision-science tools behind the recommendation.
- `/demo/golden` exposes `store_intelligence` for batch split, delivery reconciliation, supplier
  cover, and learning summary.
- `/intelligence/*` endpoints let those same store skills run against new request payloads.
- `/inference/smoke` shows whether the app is offline, using Fireworks, or using MI300X/vLLM.
