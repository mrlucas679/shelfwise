# Judge Guide

## What This Project Shows

ShelfWise demonstrates a math-backed, agent-reviewed retail operations cascade. A seeded
load-shedding and payday scenario produces evidence-backed recommendations for reducing food waste.

## Run Locally

```powershell
$env:PYTHONPATH="src"
python -m pytest -q
python -m shelfwise_backend
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
- Every recommendation includes sources and supporting data.
- The action is high-risk and remains `pending` for human approval.
- The action can be approved or rejected through the UI or HITL endpoints.
- Trace spans show the deterministic decision-science tools behind the recommendation.
- `/inference/smoke` shows whether the app is offline, using Fireworks, or using MI300X/vLLM.
