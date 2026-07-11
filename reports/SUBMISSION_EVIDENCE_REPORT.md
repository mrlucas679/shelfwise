# ShelfWise Submission Evidence Report

Evidence cutoff: 2026-07-11. This report separates measured behavior from configured or planned
behavior. A passing unit test, configured model name, or old harness marker is not treated as live
cloud proof.

## Executive Verdict

ShelfWise's core decision pipeline, agent/tool integration, HITL controls, tenant isolation, and
sequential live chat path are proven. The latest valid 15-minute run completed 333 world cycles,
made 333 model-backed chat calls through Gemma 4 E4B on an AMD MI300X cloud endpoint, and recorded
zero chat errors or offline fallbacks.

The application is not yet quantitatively capacity-proven for concurrent live inference or for a
two-model E4B/31B deployment. The repository now supports independent routine/strong endpoints and
refuses to claim dual-model submission readiness when both tiers resolve to one model ID. Actual
31B weight loading, endpoint smoke, comparative quality, queue depth, token throughput, and ROCm
resource telemetry still require the cloud instance.

## Measured Runs

### Valid live MI300X soak

Source: tracked compact receipt `reports/soak_15min_20260711T042648Z/summary.json`. It records
SHA-256 hashes for the local raw `manifest.json`, `chat_samples.json`, and `feature_receipts.json`.

| Metric | Measured value |
|---|---:|
| Duration | 900 seconds |
| World cycles | 333 |
| Generated / submitted / accepted events | 34,077 / 26,640 / 26,640 |
| Decisions / unique IDs | 4,618 / 4,618 |
| HITL approvals / rejections / mismatches | 2,934 / 56 / 0 |
| Learning movements | 34 / 34 expected |
| Chat calls | 333 |
| Model-backed / offline / errors | 333 / 0 / 0 |
| Sequential chat request rate | 0.37 requests/s |
| Retained chat latency sample | 20 calls |
| Sample average / P95 / peak latency | 2,025 / 2,266 / 3,531 ms |

The latency figures describe the retained 20-call sample, not all 333 calls. The run was a
sustained sequential product soak, not a concurrent inference saturation test.

### Rejected historical v2 run

Source: `reports/full_capacity_v2_revalidation.json`.

The old run originally exited successfully but is invalid under the current live-inference gate:
only 2 of 51 chats were model-backed and 49 silently used offline fallback. Revalidation returns
failure. It must not be cited as capacity evidence.

### Local control-plane concurrency replay

Source: `reports/inference_architecture_eval_20260710T091621Z/raw_results.json`.

| Scenario | Concurrency | Requests | Errors | RPS | Avg / P95 / peak latency ms |
|---|---:|---:|---:|---:|---:|
| Single user | 1 | 19 | 0 | 8.26 | 120 / 177 / 192 |
| Moderate | 8 | 680 | 0 | 72.44 | 95 / 226 / 428 |
| Heavy | 32 | 1,632 | 0 | 141.56 | 189 / 356 / 587 |
| Peak local | 64 | 2,176 | 0 | 183.55 | 252 / 426 / 762 |

These scenarios made zero model calls. They prove FastAPI/control-plane stability only. Their local
Windows GPU counters and 414.6 MB VRAM reading are not W7900 or MI300 model measurements.

## Required Metrics: Evidence Status

| Requirement | Current evidence |
|---|---|
| Agents and execution order | Proven for golden: inventory -> demand -> expiry -> opportunity -> simulation -> critic -> executive; Critic/Executive are sequential |
| Parallel vs sequential | Current production agent loops are sequential; no model-call fan-out is claimed |
| Model calls | 333 sequential live chat calls in valid soak; per-workflow agentic call counts are recorded by model-run telemetry |
| Input/output/total tokens | Instrumented per model run, but not exported with the final soak artifact; not available for the valid run |
| Average/peak response latency | 2,025 ms average and 3,531 ms peak over retained 20-chat sample |
| Requests per second | 0.37 live chat requests/s in sequential soak; concurrent model RPS not measured |
| Concurrent inference requests | Not measured on MI300X |
| GPU utilization / VRAM | Not captured from ROCm/vLLM during valid soak |
| CPU / host memory | Not captured from MI300X host during valid soak |
| Queue length / queue wait | Not exposed or captured by the endpoint |
| Idle time / inference wait | Not captured for the valid soak |
| Workflow completion time | 900 seconds for 333 sequential world cycles; individual chat sample latency above |

