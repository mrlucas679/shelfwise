# ShelfWise

AMD Developer Hackathon: ACT II project.

ShelfWise is an evidence-first operations brain for FMCG retail. The first demo slice
runs a seeded South African supply-chain scenario:

`scan -> inventory -> expiry risk -> demand -> opportunity -> simulation -> critic -> executive -> HITL`

The current implementation is the first runnable scaffold extracted from the project domain specs.

## Quick Start

```powershell
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

- `http://127.0.0.1:5173`

Useful API endpoints:

- `GET http://localhost:8000/health`
- `GET http://localhost:8000/readiness`
- `GET http://localhost:8000/demo/golden`
- `GET http://localhost:8000/inference/smoke`
- `POST http://localhost:8000/decisions/{decision_id}/approve`
- `POST http://localhost:8000/decisions/{decision_id}/reject`

## Smoke

```powershell
./scripts/smoke.ps1
```

## Container

```bash
docker compose up --build
```

## Current Scope

Built now:

- Money/source/evidence/decision contracts.
- Deterministic decision-science tools.
- Golden cascade runner.
- FastAPI health and demo endpoints.
- HITL approve/reject endpoints.
- Offline-safe OpenAI-compatible inference gateway for Fireworks/vLLM.
- React/Vite ops console with evidence, trace, inference routing, learning note, and HITL approval.
- Tests for the golden cascade.
- Backend and frontend Dockerfiles plus Compose services.

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
