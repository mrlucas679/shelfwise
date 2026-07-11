# HANDOFF — session state as of 2026-07-11 ~01:40 (local)

## Critical correction — verified after the original handoff

The v2 run finished with an original `FULL_CAPACITY_V2_PASSED` marker, but current
revalidation correctly marks it **failed**: only 2 of 51 chats were model-backed and 49
silently used the offline fallback. The immutable correction receipt is
`reports/full_capacity_v2_revalidation.json`. The harness now requires every chat in a
`live_required` run to be model-backed, rejects offline answers and chat errors, and can
revalidate historical runs with `scripts/validate_full_system_artifact.py`.

Root cause: HITL resolution happened after the 15-minute rotation while `/chat` injected every
pending decision into its prompt. The store remains complete, but chat context now carries
aggregate counts plus bounded pending/resolved windows. `live_required` chat returns 503 instead
of falling back.

Infrastructure correction: the `rocm` container was found `Exited (137)` and the endpoint refused
connections. A container restart was issued, after which ports 22 and 8000 stopped responding.
Treat the droplet as **unreachable** until the cloud dashboard and `/v1/models` prove otherwise.

**Deadline: TODAY 2026-07-11, 6pm CET. Submission requires: repo + demo video + slide deck,
and the work MUST be merged to `main` before submitting. Do not leave the merge to the last hour.**

## Where we are

Branch: `codex/gemma-full-system-integration` (all work committed, working tree clean except
untracked run-artifact dirs: `20260710T*/`, `reports/`, `shelfwise-gemma-final-adapter/`,
`stress_run_*/`, `data/harness_runs/`, `full_capacity_v2.log`).

Latest commits (newest first):
- `5b30d15` tenant-isolation fix + full 11/11 tool coverage
- `561c50b` bound /chat state (unbounded-prompt scaling bug)
- `c615399` drop strict json_schema decoding (vLLM/Gemma whitespace-loop bug) + 11-role harness
- `afb7c8c` force tool_choice=required on opening agent call
- `c7fbdaf` wire golden cascade Critic/Executive through real Gemma tool calling
- earlier: merge of gpu-notebook-testing, docker-compose env_file fix

389/389 tests pass. `python -m pytest -q`, `python -m ruff check .`, and the production frontend
build pass. Capability manifest is in sync (`python scripts/compare_capability_manifests.py --write`
regenerates it after any route/tool/test change — the contract test fails when stale).

## Live infrastructure (BILLING: $1.99/hr — shut down when done!)

AMD Developer Cloud MI300X droplet: `165.245.130.225` (SSH worked earlier from this machine,
key `~/.ssh/id_ed25519`). Inside it, Docker container `rocm` previously ran vLLM 0.23.0 (ROCm)
serving `google/gemma-4-E4B-it` on port 8000 with the Gemma tool parser. `.env` points at that
endpoint, but configuration/readiness metadata is not proof that the process is currently live.

**Restart runbook if the droplet/pod restarts:**
```
ssh root@165.245.130.225
docker start rocm
docker exec rocm bash -c 'nohup vllm serve google/gemma-4-E4B-it --host 0.0.0.0 --port 8000 \
  --enable-auto-tool-choice --tool-call-parser gemma4 > /tmp/vllm_serve.log 2>&1 &'
# wait ~7 min (torch.compile) then: curl http://165.245.130.225:8000/v1/models
```
HF auth is already done inside the container (user keorapetswe; Gemma license accepted).
Model weights are cached in the container (~15GB). The Jupyter hackathon notebook portal
(notebooks.amd.com) is DOWN for maintenance — W7900 training shakedown blocked on that.

## What is VERIFIED live (with receipts in commits/artifacts)

- `/inference/smoke`, `/chat`, `POST /demo/golden/agentic` all hit real MI300X Gemma.
- 11/11 agent roles + 11/11 platform tools genuinely exercised by real Gemma tool calls:
  `python -c` runner in `src/shelfwise_eval/agent_role_coverage.py` (needs `.env` sourced).
