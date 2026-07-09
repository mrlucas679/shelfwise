# ShelfWise

AMD Developer Hackathon: ACT II project.

ShelfWise is an evidence-first operations brain for FMCG retail. The first demo slice
runs a seeded South African supply-chain scenario:

`scan -> inventory -> expiry risk -> demand -> opportunity -> simulation -> critic -> executive -> HITL`

The current implementation is a runnable MVP slice: a chat-first operations console backed by
deterministic store-intelligence tools and human approval.

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
- `GET http://localhost:8000/demo/critic-rejection`
- `GET http://localhost:8000/data/seed/summary`
- `GET http://localhost:8000/learning`
- `GET http://localhost:8000/inference/readiness`
- `POST http://localhost:8000/intelligence/stock/fefo-split`
- `POST http://localhost:8000/intelligence/deliveries/reconcile`
- `POST http://localhost:8000/intelligence/suppliers/cover-plan`
- `POST http://localhost:8000/intelligence/outcomes/summarize`
- `GET http://localhost:8000/inference/smoke`
- `GET http://localhost:8000/submission/readiness`
- `POST http://localhost:8000/decisions/{decision_id}/approve`
- `POST http://localhost:8000/decisions/{decision_id}/reject`

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
- Product attention and search endpoints that keep the sidebar bounded while allowing product and
  lot drill-down in the app.
- Runnable local eval harness via `python -m shelfwise_eval`.
- CSV-backed SA retail seed data under `data/datasets`, with validation and a loaded golden
  scenario consumed by the cascade.
- Golden cascade runner.
- Visible Critic rejection cascade that downgrades an unsupported supplier-switch claim to monitor.
- FastAPI health and demo endpoints.
- HITL approve/reject endpoints.
- In-memory learning store that records approved outcomes, mocked write-back receipts, and visible
  threshold adjustments.
- Offline-safe OpenAI-compatible inference gateway for Fireworks/vLLM.
- React/Vite ops console with one executive answer, numeric proof rail, compact agent chain,
  drill-down evidence, scenario switch, decision log, inference routing, learning note, and HITL approval.
- Tests for the golden cascade and store-intelligence skills.
- Tests for product-scale catalogue behavior, frontend product wiring, eval readiness, and smoke
  training JSONL shape.
- Backend and frontend Dockerfiles plus Compose services.
- GitHub Actions CI for lint, backend tests, frontend build, and Compose validation.

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

### AMD Developer Cloud / vLLM preflight

Before creating the MI300X droplet, the app is ready to accept an OpenAI-compatible vLLM endpoint.
When the droplet exists, configure the backend with:

```powershell
$env:LLM_BASE_URL="http://<mi300x-public-ip>:8000"
$env:LLM_API_KEY="demo-key"
$env:LLM_MODEL="shelfwise-demo"
$env:LLM_ROUTINE_MODEL="shelfwise-demo"
$env:LLM_STRONG_MODEL="shelfwise-demo"
$env:LLM_TIMEOUT_SECONDS="25"
$env:LLM_COMPUTE_RESOURCE="AMD Developer Cloud"
$env:LLM_ACCELERATOR="AMD Instinct MI300X"
```

Use these proof endpoints before recording or submitting:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/inference/readiness
Invoke-RestMethod http://127.0.0.1:8000/inference/smoke
Invoke-RestMethod http://127.0.0.1:8000/submission/readiness
```

For a hosted frontend, set `frontend/public/shelfwise-config.js` or build with `VITE_API_BASE`
so the browser calls the public backend URL instead of localhost.

## Gemma 4 Multimodal Training Harness

The repo now has a scriptable harness for the Gemma 4 LoRA path that was previously proven mostly
inside notebooks. It keeps `patch_dense` and `embedding_projection` in the LoRA targets so the run
does not silently collapse to text-only adaptation. Audio and video are supported through honest
fallbacks when native processor tensors are unavailable: audio uses transcripts, and video uses
sampled frame metadata.

Install training dependencies on the ROCm notebook host:

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[training]"
pip install torch --index-url https://download.pytorch.org/whl/rocm7.2
```

