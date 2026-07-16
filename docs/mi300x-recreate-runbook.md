# MI300X Recreate Runbook

This is the repeatable ShelfWise inference and pressure-test path for an AMD Developer
Cloud MI300X x1 Quick Start droplet. It intentionally contains no credential values.

## Known-Good Runtime

- Droplet image: AMD Quick Start `vLLM`, ROCm 7.2.4, Ubuntu 24.04.
- Quick Start container: `rocm`.
- vLLM version observed: `0.23.0+rocm723`.
- Routine tier: `google/gemma-4-E4B-it` on port `8000`.
- Strong tier: `google/gemma-4-31B-it` on port `8001`.
- Serving budget: 20% routine plus 55% strong GPU memory utilization.
- Bootstrap commit baseline: use the current `developers` branch, never a copied script.

## Provision

1. Create an MI300X x1 droplet with the AMD vLLM Quick Start image and add the existing
   SSH public key.
2. Connect as root and install the repository:

```bash
git clone https://github.com/mrlucas679/shelfwise.git /opt/shelfwise
cd /opt/shelfwise
git checkout developers
git pull --ff-only origin developers
```

3. Create short-lived credentials locally in the shell only. The Hugging Face token must
   have accepted the Gemma licence. Do not put either value in source control.

```bash
read -rsp "Hugging Face token: " HF_TOKEN; echo
export HF_TOKEN
export VLLM_API_KEY="$(openssl rand -hex 32)"
export VLLM_ALLOWED_CIDR='<application-host-private-ip>/32'
printf '%s\n' "$VLLM_API_KEY" > /root/shelfwise-vllm-api-key
chmod 600 /root/shelfwise-vllm-api-key
```

4. Start both serving tiers and the host-side control-plane environment:

```bash
bash scripts/bootstrap_mi300x_vllm.sh
```

The script detects AMD's preinstalled `rocm` container, installs `/opt/shelfwise/.venv`
with the benchmark extra, forwards the HF token for cold downloads, starts both models, and
publishes the strong port. It uses `--enforce-eager` to avoid long ROCm graph-compilation
delays during demo bootstrap. It validates the host tools, ports, ROCm devices, and source CIDR
before downloading weights, and writes `/root/shelfwise-mi300x-bootstrap.json` only after both
model IDs are returned by authenticated `/v1/models` checks.

Keep the commit from `git rev-parse HEAD` and that receipt with the run artifacts. If the command
times out, inspect the printed `/root/shelfwise-vllm/vllm-8000.log` or `vllm-8001.log` tail before
retrying; the failure is otherwise indistinguishable from a long model download.

## Verify Models

Use the container path for the authoritative readiness check:

```bash
VLLM_API_KEY="$(cat /root/shelfwise-vllm-api-key)"
docker exec rocm curl -fsS \
  -H "Authorization: Bearer $VLLM_API_KEY" \
  http://127.0.0.1:8000/v1/models
docker exec rocm curl -fsS \
  -H "Authorization: Bearer $VLLM_API_KEY" \
  http://127.0.0.1:8001/v1/models
rocm-smi --showuse --showmemuse --showpids
```

The bootstrap receipt is the compact, secret-free record of the same proof:

```bash
cat /root/shelfwise-mi300x-bootstrap.json
```

The public droplet address is ephemeral. Set the application values using the new droplet
IP, the two model names above, and the API key from `/root/shelfwise-vllm-api-key` through a
secret manager or a local ignored `.env` file.

## Host Benchmark

For a benchmark running on the droplet host, target the Quick Start container address rather
than host loopback for both tiers. The Quick Start NAT publishes external traffic, but host
loopback does not reliably reach the second port.

```bash
cd /opt/shelfwise
CONTAINER_IP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' rocm)"
VLLM_API_KEY="$(cat /root/shelfwise-vllm-api-key)" \
BENCH_HYBRID_ROUTINE_BASE_URL="http://${CONTAINER_IP}:8000" \
BENCH_HYBRID_STRONG_BASE_URL="http://${CONTAINER_IP}:8001" \
BENCH_HYBRID_ROUTINE_MODEL="google/gemma-4-E4B-it" \
BENCH_HYBRID_STRONG_MODEL="google/gemma-4-31B-it" \
BENCHMARK_API_KEY="$VLLM_API_KEY" \
.venv/bin/python -m shelfwise_benchmark.cli \
  --execution-scope cloud_inference_host \
  --strategy hybrid --peak 32 --synchronized-workflows 1 \
  --warmup-seconds 0 --steady-seconds 30 --repeats 1 \
  --max-workflows-per-window 32 \
  --output-dir reports/soak/mi300x_hybrid_concurrency
```

For the full 1/8/32 concurrency sweep referenced in `IMPLEMENTATION_STATUS.md`'s external-proof list,
repeat the same invocation at each peak (same env vars as above, omitted for brevity):

