# Judge Guide

**Read this first if you only read one file.** Full detail lives in [`README.md`](README.md);
this page is the fast path — what ShelfWise is, why it's on AMD, and how to run it in minutes.

## What This Project Shows

ShelfWise is an agentic AI platform for supermarket operations. Critic/Executive agent pairs —
**google/gemma-4-E4B-it served on an AMD Instinct MI300X GPU via vLLM 0.23 on ROCm** — run a
bounded tool-calling loop over read-only decision-science tools (demand forecasting, expiry risk,
reorder policy, supplier ranking, multi-source stock sourcing, cold-chain risk, price integrity,
markdown simulation) to resolve a supermarket's daily decisions: expiry markdowns, procurement and
sourcing, till-price integrity, cold-chain response, recall quarantine, and inventory exceptions.

Every final answer is checked in code against the numbers its own tool calls actually returned
(`assert_conclusion_grounded_in_tool_results`); an answer citing a figure no tool produced is
rejected and re-run, never shipped. Every recommendation lands as a pending decision that a human
must approve before any write-back — full audit trail, tenant isolation, and a learning loop that
adjusts thresholds from real outcomes. The system runs against a persistent, continuously evolving
world simulation (a digital twin per store) rather than a single static scenario.

## Proof, Not Promises

A 15-minute continuous simulation drove the full public API against the **live** AMD MI300X
endpoint before this was ever demoed. Artifacts are committed under [`reports/`](reports/):

| Metric | Result |
|---|---:|
| Chat calls genuinely model-backed | 333 / 333 (zero offline fallbacks, zero errors) |
| Decisions created | 4,618 (zero ID collisions) |
| Human approve/reject cycles | 2,990 (zero mismatches) |
| Automated tests passing | 415 |
| Machine-verified capabilities | 165 (CI fails on drift) |

See [`reports/SUBMISSION_EVIDENCE_REPORT.md`](reports/SUBMISSION_EVIDENCE_REPORT.md) for the full,
honest breakdown of measured vs. configured behavior, and
[`reports/ORIGINAL_PROBLEM_COVERAGE.md`](reports/ORIGINAL_PROBLEM_COVERAGE.md) for which retailer
workflows are proven, partial, or roadmap.

## Run Locally

```powershell
$env:PYTHONPATH="src"
python -m pytest -q
python -m uvicorn shelfwise_backend.app:app --host 127.0.0.1 --port 8000
```

Frontend (separate shell):

```bash
cd frontend
npm install
npm run dev
```

Open:

```text
http://127.0.0.1:5173                              # console
http://localhost:8000/inference/smoke              # offline vs Fireworks vs MI300X/vLLM
http://localhost:8000/demo/golden/agentic           # live agentic cascade (expiry/markdown)
```

Set `LLM_BASE_URL` to any OpenAI-compatible vLLM endpoint serving `google/gemma-4-E4B-it` with
`--enable-auto-tool-choice --tool-call-parser gemma4` to exercise the live agent path; otherwise
the app runs against the Fireworks fallback or clearly reports offline mode via `/inference/smoke`.

## Run With Docker

```bash
docker compose -f docker-compose.production.yml up --build
```

Open:

```text
http://localhost:5173                               # console
http://localhost:8000/health
http://localhost:8000/submission/readiness           # Track 3 gate self-check
http://localhost:8000/demo/golden/agentic
http://localhost:8000/demo/procurement/agentic
http://localhost:8000/demo/cold-chain/agentic
http://localhost:8000/demo/worldgen-runs             # digital-twin world simulation runs
http://localhost:8000/products/attention
http://localhost:8000/mlops/observability
http://localhost:8000/connectors/systems
```

## What To Look For

- **Chat is the front door.** `POST /chat` runs the same tool registry and grounding guarantee as
  the automated cascades — ask it a real operational question and it will call live tools
  (stock, forecast, sourcing, approvals) and answer from what they returned, not from guesswork.
- **The grounding guarantee is enforced in code, not a prompt instruction** — see
  `assert_conclusion_grounded_in_tool_results` in `src/shelfwise_backend/`.
- **Nothing writes back without a human.** Every `/demo/*/agentic` cascade and every chat action
  that changes state produces a pending decision; approve/reject via the UI or the `/decisions`
  endpoints, and watch the learning loop move thresholds afterward (`/learning`).
- **The digital twin is real, not decorative.** `/demo/worldgen-runs` lists actual continuous
  world-simulation runs the agents were trained and stress-tested against — this is the substrate
  the 15-minute soak evidence above was measured on.
- **AMD compute is enforced, not claimed.** `/inference/smoke` reports whether a request is
  offline, Fireworks, or `vllm_mi300x`. In `APP_ENV=production`, any provider other than the AMD
  MI300X endpoint is rejected with HTTP 503 — there is no silent fallback path in production.
- **Governed exception workflows** — recall quarantine, returns, damage, shrink investigation,
  misplaced-stock relocation — each carry required evidence, a Critic review step, and HITL
  gating; runnable as generated-world drills via `/demo/worldgen/{scenario_id}`.
- **Multi-tenant by construction.** The authenticated tenant always overrides anything the model
  writes into tool arguments; verify via `/tenants/me` and the tenant-scoped `/decisions` list.

## If You Have Five More Minutes

- [`README.md`](README.md) — full architecture, tech stack, and getting-started detail.
- [`submission/`](submission/) — slide deck and cover image.
- [`reports/SUBMISSION_EVIDENCE_REPORT.md`](reports/SUBMISSION_EVIDENCE_REPORT.md) — the honest
  evidence ledger: what's measured on the live endpoint vs. what's configured but unmeasured.