- Full-system world sim 15-min run v1 PASSED (145 cycles, 3152 decisions, unique IDs, zero
  HITL mismatches) but exposed the /chat unbounded-state bug (2/49 model answers) — FIXED.
- V2 completed 151 cycles and preserved decision/HITL/learning integrity, but its live-chat
  requirement failed under current rules: 2 model answers and 49 offline fallbacks. Its old pass
  marker is superseded by `reports/full_capacity_v2_revalidation.json`.

## 8 real bugs found by live testing this session (all fixed + regression-tested)

1. docker-compose loaded `.env.example` (blank creds) instead of `.env` → silent offline mode.
2. `tool_choice="auto"` → Gemma skipped tools entirely, then emitted degenerate output.
3. Raw `InferenceError` leaked through the orchestrator instead of typed failure.
4. Strict `json_schema` response_format → infinite whitespace loop on vLLM/Gemma-4 (NOT a
   token-budget issue; proven with max_tokens 800 vs 4000). Now: text mode + schema-in-prompt
   + post-hoc validation. NEVER re-enable strict json_schema against this endpoint.
5. `FinalAnswerValidationError` not caught → batch crash instead of per-role failure.
6. `/chat` sent unbounded decision/learning history → prompt growth → timeout → silent
   offline fallback after ~cycle 6 of a long run. Context is now bounded without deleting state.
7. **Tenant-isolation hole**: Gemma invented `tenant_id="default_tenant"` in tool args and
   the tool honored it. Now `trusted_overrides` in `PlatformToolRegistry.execute` forces the
   caller-authenticated tenant over any model-supplied value.
8. The v2 harness accepted one model answer as sufficient for a `live_required` run, allowing
   49 offline fallbacks to pass. It now requires model answers to equal chat calls and supports
   historical artifact revalidation.

## NEXT STEPS, in priority order (the plan we were executing)

1. Restore and prove the droplet from the cloud dashboard, then verify `/v1/models` and a
   `live_required` `/chat` response. Re-run a bounded soak before claiming capacity success.
2. **Frontend end-to-end against the live backend** — never verified this session (only
   `npx tsc --noEmit`). The demo IS the frontend; this is the biggest remaining demo risk.
   Backend: `uvicorn shelfwise_backend.app:app` with `.env` loaded; frontend: `npm run dev`
   in `frontend/`. Verify: runtime-config/inference badge shows live MI300X, chat answers
   come from the model, golden + agentic flows and HITL approve/reject work in the UI.
3. **Record the demo video while the droplet is hot.**
4. **Merge this branch to `main`** (required for submission).
5. Only if time remains: wire procurement cascade through the agentic path (pattern is in
   `src/shelfwise_backend/agentic_cascade.py`); run `shelfwise_benchmark` at 1/8/32
   concurrency against the live endpoint for the report.

## Known honest gaps (do not overclaim in the deck/video)

- 4 of 5 production cascades (procurement/sales/cold-chain/price+expiry checks) remain
  deterministic math + hand-authored evidence; only the golden cascade's Critic/Executive
  run through real Gemma tool calling (`POST /demo/golden/agentic`, `live_required` default).
  The 11-role coverage harness proves the mechanism for every role in eval mode.
- Training matrix: E2B/12B W7900 shakedown blocked (Jupyter portal down). Only E4B is live.
- Benchmark architecture comparison (shared/replicated/per-agent/hybrid) is built + tested
  offline but has no real cloud measurements yet.
- MI300X operator-side AMD-SMI telemetry: not collected (provider gives no host access);
  report as missing evidence, never estimated. vLLM /metrics IS available on the droplet.

## House rules (unchanged, binding)

No AI attribution anywhere (commits/PRs). Free-tier/open-source only. Cloud inference only
(MI300X/vLLM + Fireworks fallback) — never local models. MIT-clean deps. No temporary fixes.
Read `CLAUDE.md` for the full mandate (full MVP, not a demo slice).
