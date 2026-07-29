# Dependency audit

Audit date: 2026-07-29

## Verification

| Gate | Result |
|---|---|
| `python -m pip check` | No broken requirements |
| `tests/test_package_contract.py` | Runtime `pyproject.toml` dependencies are present in `requirements.txt` |
| `npm audit --omit=dev --json` | 0 production vulnerabilities at all severities |
| `npm ls --omit=dev --depth=0` | Production tree resolves |
| Frontend typecheck/build | Passed; 288 modules transformed |
| Python imports/wheel package list | Covered by full tests and package contract |

## Retained runtime dependencies

| Dependency | Active owner |
|---|---|
| `cryptography` | Fernet encryption for connector, webhook, and edge credentials |
| `fastapi` | API and dependency/authorization surface |
| `httpx` | Deployment shakedown and supported HTTP test/runtime tooling |
| `psycopg[binary,pool]` | Durable Postgres repositories and pooling |
| `pydantic` | API, capability, edge, multimodal, inference, and synthetic-data contracts |
| `python-multipart` | FastAPI upload/form parsing for real import and multimodal paths |
| `redis` | Redis Streams event bus |
| `uvicorn[standard]` | Supported backend server entry point |
| React / React DOM | Existing chat-first UI |
| React Markdown / remark-gfm | Assistant and evidence markdown rendering |
| IBM Plex font packages | Product typography imported by the frontend entry point |

Optional training, benchmark, and TSFM dependencies remain in named extras and are not forced
into the base production install. Playwright, TypeScript, Vite, pytest, and Ruff remain
development/build dependencies.

## Changes

No dependency was removed or upgraded. Every production dependency has an active source,
build, deployment, or supported optional-profile consumer. A major-version or speculative
upgrade would expand risk without closing a verified defect.

## Remaining evidence gap

`pip-audit` is not installed, so this pass cannot claim a current Python vulnerability
database scan. That is documented as TD-008. It does not invalidate the clean dependency
resolution, package parity, Node audit, build, or test results.
