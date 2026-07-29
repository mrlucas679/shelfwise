# Capability manifest

Audit date: 2026-07-29
Canonical machine-readable artifact:
[`capabilities/manifest.json`](../capabilities/manifest.json)
Schema: [`capabilities/manifest.schema.json`](../capabilities/manifest.schema.json)
Policy and test evidence: [`capabilities/policy.json`](../capabilities/policy.json)

The manifest is generated from source without importing the application. Pull requests run
`scripts/compare_capability_manifests.py`, compare the current snapshot with the base branch,
and fail on omitted capabilities, status downgrades, removed relationships, source drift,
removed test node IDs, invalid waivers, or a non-normalized snapshot.

## Current inventory

| Kind | Records | Evidence status |
|---|---:|---|
| Agents | 11 | 11 verified |
| Bus backends | 2 | 2 verified |
| Connectors | 9 | 9 verified |
| Deployment profiles | 4 | 2 verified, 2 external-proof partial |
| Event consumers | 9 | 9 verified |
| Event types | 9 | 9 verified |
| Frontend surfaces | 14 | 14 verified |
| Multimodal features | 4 | 4 verified |
| OpenAPI method/path pairs | 141 | 141 verified |
| Storage backends | 2 | 2 verified |
| Agent/runtime tools | 22 | 22 verified |
| Training stages | 6 | 1 verified, 1 implemented, 4 external-proof partial |
| Workers | 3 | 3 verified |
| Workflows | 9 | 9 verified, including adaptive failure attribution |
| World-generation scenarios | 3 | 3 verified |
| **Total** | **249** | **242 verified, 1 implemented, 6 partial** |

Fingerprint:
`sha256:5e0a4aa76d4f89e93365463cb52231e76daeef0e3830d8a0d5918b33e152a048`.

## Scope and interpretation

The 249 individual records contain their capability ID, source locations, relationships,
status, and meaningful pytest node IDs. OpenAPI discovery scans `app.py`, every
`routes_*.py` module, the intelligence router, and multimodal routes. Frontend, agent, tool,
connector, event, worker, storage, world-generation, training, and deployment discovery use
their authoritative registries or declarations.

The manifest is the record-level inventory; `FEATURE_COMPLETION_MATRIX.md` is the
reader-oriented roll-up. The repository does not infer live provider or accelerator proof from
source code, mocks, configuration, or local deterministic tests.

Exact-SHA push and pull-request Capability Contract workflows passed for implementation commit
`abe6924`: [push run 30484272250](https://github.com/mrlucas679/shelfwise/actions/runs/30484272250)
and [pull-request run 30484275116](https://github.com/mrlucas679/shelfwise/actions/runs/30484275116).

## External-proof records

| Capability | Status | Boundary |
|---|---|---|
| `deployment_profile:fireworks_demo` | partial | Code routing exists; no current live Fireworks receipt |
| `deployment_profile:mi300x_vllm_demo` | partial | Code routing exists; current accelerator endpoint proof is external |
| `training_stage:preflight` | implemented | Requires the designated W7900D environment |
| `training_stage:evaluate` | partial | Fixture evaluation is verified; generated-model evaluation is external |
| `training_stage:serving_check` | partial | Metadata checks are verified; generated endpoint inference is external |
| `training_stage:shakedown` | partial | Dry-run orchestration is verified; hardware execution is external |
| `training_stage:train` | partial | Entry point exists; no new hardware training run was claimed |
