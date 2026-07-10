# ShelfWise Demo Runbook

## Preflight

```powershell
$env:PYTHONPATH="src"
python -m ruff check src tests scripts
python -m pytest -q
cd frontend
npm run build
cd ..
docker compose config --quiet
./scripts/smoke.ps1
```

## Local Demo

Terminal 1:

```powershell
$env:PYTHONPATH="src"
python -m uvicorn shelfwise_backend.app:app --host 127.0.0.1 --port 8000
```

Terminal 2:

```powershell
cd frontend
npm run dev -- --host 127.0.0.1 --port 5173
```

Open `http://127.0.0.1:5173`.

## Three-Minute Story

1. Start on **Approval case**.
2. Show the single executive recommendation: `Apply Markdown 20%`.
3. Open **Show reasoning** to reveal the compact agent chain and one step detail.
4. Approve the action. Point to the HITL-gated write-back task, threshold-learning message, and
   decision log.
5. Switch to **Critic rejection**.
6. Show that the action is downgraded to `Monitor`, approval buttons are disabled, and the
   decision log records `Rejected`.
7. Open reasoning, select the Critic step, and show `critic_passed=False` plus the missing
   backup-supplier source requirement.
8. Point to inference routing: routine agents use the small tier; Critic, Executive, and
   Orchestrator use the strong tier.
9. Open the Products workspace and search `amasi`; show that product-scale lookup stays out of the
   sidebar and returns the attention-ranked Amasi 2L record.

## Cloud Proof

Use the same OpenAI-compatible contract for Fireworks or AMD Developer Cloud/vLLM:

```powershell
$env:LLM_BASE_URL="https://your-openai-compatible-base-url"
$env:LLM_API_KEY="your-key"
$env:LLM_ROUTINE_MODEL="your-routine-model"
$env:LLM_STRONG_MODEL="your-strong-model"
Invoke-RestMethod http://127.0.0.1:8000/inference/config
Invoke-RestMethod http://127.0.0.1:8000/inference/smoke
```

For the hackathon recording, use Fireworks for reliability and AMD Developer Cloud/vLLM for the
MI300X proof when credits are active.

## GPU Pod Access (notebooks.amd.com/hackathon)

- Access is per-**team**, not per-participant. Even solo builders must create/join a team on the
  lablab.ai event page (`https://lablab.ai/ai-hackathons/amd-developer-hackathon-act-ii` → Teams tab)
  or `notebooks.amd.com/hackathon` returns a "team not registered" error.
- After team registration, allow up to 24 hours for the GPU pod to be allocated.
- Pod usage budget is **8 hours per rolling 24 hours** per team (raised from the original 4-hour
  limit) — plan longer benchmark/fine-tune runs around this window and stop idle notebooks promptly
  so the quota resets sooner.
- Once the notebook exposes its OpenAI-compatible vLLM base URL and key, set `LLM_BASE_URL` /
  `LLM_API_KEY` (see `.env`) to that endpoint for the direct MI300X proof above.