Missing values are not estimated from hardware specifications. The benchmark harness and templates
exist to collect them when both cloud endpoints are available.

## Agent and Tool Analysis

The application exposes 11 roles: orchestrator, inventory, sales, cold-chain, expiry, demand,
procurement, opportunity, simulation, critic, and executive. Live tests have exercised all 11
platform tools. The routine/strong policy routes Critic, Executive, and Orchestrator to the strong
tier; other roles use the routine tier. Deterministic decision-science tools perform arithmetic and
policy evaluation; Gemma interprets evidence, chooses tools, reviews contradictions, and explains
the decision. This avoids one model call per SKU.

## Bottlenecks and Loopholes Found

1. The old live-required harness accepted one model answer while 49 calls silently fell back. The
   gate now requires model answers to equal chat calls and rejects every offline answer/error.
2. Unbounded pending decisions grew chat prompts until inference timed out. Prompt state is now
   compact and bounded while the full decision store remains intact.
3. Chat originally had no conversation/message identity. It now scopes conversations to the trusted
   tenant and user, suppresses duplicate message IDs, bounds history, and has concurrent isolation
   tests.
4. A model-supplied tenant ID could override authenticated identity in tool calls. Trusted caller
   identity now wins, with a regression test.
5. The production frontend called `localhost:8000` from the viewer's browser. Nginx now proxies UI
   and APIs through one public origin.
6. Routine and strong model labels originally shared one transport target. Runtime configuration now
   supports independent endpoints/keys and submission readiness requires distinct model IDs.
7. The capability CI job did not install FastAPI although its test collection imported the app. The
   workflow now installs the project with `.[dev]`.

## Deployment Alternatives

### One shared model

Advantages: already proven with E4B for the sequential soak; lowest VRAM and operational cost; no
cross-tier routing failure mode. Disadvantages: strong review competes with routine chat, quality on
Critic/Executive is not compared against 31B, and one endpoint is a single failure domain.

### Multiple instances of one model

Advantages: can reduce queueing and improve availability. Disadvantages: no measured MI300X queue
or concurrent tail-latency evidence currently justifies the extra memory/compute cost.

### Hybrid small/large models

Advantages: matches the measured workload shape: deterministic tools and routine roles are lighter,
while Critic/Executive/Orchestrator perform synthesis and contradiction review. It allows E4B to
protect routine latency while 31B handles quality-sensitive work. Disadvantages: two weight sets,
two health checks, routing/failover complexity, and no current E4B-vs-31B quality/cost comparison.

## Recommendation

Deploy **E4B routine plus 31B strong as the production evaluation candidate**, using the repository's
dual-endpoint contract, but do not yet claim it is quantitatively superior. Keep the already-proven
single E4B path as the operational fallback. Promote the hybrid configuration only after one
1/8/32-user live benchmark captures token totals, queue depth/wait, P50/P95/peak latency, throughput,
ROCm GPU utilization, VRAM, CPU, host memory, and quality by role for both endpoints.

This recommendation is conditional for a measured reason: one E4B endpoint comfortably handled the
observed sequential 0.37 request/s workload, so extra replicas are not justified for throughput.
The reason to evaluate 31B is strong-role quality, not an unmeasured capacity assumption.

## Release Gates Before Recording

- Merge and deploy the public-origin/dual-readiness patch.
- Load and query both exact Gemma model IDs; do not reuse an adapter across sizes.
- Run routine and strong smoke calls and retain model-run IDs.
- Open the public URL in a clean browser and verify chat, agentic routes, HITL, and conversation
  isolation with zero console/network errors.
- Capture ROCm/vLLM metrics during the 1/8/32-user live benchmark.
- Re-run the full test/lint/frontend/Compose gates on the deployed revision.
