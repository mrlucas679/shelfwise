# Hardcoded-values audit

Audit date: 2026-07-29

## Result

No committed production secret, token, private key, password, public cloud IP, user-specific
absolute path, or fixed production tenant/store identifier was found by the tracked-file and
high-risk literal scans.

Dedicated secret scanners (`gitleaks`, `trufflehog`, `detect-secrets`) are not installed in this
workspace; that limitation is recorded as debt. The manual scan covered common API-key,
password, token, secret, private-key, `sk-` key, IPv4, URL, Windows path, and Unix home/path
patterns. The only tracked environment files are `.env.example` and `frontend/.env.example`.

## Classification

| Value class | Finding | Classification / action |
|---|---|---|
| Credentials and encryption keys | Runtime reads environment variables; examples are blank or clearly test-only | Correct; no real secret committed |
| Model/provider names | Typed inference config with environment overrides and profile snapshots | Correct configuration, not embedded credential |
| API/database/Redis URLs | Environment/profile driven; localhost defaults occur only in local startup, tests, CI, and optional local services | Safe local defaults |
| Ports | Backend/frontend/Playwright/startup defaults are overrideable | Safe local defaults; isolated browser run used 8017/5187 |
| CORS origins | Local origins are safe defaults; production validation rejects unsafe combinations | Correct environment boundary |
| Tenant/store/SKU IDs | Demo/test fixtures are explicitly simulation-scoped; operational events require authenticated tenant context and measured facts | Stable fixture/domain constants |
| Currency | `Money` is typed with explicit currency; current product policy is ZAR | Intended South African domain constant, not accidental unit |
| Retry/timeout/queue bounds | Named constants or validated environment settings; registry/stream/body/context limits are bounded | Retained as documented operational policy |
| Simulation constants | Named scenario/story constants under generated-world data only | Retained; operational paths fail closed on missing measured facts |
| Roles | Central role enum and decision-assignment map | Retained canonical authorization policy |
| Paths | Repository-relative `Path` construction and environment-derived persistence roots | Portable; no user machine path in production code |
| Adaptive attribution | Disabled flag, minimum successes, and alpha documented/validated | Added `.env.example` entries; invalid values fail closed |

## Literal URLs reviewed

- `127.0.0.1`/`localhost` in startup, Playwright, CI, and inference-evaluation scripts:
  local-only and overrideable.
- Multimodal localhost defaults: the entire feature is disabled by default and production
  startup validates its auth boundary.
- SVG XML namespace: protocol identifier, not a network endpoint.
- Deliberately dead port `127.0.0.1:1` in a failure-injection test path: retained test fixture.

No literal was moved merely to replace a clear stable domain constant with configuration.