```bash
for peak in 1 8 32; do
  .venv/bin/python -m shelfwise_benchmark.cli \
    --execution-scope cloud_inference_host \
    --strategy hybrid --peak "$peak" --synchronized-workflows 1 \
    --warmup-seconds 0 --steady-seconds 30 --repeats 1 \
    --max-workflows-per-window "$peak" \
    --output-dir "reports/soak/mi300x_hybrid_concurrency_peak_${peak}"
done
```

Before running any of the above against the live droplet, validate the config offline first (no
endpoint required, catches config typos before burning droplet time):

```bash
PYTHONPATH=src python -m shelfwise_benchmark.cli --validate-config
# expected: valid workflow=shelfwise_eleven_role_cascade agents=11 strategies=4 kinds=[shared, replicated, per_agent, hybrid]
```

## Request-Deadline Semantics (operator note)

The backend's `enforce_request_deadline` middleware bounds every HTTP request at
`SHELFWISE_REQUEST_TIMEOUT_SECONDS` (default 120s) and returns a 504 when exceeded. Two
properties an operator must know:

- **The 504 cancels the wait, not the work.** Synchronous route handlers run on a thread
  pool; `asyncio.wait_for` cancels the awaiting coroutine, but the worker thread keeps
  running to completion in the background. Store writes are individually transactional and
  idempotent (scenario-stable decision IDs, `ON CONFLICT` learning inserts), so the
  finished-late work lands consistently rather than corrupting state - but CPU/DB capacity
  is still consumed. A burst of 504s under load therefore does NOT mean the work stopped;
  check DB/CPU pressure before assuming capacity is free.
- **The real inference bound lives in the LLM client**, whose per-call budgets
  (`LLM_TIMEOUT_SECONDS`, per-turn deadline math in the orchestrator) expire earlier and
  fail closed. The middleware is the outer safety net, not the primary control.

## Application Shakedown

Run the local ShelfWise backend with MI300X environment values, then run the receipt-driven
world harness. It fails on route errors, offline answers, chat failures, decision-ID reuse,
HITL mismatches, and no-op learning.

**Always use a fresh, timestamped `--output-dir` for a real droplet run - never a static name
like `reports/soak_15m`.** A static name is exactly what produced the pile of
`reports/soak_final`/`soak_final2`/`soak_final3`/`soak_final4`/`soak_15m_retry`/
`soak_postfix_final` directories from earlier attempts: every retry after an interruption
collided with the previous attempt's directory. As of 2026-07-14, a collision now fails fast
with a clear error instead of silently overwriting the previous run's data (pass
`--overwrite-artifacts` only if you genuinely want to discard the old run) - but a fresh
timestamp avoids the question entirely and keeps every attempt's data.

Also as of 2026-07-14, an interrupted run (SSH drop, droplet timeout, Ctrl+C, any unhandled
exception) still writes a best-effort `manifest.json`/`feature_receipts.json`/etc reflecting
everything accumulated up to the interruption - only `decision_trail.jsonl`/`cycles.jsonl` used
to survive a crash before this fix; the summarized report was previously lost entirely.

Run the first 15 minutes and inspect the result before deciding whether to run 30 minutes - do
not queue both back to back:

```bash
RUN_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
python -m shelfwise_eval.full_system \
  --duration-seconds 900 --live-required \
  --output-dir "reports/soak_15m_${RUN_STAMP}" \
  --run-id "mi300x_live_15m_${RUN_STAMP}"
```

Inspect `reports/soak_15m_${RUN_STAMP}/manifest.json`'s `passed`/`failures`/`totals` fields (or
run `python scripts/validate_full_system_artifact.py "reports/soak_15m_${RUN_STAMP}"`) before
deciding to continue. Only once that looks right, run the 30-minute follow-up with its own fresh
timestamp:

```bash
RUN_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
python -m shelfwise_eval.full_system \
  --duration-seconds 1800 --live-required \
  --output-dir "reports/soak_30m_${RUN_STAMP}" \
  --run-id "mi300x_live_30m_${RUN_STAMP}"
```

The known-good 15-minute run (`reports/soak_15min_20260711T042648Z/`) produced 20 feature
receipts, 14,341 route receipts, 158 live model chat answers, 1,520 unique decisions, zero HITL
mismatches, and 381 expected learning movements - use that as the rough shape of a healthy run.

## Training Boundary

The full Gemma 4 12B LoRA training configuration is intentionally pinned to
`w7900_jupyter`. Do not start it alongside the dual MI300X vLLM servers. Run the data and
evaluation shakedown on the W7900 training environment, and reserve the MI300X for serving,
benchmarking, and demo proof.

## Before Destruction

1. Ensure the current repository commit is pushed.
2. Copy any desired non-secret `reports/` artifacts and benchmark output.
3. Revoke the Hugging Face token used for provisioning and discard the droplet VLLM API key.
4. Destroy the droplet only after the local artifacts and this runbook are present.
