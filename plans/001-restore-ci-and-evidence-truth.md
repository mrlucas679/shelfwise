# Plan 001: Restore CI and evidence truth

> **Executor instructions**: Follow each step and verification gate. Do not modify submission
> images, PDFs, generated reports, or recovery archives. Stop on any condition listed below.
>
> **Drift check**: `git diff --stat 9c907b3..HEAD -- .github/workflows/ci.yml src/shelfwise_inference/orchestration.py capabilities/manifest.json README.md HANDOFF.md`

## Status

- **State**: IN PROGRESS (local implementation complete; remote job proof pending)
- **Priority**: P1
- **Effort**: M
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug / tests / docs
- **Planned at**: commit `9c907b3`, 2026-07-13

## Why this matters

The successful MI300X soak does not make the branch releasable. Locally, Ruff reports two line
length failures, pytest reports one capability-contract failure (`464 passed, 1 failed, 3 skipped`),
and deterministic capability comparison reports broad drift. GitHub's latest `developers` workflow
runs fail with zero jobs, so no remote gate is executing. Release claims must not outrun CI truth.

## Current state

- `.github/workflows/ci.yml:128` and `:177` reference `secrets.*` directly in step-level `if`
  expressions; recent GitHub runs fail before creating any jobs.
- `src/shelfwise_inference/orchestration.py:337` and `:362` exceed Ruff's 100-character limit.
- `capabilities/manifest.json` differs from deterministic discovery, including the orchestrator and
  many auth-adjusted routes.
- `README.md:18`, `:74-77`, `:105`, and `:134-135` describe a single E4B tier and older soak totals,
  while `docs/mi300x-recreate-runbook.md:12` and `:106-107` record E4B + 31B and the final run.
- `HANDOFF.md:70`, `:409`, and `:417` retain mutually stale verification statements.
- Existing commit style is conventional, e.g. `fix: retry transient live chat inference`.

## Commands

| Purpose | Command | Expected success |
|---|---|---|
| Lint | `python -m ruff check src tests scripts` | exit 0 |
| Tests | `python -m pytest -q` | all tests pass; only documented skips |
| Capability | `python scripts/compare_capability_manifests.py` | exit 0 |
| Frontend | `cd frontend && npm run typecheck` | exit 0 |

## Scope

**In scope**: `.github/workflows/ci.yml`, `src/shelfwise_inference/orchestration.py`,
`capabilities/manifest.json`, `README.md`, `HANDOFF.md`, and focused workflow/capability tests.

**Out of scope**: application behavior, model routing, submission assets, generated reports, and
all user-modified binary files.

## Steps

### Step 1: Make the workflow parse and create jobs

Remove direct `secrets.*` use from `if`. Prefer one always-defined step with secrets mapped into
that step's environment and a shell guard: when endpoint/key values are absent, print the existing
fail-closed notice and exit 0; when present, execute the live gate. Remove the complementary second
step so there is one source of truth. Preserve the rule that ordinary CI never requires a GPU.

**Verify**: inspect the rendered YAML and run any repository workflow-contract tests. If `actionlint`
is already available, run it; do not add a new dependency just for this plan.

### Step 2: Restore local static gates

Wrap the two long comprehensions in `orchestration.py` without changing behavior. Run Ruff.

**Verify**: `python -m ruff check src tests scripts` exits 0.

### Step 3: Regenerate and review the capability manifest

Run `python scripts/compare_capability_manifests.py --write`, inspect the diff, and confirm every
changed capability maps to current source. Do not accept removed routes or weakened auth silently.

**Verify**: `python scripts/compare_capability_manifests.py` exits 0 and
`python -m pytest -q tests/test_capability_contract.py tests/test_capability_*.py` passes.

### Step 4: Reconcile release-facing evidence

Update README's model topology and soak figures to the final E4B/31B receipt. Replace stale HANDOFF
verification summaries with one dated authoritative block; mark older blocks historical rather than
deleting evidence. Ensure README does not describe in-process soak as production-topology proof.

**Verify**: tests that parse README/capability docs pass; `rg "333/333|4,618|2,990" README.md`
returns no release-facing stale claims.

### Step 5: Prove local and remote CI

Run all commands above. Commit only after they pass. Push only when the operator authorizes it, then
confirm the GitHub run contains real jobs and reaches at least Checkout/Lint rather than failing with
zero jobs.

**Verify**: `gh run view <run-id> --json jobs,conclusion` shows a non-empty `jobs` array.

## Test plan

- Add a workflow contract test that rejects direct `secrets.*` references inside `if:` lines.
- Keep capability snapshot equality as the regression gate.
- Run full pytest, Ruff, frontend typecheck, and capability comparison.

## Done criteria

- [ ] Ruff exits 0.
- [ ] Full pytest exits 0.
- [ ] Capability comparison exits 0.
- [ ] Frontend typecheck exits 0.
- [ ] GitHub workflow creates jobs and no longer fails at parse time.
- [ ] README/HANDOFF match the final dual-tier evidence.
- [ ] No submission or generated evidence file is modified.

## STOP conditions

- Manifest regeneration removes a route/tool/agent rather than updating metadata.
- Workflow repair requires exposing a secret in logs or repository files.
- In-scope source has drifted materially from the cited lines.

## Maintenance notes

Treat this plan as the prerequisite for every other change. A passing soak and a failing CI suite are
not competing truths: the soak proves runtime behavior; CI proves reproducibility and change safety.
