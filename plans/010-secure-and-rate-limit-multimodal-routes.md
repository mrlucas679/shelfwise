# Plan 010: Secure and rate-limit multimodal processing routes

> **Executor instructions**: Treat uploaded audio and images as costly, hostile inputs. Keep candidate generation and human review behavior intact while ensuring an enabled production feature cannot become anonymous processing.

> **Drift check (run first)**: `git diff --stat f7d3ab8..HEAD -- src/shelfwise_multimodal/router.py src/shelfwise_backend/app.py docker-compose.production.yml tests/test_multimodal.py tests/test_infra_config.py`

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: none
- **Category**: security
- **Planned at**: commit `f7d3ab8`, 2026-07-21

## Why this matters

Voice and scan routers use a local optional API-key guard rather than the backend's standard authorization and rate-limit policy. When `MULTIMODAL_ENABLED=true` and `API_KEY` is unset, JWT mode does not protect `/voice/in`: an anonymous audio upload receives HTTP 200 and reaches transcription. The production Compose file disables multimodal now, but the supported configuration becomes unsafe as soon as it is enabled. Image uploads, transcription, and synthesis are expensive hostile-input boundaries and need an explicit production access contract.

## Current state

- `src/shelfwise_multimodal/router.py:76-84` and `:124-132` define local guards that allow every request when `API_KEY` is empty.
- `src/shelfwise_multimodal/router.py:87-162` exposes audio upload, synthesis, barcode, receipt, and image routes without the shared `WRITE_LIMIT_DEP`.
- `src/shelfwise_backend/app.py:285-293` includes both routers without adding router-level tenant/role dependencies.
- `docker-compose.production.yml` sets JWT mode but does not set a multimodal access invariant; it merely leaves the feature disabled by default.
- `tests/test_multimodal.py` verifies a configured API key, but does not assert that enabled JWT deployments reject anonymous voice/image processing.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused tests | `$env:PYTHONPATH='src'; python -m pytest -q tests/test_multimodal.py tests/test_tenant_auth.py tests/test_infra_config.py` | all pass |
| Lint | `$env:PYTHONPATH='src'; python -m ruff check src tests` | exit 0 |
| Full regression | `$env:PYTHONPATH='src'; python -m pytest -q` | all pass |

## Scope

**In scope**:

- `src/shelfwise_multimodal/router.py`
- `src/shelfwise_backend/app.py` only for shared dependency wiring
- `docker-compose.production.yml` and `.env.example` if configuration validation needs documentation
- `tests/test_multimodal.py`, `tests/test_tenant_auth.py`, and `tests/test_infra_config.py`

**Out of scope**:

- Changing media sniffing, file-size ceilings, model providers, or review-required candidate semantics.
- Persisting raw media beyond the existing configured upload behavior.
- Exposing an unauthenticated public-upload product flow without a separately designed abuse-control system.

## Steps

### Step 1: Define the multimodal access contract

Choose and document one explicit policy compatible with the rest of the backend: in JWT mode, processing routes require a valid authenticated tenant context plus the normal write guard/rate limit; local auth-off development may remain convenient. Ensure named deployments fail fast when multimodal is enabled without the required access configuration, rather than relying on an empty optional key.

**Verify**: production configuration with multimodal enabled and missing required protection fails at startup or rejects all processing requests deterministically.

### Step 2: Reuse shared protection rather than duplicate it

Wire the existing backend `write_path_guard` and `WRITE_LIMIT_DEP` into both optional routers without creating a second token bucket. For routes that create tenant-bound scan candidates, resolve tenant identity through the same current-tenant dependency and apply the appropriate operational role. For voice/image routes that do not persist a domain entity, still require an authenticated caller in JWT mode and retain bounded error messages.

**Verify**: configured API key and valid browser-session JWT continue to work through the mounted backend router.

### Step 3: Add adversarial regression tests

Use the existing fake transcription/synthesis seams. Assert anonymous requests are rejected when JWT mode and multimodal are enabled, valid tenant/session requests succeed, and repeated requests consume the shared limiter. Test voice input, image input, and barcode/receipt candidate generation separately because their persistence and identity behavior differ.

**Verify**: focused tests pass without contacting external model providers.

### Step 4: Verify deployment truth

Add a configuration contract test covering the production Compose behavior when multimodal is deliberately enabled. Update `.env.example` only with non-secret configuration guidance. Run lint and the full suite.

## Done criteria

- [ ] Enabled JWT deployment rejects anonymous voice and image processing.
- [ ] Multimodal routes use the central write guard and rate limiter or an explicitly stronger equivalent.
- [ ] Valid authorized calls retain media validation and review-required outputs.
- [ ] Production configuration cannot accidentally enable anonymous multimodal processing.
- [ ] Focused tests, lint, and full pytest pass.

## STOP conditions

- Stop if a user-facing public upload flow is a stated product requirement; it needs a separately designed abuse-control policy (quotas, proof-of-work or signed upload grants), not a silent exemption.
- Stop if importing backend dependencies into the multimodal package creates a circular import; move only the small shared dependency construction to a neutral module and add regression coverage.

## Maintenance notes

Any new upload or model-invocation endpoint must be classified as an expensive trust boundary. Its route declaration should show authentication, authorization, size limit, rate limit, and failure behavior together so reviewers do not infer safety from optional-feature defaults.