After pulling the update on the Jupyter GPU server, run the connected smoke path:

```bash
git pull
bash scripts/jupyter_gemma4_bootstrap.sh
```

That script installs the package, runs the harness tests, runs full GPU preflight, then starts a
gated full shakedown. Override the run name without editing files:

```bash
RUN_NAME=shelfwise-mm-full-8h-002 bash scripts/jupyter_gemma4_bootstrap.sh
```

PowerShell local command prefix when the package is not installed in editable mode:

```powershell
$env:PYTHONPATH="src"
```

Preflight only:

```bash
python -m shelfwise.training.preflight --config configs/train_gemma4_multimodal.yaml
```

Smoke train:

```bash
python -m shelfwise.training.train --config configs/train_gemma4_multimodal.yaml --max_steps 20 --run_name smoke-mm
```

Tomorrow 8-hour shakedown:

```bash
python -m shelfwise.training.train --config configs/train_gemma4_multimodal.yaml --run_name gemma4-mm-8h-001
```

Resume by setting `resume_from_checkpoint` in `configs/train_gemma4_multimodal.yaml` to the
checkpoint path under `runs/gemma4-multimodal/<run>/checkpoints/`.

Eval:

```bash
python -m shelfwise.training.evaluate --config configs/train_gemma4_multimodal.yaml --dry-run
```

Adapter export:

```bash
tar -czf /workspace/shelfwise-gemma-final-adapter.tar.gz -C /workspace/checkpoints/shelfwise-gemma final_adapter
```

Serving/plugin check:

```bash
python -m shelfwise.training.serving_check --config configs/train_gemma4_multimodal.yaml --adapter-path shelfwise-gemma-final-adapter/final_adapter --skip-model-load
```

Troubleshooting:

- Missing target modules: preflight fails if `patch_dense` or `embedding_projection` are absent
  unless `allow_missing_multimodal_targets` is explicitly set.
- Processor load failure: update `transformers`; Gemma 4 uses `Gemma4UnifiedProcessor`.
- Token mismatch: serving check validates the adapter tokenizer metadata and special tokens.
- ROCm OOM: keep `max_seq_length: 2048`, batch size `1`, gradient checkpointing on.
- NaN loss: training stops when configured to fail on non-finite loss.
- Missing evidence file: strict dataset mode fails with the exact row and path.
- vLLM adapter load failure: do not claim full serving support until the adapter loads with the
  deployed vLLM/transformers stack.

## Gemma 4 Multimodal Full Shakedown

Use this when the Jupyter GPU server is ready and you want the whole ShelfWise application AI path in
one gated run:

```bash
python -m shelfwise.training.shakedown --config configs/train_gemma4_multimodal.yaml --run_name shelfwise-mm-full-8h-001
```

The command runs:

`preflight -> simulation dataset build -> smoke train -> full train -> eval -> serving check -> final report`

Generate the simulation dataset only through the dry-run path:

```bash
python -m shelfwise.training.shakedown --config configs/train_gemma4_multimodal.yaml --run_name dataset-check --dry-run
```

The simulation builder emits canonical multimodal episodes across supply-chain reasoning,
multimodal evidence interpretation, incident simulation, report/action planning, and structured
tool-call behavior. It covers damaged goods, missing stock, supplier delays, fake POD, warehouse
voice transcripts, screenshots, proof-of-delivery mismatches, product quality failures, inventory
reconciliation, high-risk supplier patterns, safe cases, and ambiguous missing-evidence cases.

Resume from a checkpoint by setting `resume_from_checkpoint` in
`configs/train_gemma4_multimodal.yaml`, then rerun the shakedown command with a new `--run_name`.

## License

MIT
