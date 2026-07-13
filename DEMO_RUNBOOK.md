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

## Three-Minute Story (as recorded)

1. Open the app; point at the top-right badge: `AMD vLLM  AMD Instinct MI300X · Live` -
   every answer comes from a live model call to the MI300X endpoint.
2. Ask chat one comprehensive question:
   `Give me a full report: approvals, stock, deliveries, cold chain, and where replacement stock should come from.`
   Narrate: behind the scenes it calls 4+ real tools in one turn (approvals, stock, delivery
   reconciliation, sourcing) and can only cite numbers a tool actually returned - the grounding
   check rejects anything else. The answer renders as structured markdown: headings, bullets,
   bolded figures.
3. Open the Products or To order workspace, use one visible generated-world SKU, then ask the sourcing question:
   `We are short on SKU <visible-sku>. Where should the replacement stock come from?`
   Narrate: it does not just say "transfer stock" - it ranks nearby branches, the regional DC,
   and suppliers by availability, distance, and lead time, names the winner and why, and
   recommends a purchase order for whatever the winner cannot cover.
4. Open the approval queue and approve one pending decision. Narrate: nothing writes back
   automatically - every recommendation waits for a human; full audit trail.
5. (Optional) Operations workspace -> "Gated operational endpoints" -> click any row ending
   "(agentic) - click to run live" to fire a real Critic/Executive Gemma cascade and watch the
   live result replace the row detail.
6. Close on the proof: a 15-minute live_required soak (receipts in `reports/`) finished
   333/333 chat calls model-backed with zero offline fallbacks and zero HITL mismatches.

## Cloud Proof (what the submission actually uses)

The submission runs exclusively on an AMD Developer Cloud MI300X droplet serving
`google/gemma-4-E4B-it` via vLLM 0.23 (ROCm) with native Gemma tool calling. The contract is
OpenAI-compatible, so any such endpoint works, but no other provider was used.

Droplet restart runbook (container and model weights persist across power cycles):

```bash
ssh root@<droplet-ip>
docker start rocm
docker exec rocm bash -c 'nohup vllm serve google/gemma-4-E4B-it --host 0.0.0.0 --port 8000   --enable-auto-tool-choice --tool-call-parser gemma4 > /tmp/vllm_serve.log 2>&1 &'
# wait for warmup, then: curl http://<droplet-ip>:8000/v1/models
```

Local backend against the live endpoint (`.env` holds the real values, gitignored):

```powershell
$env:LLM_BASE_URL="http://<droplet-ip>:8000"
$env:LLM_ROUTINE_MODEL="google/gemma-4-E4B-it"
$env:LLM_STRONG_MODEL="google/gemma-4-E4B-it"
Invoke-RestMethod http://127.0.0.1:8000/inference/config
Invoke-RestMethod http://127.0.0.1:8000/inference/smoke
```

Verify any chat answer is genuinely live: response headers carry
`x-shelfwise-provider: vllm_mi300x`, `x-shelfwise-model: google/gemma-4-E4B-it`, and
`x-shelfwise-answer-source: model`. Agentic demo endpoints default to `live_required` and
return 503 rather than fake an offline success.

## Generated-World Data Proof

The current live request path uses generated-world facts through `WorldFactsProvider`, not the
superseded fixture blend. With `SHELFWISE_STORE_BACKEND=postgres`, the first tenant request lazy-populates
`shelfwise_world_snapshot`; every product search, attention row, cascade, and sourcing decision
then reads that tenant snapshot instead of hardcoded SKU literals.

Optional local Postgres proof:

```powershell
$env:SHELFWISE_TEST_DATABASE_URL="postgresql://shelfwise_app:<password>@127.0.0.1:5433/shelfwise"
python -m pytest -q tests/test_postgres_world_integration.py
```

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
