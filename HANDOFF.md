# HANDOFF — current continuation state as of 2026-07-28

## Workforce productization, bounded retrieval, and POS inventory projection — BUILT, 2026-07-28

Plan 012 is implemented against the existing dedicated-client architecture:

- A platform-authorized, one-time browser bootstrap creates the configured client profile and
  first opaque-ID owner. Configured legacy-owner credentials migrate idempotently and cannot
  remain an implicit steady-state fallback unless emergency recovery is explicitly enabled.
- Workforce accounts now have durable status, single-use invitation/reset digests, session
  versions, and identity-only audit events in memory/Postgres stores with central-schema RLS.
  Owner actions cover invitation/resend, role changes, recovery, deactivation/reactivation, and
  last-owner/self-deactivation protection. Workers activate, reset, and replace temporary
  passwords in the browser; role/password/activity changes invalidate earlier workforce JWTs.
- Account email uses a provider-neutral standard SMTP contract. Missing delivery configuration
  fails closed and tokens are carried in URL fragments, never returned by browser APIs or stored
  as plaintext. A real mail account remains an operator credential boundary, not hidden software.
- `retrieval_planning.py` builds a deterministic plan before chat state is read. Every answer
  records selected partitions, counts, omissions, freshness, conflicts, inadequacy, and a hard
  maximum-one-follow-up receipt. Support/account questions skip operational state; conflict,
  risk, or inadequate evidence selects the actual strong model role.
- Normalized operational stock updates and POS sales now project into the inventory ledger with
  tenant-scoped replay receipts. Duplicate delivery cannot double-decrement, shortfalls never
  make quantity negative, and missing baselines/fractional quantities remain visible receipts.
  The existing Square, Shopify, Lightspeed, and Yoco paths supply these normalized events; live
  retailer credentials are still required for external acceptance.

Verification on the final local tree:

- `907 passed, 21 skipped` — complete Python suite.
- Ruff clean across `src`, `tests`, and `scripts`.
- Frontend TypeScript check and production build passed (288 modules).
- Capability contract: 241 deterministic capabilities,
  `sha256:3697ed095051760ab9146f1247320b68c175bfd4cc0b2abde5ca4a25aedc3f47`.
- Playwright: 10/10 passed against isolated ShelfWise ports 5187/8017, including activation and
  first-owner setup screens plus the existing onboarding, HITL, chat, connector, and device paths.

Exact-head GitHub Actions passed on implementation commit `e656d1d`: both push and pull-request
CI plus both capability-contract runs completed successfully. CI exercised 927 tests with fresh
Postgres/Redis (1 environment-gated skip), the distributable wheel, production Compose topology,
and deployment shakedown. Live MI300X/Fireworks, SMTP delivery, and retailer-sandbox traffic
require external credentials and must not be inferred from local or CI success.

## Guided first-store onboarding — BUILT and browser-verified, 2026-07-28

ShelfWise now has a resumable **Setup guide** in the product instead of requiring a new
client to assemble the runbook manually. The owner completes three required steps backed by
authoritative tenant-scoped server state: company profile, store creation, and one real data
source (encrypted connector credentials or a committed CSV import). Device provisioning and
named work accounts are available in the same flow but remain optional, so an owner-operated
shop without edge hardware is not blocked.

The new owner-only `GET /onboarding/status` read model derives progress from the company
profile store, durable twin onboarding manifests, configured connector credentials, imported
records, edge devices, and work accounts. It never accepts browser completion flags. Store
manifest listing is now bounded and tenant-scoped in both memory and Postgres.

The browser campaign found and fixed two real defects:

- An empty runtime `apiBase` incorrectly overrode `VITE_API_BASE`, so isolated deployments and
  tests silently fell back to `localhost:8000`. Empty runtime values now correctly defer to the
  build-time endpoint.
- The company form merged the full server profile into editable form state, then posted
  server-only fields (`tenant_id`, `status`, and timestamps) back to a strict endpoint, causing
  a 422. The form now selects only the four editable company fields at the trust boundary.

The Playwright harness now uses explicit isolated frontend/backend ports and invokes the local
Vite executable directly, preventing it from reusing an unrelated server on port 5173. Its
stale navigation smoke assertion was also corrected: dynamic simulation counts are matched as
counts, while the dedicated delivery test continues to assert the exact 17-issue result.

Verification on the final tree:

- `892 passed, 21 skipped` — complete local Python suite.
- Ruff clean across `src`, `tests`, and `scripts`.
- TypeScript `tsc --noEmit` and the Vite production build passed (287 modules).
- Capability contract: 231 deterministic capabilities,
  `sha256:704b122afaf55b2157bb80a4ff2fa0c3c3f4c2965c3d0759a60392044fe7b582`.
- Playwright golden path: 8/8 passed against disposable ShelfWise servers, including guided
  onboarding, HITL approval, grounded chat, ERP connection, and edge-device registration.

The first GitHub CI run on this work then found an older packaging debt that local dependency
state had masked: `cryptography>=42` was declared in `pyproject.toml` but absent from
`requirements.txt`, which is what CI and the backend Dockerfile install. The distributable
wheel therefore failed to import its encrypted-credential module in a clean environment.
`requirements.txt` is aligned, and `test_package_contract.py` now enforces that every runtime
project dependency remains present in the deployment requirements file. A wheel-only import
was reproduced successfully from outside the source tree before the follow-up push.

The next clean CI run exposed a second production-only drift: the durable edge-device registry
had a module-local auto-schema but was absent from the central `schema.sql` used when production
disables runtime DDL. The central migration now creates the encrypted device table, and a static
schema contract prevents another local/production split. The same run also proved that a raw
connector-credential assertion was querying a forced-RLS table without binding its tenant; the
test now reads the stored ciphertext through the restricted, tenant-bound application role.

GitHub Actions is green on final implementation commit `2dc89b4`: **912 passed, 1 skipped**
against fresh Postgres and Redis; the 33/33 eval gate, wheel-only import, 8/8 browser suite,
amd64 production image build, public-origin smoke, and three-cycle deployment shakedown all
passed. The separate capability-contract workflow also passed. CI did not have AMD endpoint
credentials, so live MI300X/Fireworks response proof remains an external cloud-run boundary;
the deployed stack correctly failed closed rather than manufacturing an offline chat answer.

Local environment note: this machine's project `.venv` launcher currently points at a removed
uv-managed Python installation. Verification therefore used the already-installed Hermes
Python environment plus the bundled Node runtime. This is a local launcher repair, not an
application or CI failure; do not claim that the broken `.venv` itself was tested.

## Camera/sensor connectivity — BUILT, 2026-07-23 (real self-serve credential, not raw video)

Direct follow-up after the login/credentials work below: the user pushed back that
"cameras" specifically still needed to be connectable, not marked out of scope. Rather than
fabricate video processing (genuinely impossible to build honestly in a session - real
computer-vision/hardware work), investigated what a real answer looks like and found a
second real, closeable gap underneath the first "explicitly out of scope" framing:

**`edge_device_registry` (`shelfwise_edge`) had a `register()` method with no API route
exposing it at all** - not even an operator could provision a camera/sensor device
credential without hand-editing code. `/twin/edge/observations` (the real,
HMAC-authenticated, media-free intake endpoint that already existed) had no working front
door. Its own module docstring already said "process-local registry used until durable
device provisioning is selected" - an honest, self-acknowledged placeholder, same pattern
as the connector-credentials gap closed earlier today.

**Built, completely:**
- `PostgresEdgeDeviceRegistry` (`shelfwise_edge/registry.py`) - encrypted-at-rest (same
  `SHELFWISE_CREDENTIAL_ENCRYPTION_KEY`/Fernet mechanism as connector credentials, kept
  package-local rather than importing `shelfwise_connectors` to avoid an unwanted layering
  coupling), durable across restarts. **Deliberately NOT tenant-RLS-scoped** - a real
  design finding caught before it shipped, not assumed: `get_active(device_id)` is exactly
  how the edge-ingest route resolves *which* tenant a request belongs to, from an opaque
  device_id alone, before any tenant is known. Binding a tenant to look up the tenant is
  circular, and with no tenant bound, `current_setting('app.tenant_id')` is NULL, so RLS's
  `tenant_id = current_setting(...)` policy would silently return zero rows for every
  lookup - permanently breaking the endpoint. Tenant scoping for `list_devices`/`revoke` is
  instead enforced at the application layer, which is correct for this table's actual
  access pattern (device_id is an unguessable random token; possessing it alone grants
  nothing without the paired HMAC secret).
- `POST /twin/stores/{store_id}/devices` (owner-only) - provisions a device, returns the
  HMAC secret exactly once. `POST /twin/stores/{store_id}/devices/{device_id}/revoke`
  (owner-only, verifies tenant+store ownership before revoking).
- `EdgeDeviceRegistrationPanel` (`frontend/src/App.tsx`, in the Store Twin workspace) - a
  real self-serve UI: register, see the one-time secret, revoke. Help text states plainly
  what this is and isn't: **most commercial retail camera/people-counting systems push
  structured events via their own webhook/REST API rather than streaming raw video** - this
  panel provisions the credential a shop points that existing integration at. It is
  explicitly not a raw-video pipeline, and the UI says so rather than implying otherwise.

Verified live end-to-end in the Browser pane: registered a real device, confirmed the
one-time secret display, confirmed the list updates, revoked it, confirmed status flips to
"Revoked". New tests: `tests/test_edge_device_registration.py` (6 tests - registration,
secret-never-listed, **a freshly registered device can actually sign a real
`/twin/edge/observations` batch end to end**, revoke blocks further batches, cross-tenant/
cross-store revoke is rejected, owner-role required) and
`tests/test_postgres_schema_contract.py::test_postgres_edge_device_secrets_are_encrypted_at_rest_and_survive_a_fresh_instance`
(encryption-at-rest + durability across a fresh registry instance, simulating a restart).
New Playwright E2E test (`golden-path.spec.ts`, "a store owner can self-serve register a
camera/sensor device credential"). Full suite green after (858 passed, 21 skipped, up from
852/20 - the new tests), manifest regenerated (221 capabilities, up from 219).

**Still honestly out of scope, stated plainly:** raw video capture, streaming, storage, or
computer-vision processing (people-counting from camera frames, etc.) is not built and was
never attempted - that is a genuinely separate, much larger project. What's built is the
real, complete, secure front door for camera/sensor SYSTEMS THAT ALREADY compute their own
structured events (the overwhelming majority of commercial retail camera products) to
connect to ShelfWise.

## Self-serve login + "Connect your systems" credential UI — BUILT, 2026-07-23

Answers the concrete question the user asked directly: "if a shop like Boxer downloads
this and wants to connect their existing systems, how do they do it?" Investigation before
writing any code found the deployment model is deliberately white-glove per
`CLIENT_INTAKE_RUNBOOK.md` ("one dedicated stack per client, owner decision 2026-07-23") -
the user confirmed via AskUserQuestion to keep that model but make the parts a client's own
staff should self-serve actually self-serve, rather than requiring an operator to
hand-edit env vars or curl the API.

**Two real, severe gaps found and closed, not just the one originally asked about:**

1. **There was no login screen at all.** `ensureBrowserSession()` calls `POST
   /auth/session` on every load; in real client deployments (`SHELFWISE_AUTH_MODE=jwt`
   with public demo sessions correctly disabled - the secure default) that 401s with no
   session cookie yet, and the frontend had nothing to recover with - the entire app broke
   silently. This is more severe than the credentials panel: without it, an owner
   literally cannot open ANY page of a real deployment, let alone connect a system. Built
   `LoginScreen` (`frontend/src/App.tsx`) wired to the real, already-existing, already
   production-grade `/auth/login` (scrypt-verified, httpOnly JWT cookie) - the app now
   detects a 401 specifically (`isUnauthorizedError`) and renders a real sign-in form
   instead of a dead error screen.
2. **The "Connections" tab was a raw API-endpoint explorer**, not a form a non-developer
   could use - built for testing routes, not for a shop owner to type in their Odoo URL
   and API key. Built `ConnectorCredentialsPanel`, a real self-serve form (per-system field
   sets matching exactly what `connector_poll_service.py`'s resolver needs) wired to the
   credential-storage API from earlier today (`POST/GET/DELETE
   /connectors/{system}/credentials`). Values are never re-displayed once saved - only
   whether a system is configured.

**Real bug this local-dev/preview harness itself had, found while verifying live (not by
reading code):** `.claude/launch.json`'s backend launch command ran `uvicorn` directly with
no `--env-file`, so `.env` was silently never loaded for local dev/testing - unlike both
`docker-compose.yml` and `docker-compose.production.yml`, which correctly use `env_file:`.
Every `.env.example` var (including today's new
`SHELFWISE_CREDENTIAL_ENCRYPTION_KEY`/`SHELFWISE_LOGIN_*`) was inert for anyone running the
backend this way. Fixed permanently: added `--env-file .env` to the launch config (a real
uvicorn flag, not a workaround).

**Two more real bugs caught only by actually clicking through the feature in a live
browser, not by reading the code:**
- Three React list renders (`GATED_ENDPOINTS` in the debug endpoint explorer) used
  `key={item.path}` - collided for the new GET/POST pairs sharing
  `/connectors/{system}/credentials`, and pre-existed for other GET/POST pairs sharing a
  path. Fixed to `key={`${item.method}:${item.path}`}` at all 3 render sites.
- The Playwright E2E test added for this (`frontend/e2e/golden-path.spec.ts`, "a store
  owner can self-serve connect an ERP system through the real credential API") initially
  used `getByRole('region', ...)` to find the panel - `WorkspaceSection` renders a bare
  `<section>` with no `aria-labelledby`, so it never gets an implicit accessible name/role
  in browsers, meaning the query silently matched nothing. Fixed to a class + text locator.
  Also added `SHELFWISE_CREDENTIAL_ENCRYPTION_KEY` to `playwright.config.ts`'s test
  webServer env (that config deliberately doesn't load `.env` either, by design, so the
  new credential-write path needs its own explicit test key).

**Verified live end-to-end**, not just via the new test: booted both servers with
`SHELFWISE_AUTH_MODE=jwt` and a real scrypt-hashed login configured, cleared cookies,
confirmed the login screen renders, typed real credentials, confirmed `/auth/login`
succeeds and the full app loads; then in the Connections workspace, filled in real Odoo
credentials, confirmed `POST .../credentials` returns 200 and the row flips to
"Connected", confirmed the raw network response never contains the submitted value,
reloaded the page and confirmed status persists from the server (not local-only state),
then disconnected and confirmed it reverts to "Not connected". New Playwright test
(`e2e/golden-path.spec.ts`) covers the same flow against a fresh server for regression
protection. Full backend suite green after (852 passed, 20 skipped), manifest regenerated
and content-verified unchanged in structure (219 capabilities).

**Still explicitly out of scope, stated honestly rather than silently claimed as done:**
- Raw camera/CCTV video integration is not built. Camera and sensor products that already
  emit structured derived observations can now be provisioned self-service through the
  device registry above; video capture, streaming, storage, and in-app computer vision
  remain a separate hardware/computer-vision project.
- Webhook-based systems (Shopify, Square, Lightspeed, Yoco) still require operator-side
  configuration - they authenticate via a shared webhook secret, not a stored credential a
  store owner enters themselves, so they're deliberately not in the new panel.
- A pre-existing, unrelated E2E test failure was found and diagnosed while running the
  suite (`golden-path.spec.ts`'s "every populated simulation workspace opens..." expects
  "Sell first 12 products" but the live seeded count is now 16) - confirmed via a direct
  `git stash`/re-test A/B check that this predates and is unrelated to every change in this
  entry; not fixed here since it's outside this round's scope, flagged for whoever next
  touches world-generation seed determinism.

## Client-readiness verification pass — 2026-07-23 (real bug caught: dependency drift in `.venv`)

Ran a real end-to-end check per an explicit "make sure the app is fully ready for a client"
directive - not just re-reading test output, but actually booting both servers and hitting
live routes.

**Real bug found and fixed:** the project's own `.venv` (the interpreter
`.claude/launch.json` actually uses to run `uvicorn`) was missing `cryptography` and
`psycopg-pool` - both added to `pyproject.toml` this session but never installed into this
specific venv. `preview_start` on `shelfwise-backend` failed outright with
`ModuleNotFoundError: No module named 'cryptography'`. This had been silently masked all
session: this shell's `PATH` puts an unrelated `hermes-agent` tool venv ahead of this
project's `.venv`, so every `python -m pytest`/`ruff`/import-check run this session had been
executing against that stray venv, which happened to already have `cryptography` installed
from unrelated prior work - meaning **the actual deployment interpreter had never once been
verified this session** until this pass. Fixed: `.venv/Scripts/python.exe -m pip install -e
".[dev]"` to sync it for real. Re-ran everything that matters against the *correct*
interpreter afterward: full suite (852 passed, 20 skipped, identical to the stray venv's
result - the code itself was never wrong, only the dependency install was incomplete in the
real venv), ruff (clean), capability manifest (219, byte-identical hash to what the stray
venv produced).

**Lesson for future sessions in this repo, on this machine:** always invoke
`.venv/Scripts/python.exe` explicitly (or `.venv\Scripts\python.exe` in PowerShell) rather
than bare `python` - this shell's PATH does not resolve to the project's own venv by default.

**Live connectivity verified for real**, not just via pytest fixtures: started both
`shelfwise-backend` (uvicorn) and `shelfwise-web` (vite) via the Browser pane, confirmed
`/health` responds correctly, loaded the actual frontend at `localhost:5173`, and read its
real network request log - **20 live API calls, all 200 OK**, including
`/connectors/systems`, `/mlops/model-runs`, `/mlops/accountability`, `/tenants/me`, and
every other route this session's work touched. No console errors, no failed requests, no
error-boundary fallback rendered - the frontend genuinely renders live backend state
("Queue clear. I'll surface exceptions as soon as they appear.").

## Multi-tenant encrypted connector credentials — BUILT, 2026-07-23 (explicit user directive: fix everything, no deferral)

## Multi-tenant encrypted connector credentials — BUILT, 2026-07-23 (explicit user directive: fix everything, no deferral)

Closes the gap the 2026-07-14 connector-poll work explicitly scoped out as "its own future
decision": credentials for Odoo/SAP/SYSPRO/Dynamics were read from process-wide env vars,
correct for a single-tenant deployment but structurally unable to give two tenants different
ERP credentials.

**Built, completely:**
- `src/shelfwise_connectors/credentials.py` - `InMemoryConnectorCredentialStore`/
  `PostgresConnectorCredentialStore`, Fernet-encrypted at rest (key derived via SHA-256 from
  `SHELFWISE_CREDENTIAL_ENCRYPTION_KEY`, same "any secret in, one valid key out" pattern
  `TENANT_AUTH_SECRET` already uses), RLS-scoped like every other tenant table
  (`shelfwise_connector_credentials` added to `TENANT_SCOPED_TABLES` and to
  `src/shelfwise_storage/schema.sql`'s compose-init schema - caught by
  `test_compose_init_schema_matches_tenant_scoped_tables`, which is exactly what that test
  exists to catch). `resolve_connector_credentials()` prefers a tenant's stored credentials,
  falling back to the env-var default only when the tenant has none configured.
- `connector_poll_service.py`'s `build_configured_connectors` now takes an optional
  `credential_store` and resolves each system's fields through it - a tenant with stored
  Odoo credentials gets a connector built from ITS values, not the shared default, proven
  end-to-end (not just at the resolver-function level) by
  `tests/test_connector_poll_service.py::test_tenant_stored_credentials_take_priority_over_env_vars`.
- `routes_connector_credentials.py` (new router, kept separate from the read-only
  `routes_connectors.py` deliberately) - owner-only `GET`/`POST`/`POST .../delete` for a
  tenant's own credentials. `GET` never returns values, only whether a system is configured -
  verified by a test asserting the plaintext secret never appears in the response body.
- `cryptography>=42` added as a real declared dependency (was previously only a transitive
  import); `.env.example` documents the new var.

**Deliberately NOT built, and why:** the background poll loop is still one process polling
one tenant's connectors (`SHELFWISE_TENANT_ID`) per cycle - turning it into a true
multi-tenant loop (N tenants, N schedules, per-tenant backpressure and failure isolation so
one tenant's slow/broken ERP can't starve another's poll) is a separate, larger architecture
change that deserves its own design pass. What's built today means a tenant who configures
credentials gets tenant-specific behavior immediately in every credential-resolution call
site, without waiting for that larger change - a real, additive step, not a stub.

Tests: `tests/test_connector_credentials.py` (9, encryption round-trip, tenant isolation,
error handling), `tests/test_connector_credential_routes.py` (6, owner-only auth, no
plaintext leakage, cross-tenant isolation, 404 on unknown system),
`tests/test_postgres_schema_contract.py::test_postgres_connector_credentials_are_encrypted_at_rest_and_tenant_isolated`
(real Postgres: raw column never contains plaintext, cross-tenant read returns nothing),
`tests/test_connector_poll_service.py` (+2). `test_database_schema.py`'s compose-init-schema
contract test caught a missed step (forgot the static `schema.sql` file initially) - real
value from that mechanical check, not a formality. Full suite pending confirmation in this
same round; manifest regenerated (219 capabilities, +3 for the new router).

## Executive-conclusion grounding — CLOSED, 2026-07-23 (explicit user directive: fix everything, no deferral)

The smaller follow-on flagged when the critic-verdict finding was re-verified closed: the
Executive's conclusion in all 6 agentic cascades (golden/markdown, procurement, sales,
catalog-price, expiry-risk, cold-chain) is now grounding-checked against the same tool-result
evidence pool the Critic used (`assert_conclusion_grounded_in_tool_results`), not left as
unverified prose. For the 4 cascades with an autonomous-approval path (golden, procurement,
sales, cold-chain), this is **conditional on the critic having actually passed** -
`_enforce_critic_verdict` already forces the safe fallback action regardless of what the
Executive says once the critic fails, so the existing adversarial tests that deliberately
script an ungrounded, disagreeing Executive to prove that override holds (e.g.
`test_critic_gate_overrides_an_executive_that_escalates_past_a_failed_critic`) still pass
unchanged - grounding is only enforced in the path where the Executive's word is actually
trusted forward. The other 2 cascades (catalog-price, expiry-risk) always hard-route to one
fixed safe action regardless of the verdict, so their Executive grounding check is
unconditional (already the case, no test needed updating).

New regression test:
`tests/test_agentic_golden_cascade.py::test_agentic_golden_cascade_rejects_an_agreeing_executives_ungrounded_conclusion`
- scripts a Critic that passes with real cited figures and an Executive that agrees with the
route but writes "Looks fine, approving it." with no citation; asserts this is now rejected,
closing the actual gap (an agreeing-but-ungrounded Executive was previously accepted
silently). Updated 2 existing fixtures in `test_agentic_golden_cascade.py`
(`_scripted_messages` and the long-conclusion test) whose Executive conclusions didn't cite
tool numbers, to genuinely cite them - this is what real Executive conclusions from a
well-behaved model look like, not a workaround. 13/13 golden-cascade tests and all 40
agentic-cascade tests across all 6 cascade files pass; full suite green after
(manifest regenerated).

> **Working-product branch boundary:** This continuation is implementation work after the
> hackathon and belongs on `developers`. Treat `main` as the protected working-product branch;
> do not commit or merge these changes there without an explicit release decision.

> **Read this section first.** The historical notes below remain as evidence, but many of
> their branch names, counts, and deadlines are stale. The authoritative working branch is
> `developers`; only `main` and `developers` exist locally and on `origin`.

## Chat streaming claims-vs-reality gap — CLOSED, 2026-07-23 (explicit user directive: fix it, no deferral)

The user directly overrode the earlier "deferred, needs its own session" framing: "fix
everything... don't look at the risk... stick on that one thing until you finish it." This
was investigated properly rather than built as a half-measure. Findings before writing any
code:

- The chat answer is produced by `AgentOrchestrator.run_messages`
  (`src/shelfwise_inference/orchestration.py`) - the exact same bounded tool-calling loop
  every production cascade shares. A turn cannot be known to be "the final answer" until
  *after* its response arrives (the loop tries to parse a tool call first; only if there
  isn't one does it attempt to parse the turn as the final JSON answer), and grounding
  validation (`_assert_chat_grounded` in `chat.py`) only runs once the complete text exists.
- This means genuine live-token capture (piping `shelfwise_inference.stream_chat_deltas`
  straight from the provider to the client as tokens arrive) is fundamentally incompatible
  with the safety invariant every other agentic surface in this app holds: nothing reaches a
  caller that has not itself been validated. Streaming live would mean showing text before
  grounding can reject it.
- The alternative - re-invoking the model a second time with `stream=True` after the first
  (blocking) call already produced a validated answer - was also rejected: a second sampled
  call is not guaranteed to reproduce the exact validated text word-for-word, so the client
  could end up seeing content that was never actually validated.
- Modifying the shared `ModelRuntime`/`AgentOrchestrator` protocol to support genuine
  mid-loop streaming *would* be possible in principle, but only by touching code shared with
  every production cascade's tool-calling correctness guarantees - real risk to
  safety-critical decision logic, not risk-aversion for its own sake.

**What was actually built, completely, not as a stopgap:** `/chat/stream`
(`src/shelfwise_backend/app.py`) now delivers the fully-computed, fully-validated answer to
the client as a genuine sequence of incremental `delta` SSE events (word-chunked via the new
`_chunk_answer_for_delta_events`), followed by the existing `answer`/`replayed` event
(kept, unchanged, for backward compatibility with any client reading only the terminal
payload) and `done`. This closes the actual defect - the route's docstring and the
frontend's endpoint-panel label both previously claimed live per-token streaming and
delivered none; now the client genuinely receives the answer incrementally, safely, with
zero changes to the shared cascade-tool-calling pipeline. The docstring and frontend label
were corrected to state precisely what this is (validated-answer chunking, not live provider
tokens) and why, rather than re-asserting the false claim or reverting to silence.

Verified: `tests/test_chat_conversations.py::test_chunk_answer_for_delta_events_reconstitutes_exactly`
(chunking never drops or duplicates a character; empty/short/long inputs all reconstitute
exactly) and the rewritten `test_chat_stream_emits_a_truthful_lifecycle_envelope` (asserts
`delta` events are now present, concatenating them reproduces the terminal `answer` event's
text byte-for-byte, and the replay path also delivers deltas). All 9
`test_chat_conversations.py` tests pass; full suite and manifest regen pending confirmation
in this same round.

## Cold-chain profile invariant missing — fixed, 2026-07-23 (Design by Contract-grounded)

Framed by Object-Oriented Software Construction (Meyer)'s Design by Contract before opening
code: does the cold-chain domain (untouched beyond risk-math in the round-1 audit) enforce
real preconditions/postconditions/invariants, or silently accept invalid states? Checking
`src/shelfwise_resilience/thermal.py`'s `ColdChainProfile` (the dataclass defining a storage
class's `safe_max_c`/`unsafe_above_c` thresholds) against that question found it had no
`__post_init__` at all - constructible with an inverted or degenerate threshold
(`safe_max_c >= unsafe_above_c`). `_status` (`feed.py`) happens to check `unsafe_above_c`
first, so it wouldn't misclassify - but `predict_time_to_unsafe`'s
`minutes_to_unsafe = (unsafe_above_c - current_temp) / slope` has no such ordering defense:
with an inverted profile, a reading `_status` still calls "safe" could already be past the
(lower) unsafe threshold, producing a self-contradictory "already unsafe by prediction, still
safe by direct reading" alert pair. **Fixed** with a `__post_init__` invariant requiring
`safe_max_c < unsafe_above_c`, matching the pattern this session's earlier round already
found correctly applied in `expiry.py`/`cold_chain.py` (the decision-science layer) but
missing here in the resilience/feed layer's own profile type. Both built-in profiles
("chilled": 5.0/8.0, "frozen": -15.0/-12.0) already satisfy it. New test:
`tests/test_resilience.py::test_cold_chain_profile_rejects_an_inverted_safety_threshold`.
33 cold-chain/resilience/thermal tests pass; full suite green after.

## Catalog identifier-conflict race fixed — 2026-07-23 (DDD-grounded)

Framed by Domain-Driven Design (Evans) before opening code: DDD's specific warning that a
product catalog's identifier resolution (GTIN/barcode/PLU cross-references onto variants) is
a classic case where a shallow, non-atomic model silently lets "the same" identifier
represent two different entities. Checking `src/shelfwise_catalog/store.py` against that
warning found a real bug in `PostgresProductCatalogStore.upsert_identifier`: it checked for a
conflicting remap with a `select`, then ran a separate, unconditional
`insert ... on conflict (tenant_id, kind, value) do update set variant_id = excluded.variant_id`
- classic check-then-act. A concurrent writer's insert landing between the select and the
insert would be silently overwritten by the unconditional `do update`, defeating the exact
guard `ConflictingIdentifierError` exists to enforce (its own error message says "resolve the
conflict explicitly...instead of silently overwriting it," which the SQL could still do under
a race). The in-memory store was never affected - it holds one process-wide lock around the
whole check-and-write.

**Fixed** by making the `do update` itself conditional
(`where shelfwise_product_identifiers.variant_id = excluded.variant_id`) with `returning
variant_id`: the database, not a race-prone two-step Python check, now decides atomically
whether the write happens. When it doesn't (a real conflict), the code re-reads the actual
current mapping only to build a useful error message - the safety decision itself was already
made by the conditional `do update`. New regression test:
`tests/test_postgres_schema_contract.py::test_concurrent_conflicting_identifier_upserts_cannot_both_win`
(two variants race to claim the same GTIN via `ThreadPoolExecutor`; asserts exactly one wins
and the other observes `ConflictingIdentifierError`, never both silently "winning" by landing
in whichever order). Like the rest of that file's Postgres-only tests, it's skipped without
`SHELFWISE_TEST_DATABASE_URL` set (no live Postgres in this session's environment) - the
existing sequential `test_postgres_catalog_rejects_conflicting_identifier_remap` test in the
same file still exercises the corrected code's non-concurrent contract and was re-read against
the new SQL to confirm it still holds. In-memory catalog tests (6) and the full suite pass.

## Multitenancy RLS coverage — exhaustively verified, 2026-07-23 (DDIA/SAiP-grounded)

Framed explicitly by two books before touching code: DDIA's (Kleppmann) instruction to be
explicit about which isolation/consistency guarantee a piece of code actually needs, and
Software Architecture in Practice's (Bass/Clements/Kazman) method of naming the specific
quality attribute (here: security/tenant-isolation) before judging whether a mechanism
delivers it. The earlier round-1 audit had spot-checked RLS on a sample of tables and called
it "genuinely fail-closed and well defended-in-depth" - this round replaced the sample with
exhaustive coverage: every `create table if not exists` statement in the codebase (33
tables) was diffed against `TENANT_SCOPED_TABLES`
(`src/shelfwise_storage/rls.py`) in both directions, and every `apply_tenant_rls(...)` call
site (26, across every `*_store.py`/registry module) was enumerated to confirm each
tenant-scoped table's schema-creation path actually wires RLS, not just declares it eligible.
Result: zero gaps either direction - no tenant-scoped table lacks RLS, no RLS-declared table
is missing its `apply_tenant_rls` call. Stated plainly because it's a real result, not a
skipped check: multitenant data isolation at the storage layer is comprehensively enforced,
not merely claimed.

## Broad domain survey, round 4 — 2026-07-23: HITL audit trail, learning loop, chat streaming

Done directly (not via subagent) after a background audit agent failed mid-run when the
account hit its monthly Claude spend limit - noting this so a stopped/incomplete-looking
audit thread isn't mistaken for a silent skip.

### Fixed and regression-tested

- **HITL approve/reject audit trail recorded the wrong reviewer.** `decision_store.approve()`/
  `.reject()` (`src/shelfwise_action/store.py`) accept a `reviewer` keyword and genuinely build
  a real `review: {reviewer, status, reviewed_at}` audit record - the infrastructure itself is
  solid. The bug was upstream: both call sites in `src/shelfwise_backend/app.py`
  (`approve_decision`/`reject_decision`) had `ctx: TenantContext` (the real authenticated
  reviewer, `ctx.user_id`) available but never passed it through, so every decision's audit
  trail recorded the hardcoded default `"demo_manager"` regardless of who actually clicked
  approve/reject - a real bug, not an honest gap, since the audit-trail *field itself* existed
  and looked legitimate. Fixed by passing `reviewer=ctx.user_id` at both call sites. New test:
  `tests/test_tenant_auth.py::test_approve_and_reject_record_the_real_authenticated_reviewer`
  (two distinct authenticated users approve/reject separate decisions; asserts the recorded
  reviewer is each one's real identity, not a shared placeholder).

### Confirmed genuinely solid, no fix needed

- **The learning loop is real, not decorative.** `learning_store.thresholds()`
  (`src/shelfwise_memory/__init__.py`) computes a real monotonic high-water-mark per
  SKU/metric from approved decisions (`greatest(...)` on conflict), and it is genuinely
  consulted both in chat grounding (`app.py`'s `_new_chat_response`) and cited as evidence in
  the decision cascade (`cascade.py`'s `_learned_threshold_evidence`) - correctly as evidence
  for the human reviewer, never as a critic gate (this was deliberately designed that way in
  an earlier campaign session, task "Close the learning loop safely," and remains correct).

### Chat streaming — CLOSED (superseded, see "Chat streaming claims-vs-reality gap — CLOSED" at the top of this file)

The entry below described the state at the time it was written; it was closed later the same
day per an explicit user directive to stop deferring. Left in place as evidence, not as the
current status - do not re-read it as "still open."

`/chat/stream`'s own docstring (`app.py:1090-1100`) explicitly claims: "Token deltas from a
live endpoint slot into this same envelope as `delta` events via
`shelfwise_inference.stream_chat_deltas` once the answer turn streams from a live provider."
This is false as implemented - the route body always calls the blocking, non-streaming
`chat()` function, waits for the complete answer, and emits exactly one `accepted` →
`answer`/`replayed` → `done` sequence. `stream_chat_deltas` (`shelfwise_inference/client.py`)
is a real, correctly-implemented, unit-tested token-delta parser
(`tests/test_chat_conversations.py::test_stream_chat_deltas_parses_real_wire_chunks_and_fails_closed_offline`)
- it is simply never called from the endpoint. The frontend's own label ("SSE chat with a
truthful lifecycle envelope... deltas only from a live provider") repeats the same claim.

**Why this wasn't fixed in this session:** wiring real token-by-token streaming into
`/chat/stream` is a genuine feature addition, not a bug fix - it changes the SSE contract's
runtime behavior (the frontend's SSE consumer currently expects the three-event shape and
would need updating to actually render incremental `delta` events instead of waiting for one
`answer` event), and needs its own dedicated testing pass against a live provider. Rushing
that under a broad survey pass risks exactly the half-finished streaming feature the "no
temporary fixes" rule warns against. **Immediate, low-risk interim step for whoever picks
this up:** correct the docstring (and the frontend label) to state what the route actually
does today, so the claims-vs-reality gap is closed even before the real streaming feature is
built - false documentation is worse than an honest "not yet streaming" note.

## Broad domain survey, round 3 — 2026-07-23: twin/worldgen fidelity + MLOps eval gate closed

- **MLOps skill-promotion evaluation gate — CLOSED.** The deferred finding from round 2
  (`SkillPromotionBody.measured_pass_rate` trusting an arbitrary client-supplied float) is
  now fixed: a real `EvaluationRecord`/`InMemoryEvaluationRegistry`/`PostgresEvaluationRegistry`
  (`src/shelfwise_mlops/registry.py`) mirroring the existing `ModelRun` pattern, wired into
  `promote()` (`skill_registry.py`) which now requires `evaluation_id`, looks up the recorded
  evaluation, and checks both `gate_passed` and the recorded `pass_rate` against
  `manifest.minimum_pass_rate` - the caller can no longer just assert a number. `evaluate.py`'s
  `run_evaluation` records the real result. `routes_mlops.py`'s promotion body now takes
  `evaluation_id: str`, not a float. Full test suite green after (830 passed). This closes the
  single highest-severity finding of the whole survey - do not reopen the old
  `measured_pass_rate` contract.
- **Twin scenario predictions were contaminating operational reads - fixed.** Following up
  the twin/worldgen audit: `TwinStore.list_properties`/`list_observations` (both in-memory and
  Postgres backends, `src/shelfwise_twin/store.py`) now exclude `PREDICTED`-lane rows by
  default, with an explicit `include_predicted=True` opt-in for the one legitimate caller that
  needs them (`scenario.py`'s `compare()`). Before this, running any what-if scenario for a
  store silently changed what `get_store()`/`fidelity()` reported as the tenant's real
  operational state and governance fidelity score - a scenario's predicted stock level could
  leak into what the twin dashboard and `governed_execution.py`'s fidelity gate treated as
  fact. New regression test:
  `tests/test_twin_scenarios.py::test_get_store_and_fidelity_are_not_contaminated_by_scenario_predictions`
  (fails on the old code - confirmed by reproducing the failure before the fix). The one
  existing test that asserted the old (buggy) unfiltered-read behavior
  (`test_scenario_isolated_from_reported_state`) was updated to assert the new fail-closed
  contract instead of the old fail-open one.
- **Event-domain boundary (`operational_twin` vs `world_simulation`) confirmed genuinely
  enforced**, not decorative - `DataDomainBoundaryError` is actually raised and caught at both
  real call sites (`projection_worker.py`, `twin_projection.py`). No fix needed there.
- Manifest regenerated (`python scripts/compare_capability_manifests.py --write`), full suite
  830 passed / 18 skipped, confirmed clean after these fixes.

## Broad domain survey, round 2 — 2026-07-23: inventory/procurement/POS, connectors, MLOps

Continuation of the same two-lens survey into the remaining domains. One more well-scoped
bug found and fixed (below); one more large finding deliberately documented, not rushed
(the MLOps skill-promotion gate — see "Deferred" section below, second entry).

### Findings that needed no fix (stated plainly, not just "looked fine")

- **Reorder-point math** (`shelfwise_decision_science/inventory.py`) is genuinely solid:
  handles zero lead time, zero demand variance, negative-input rejection, and the
  `available == reorder_point` equality edge case with a documented deliberate choice. Not
  a finding - explicitly noting it so it isn't miscounted as unaudited debt.
- **Connector dedup/quarantine/durability** (`shelfwise_connectors/`, `ingest_pipeline.py`)
  matches the source-systems research's flagged risks (malformed records, dedup,
  publish-before-mark durability) with real, tested logic - not just claimed.
- **No live POS/cashier stock-decrement path exists anywhere in the codebase** (confirmed by
  full-tree search for sale/checkout/decrement code - the only sale-adjacent code is a Yoco
  webhook mapped into an after-the-fact analytics event in
  `shelfwise_connectors/connectors/systems/yoco.py`). This is an honest absence, not a bug:
  nothing claims real-time stock decrement exists. Flagging because it's a genuine capability
  gap for "build the whole store, every position" (see CLAUDE.md's mandate) - POS/cashier is
  explicitly named as in-scope and isn't built yet.

  **Re-examined 2026-07-23 under an explicit "fix everything, no deferral" directive - traced
  fully, not deferred out of caution.** `record_sale` (the sales cascade's recommended action)
  was followed all the way through: an approved `record_sale` decision only ever creates a
  human-facing manager task via `writeback_sink.create_task(...)`, whose own
  `rollback_instructions` explicitly declare
  `"policy": "recommend_only_no_source_mutation"` - this is not an accidental gap, it is the
  application's actual, deliberate architecture: **ShelfWise never writes back to a source
  POS/ERP system.** Every connector in `shelfwise_connectors` is inbound-only (pull/webhook
  *from* Odoo/SAP/SYSPRO/Dynamics *into* ShelfWise); there is no outbound write method to any
  of them anywhere in the codebase.

  A genuine fix has exactly two forms, and neither is buildable responsibly in this
  environment: (1) real outbound writes to each connected ERP's live API to actually
  decrement its stock - requires per-vendor API credentials and live sandbox access this
  environment does not have, and code written against an API you cannot reach cannot be
  honestly claimed as tested; (2) a shadow inventory ledger inside ShelfWise that decrements
  independently of the real POS/ERP - this would be a **regression**, not a fix: it creates a
  *new* false claim (the app implying it tracks live stock) while the actual source of truth
  was never touched, which is exactly the class of technical debt this campaign exists to
  remove, not add. **This is a resource/access constraint, not risk-aversion** - the honest
  "not built" state stands, and building around it dishonestly would be worse than leaving it
  open. Whoever picks this up needs real ERP sandbox credentials for at least one connected
  system before writing the outbound integration.

### Fixed and regression-tested (round 2)

- **`neutralise_formula_text` corrupts quoted CSV fields containing commas**
  (`src/shelfwise_connectors/gateway.py`) — it defanged formula-injection cells by splitting
  raw CSV lines on `,` before the real `csv.reader` (which understands quoting) ever runs, so
  a quoted value like `"Smith, John"` silently split into two cells. Called from
  `quarantine_intake`, the front door for all CSV intake - a "built but silently wrong" bug,
  not an honest gap. Fixed by rewriting `neutralise_formula_text` to parse/rewrite with the
  real `csv` module (`csv.reader`/`csv.writer`) instead of `line.split(",")`, which
  understands quoting and is byte-identical to the old output for the common unquoted case.
  New test: `tests/test_connectors.py::test_quarantine_preserves_quoted_fields_containing_commas`.

### "Open order" store vs PO lifecycle — CLOSED (naming corrected)

Re-checked 2026-07-23 per the explicit "fix everything" directive: `open_orders.py`'s module
docstring no longer overclaims - it now reads "Shipment-backed inbound-order reconciliation
ledger... intentionally does not create, approve, or transmit purchase orders." No remaining
reference anywhere in `open_orders.py` or `shelfwise_decision_science/sourcing.py` claims a
PO create/approve/transmit lifecycle; `sourcing.py`'s `expedite_supplier_order` is an
honestly-labeled advisory recommendation, not a claim of a real PO being sent. The
claims-vs-reality gap is closed via the correction path (accurate naming), not the
build-the-real-lifecycle path - a genuine PO create/send/approve system remains unbuilt, but
nothing in the codebase claims it exists.

### Deferred #2: MLOps skill promotion doesn't actually gate on evaluation — HIGH severity

`src/shelfwise_backend/routes_mlops.py:400-431`'s `SkillPromotionBody.measured_pass_rate` is
a client-supplied float, passed straight into `skill_registry.promote()`
(`src/shelfwise_mlops/skill_registry.py:321-339`), which only checks it against
`manifest.minimum_pass_rate` — a number the caller invented, never verified against a real
evaluation run. `SkillManifest.evaluation_ids` exists on the manifest but nothing ever reads
it or looks up an actual recorded result. **Anyone with the approval role can promote any
draft skill by posting `{"measured_pass_rate": 1.0}` with zero evaluations ever executed.**
The docstrings ("only past its own evaluation bar", "the enforcement point that makes the
lifecycle real") explicitly claim gating the code does not perform. There's also a
lower-severity TOCTOU race between `promote()`/`retire()` (get-then-set-status, no
compare-and-swap, in both the in-memory and Postgres registries) that can un-retire a skill
via a racing promote.

**Why this wasn't fixed in this session:** a correct fix means a real evaluation-results
store that both the training harness (runs on a separate host per the training/production
split - see memory `training-vs-production-environments`) and the backend API can share, plus
changing `promote()`'s contract from `measured_pass_rate: float` to `evaluation_id: str`
(looked up, not trusted) - a cross-subsystem persistence change, not a patch, and it touches
6 existing test call sites (`tests/test_conversation_assistant.py`) that currently assert the
old trust-the-caller contract. Building a shallow, unwired "evaluation_results table" that
nothing in the training harness actually populates would just relocate the claims-vs-reality
gap, not close it.

**Recommended design for whoever picks this up:**
1. New `EvaluationRecord` dataclass + `InMemoryEvaluationRegistry`/`PostgresEvaluationRegistry`
   + `create_evaluation_registry()` in `shelfwise_mlops`, mirroring `registry.py`'s `ModelRun`
   pattern exactly (same `SHELFWISE_STORE_BACKEND`/`DATABASE_URL` env wiring, so training-host
   and backend-host share one Postgres table the same way `model_run_registry` already does).
2. `shelfwise/training/evaluate.py`'s `run_evaluation` records one `EvaluationRecord` (id,
   pass_rate derived from the real `_evaluation_summary`, `gate.passed`, timestamp) after
   writing `eval_summary.json` - this becomes the only place a pass rate is legitimately
   produced.
3. Change `promote()`'s signature to `evaluation_id: str` instead of
   `measured_pass_rate: float`; it looks up the record, verifies `manifest.evaluation_ids`
   actually references that record's evaluation, and compares the *recorded* pass rate
   against `manifest.minimum_pass_rate`.
4. Update the 6 test call sites in `tests/test_conversation_assistant.py` (lines ~362-533) to
   seed a fake `EvaluationRecord` instead of passing a bare float, and add a new adversarial
   test proving an unrecorded/mismatched `evaluation_id` is rejected.
5. Fix the TOCTOU race as a small follow-on: wrap `promote`/`retire`'s get+set-status in the
   same per-skill lock the in-memory registry already holds elsewhere, and a
   `WHERE status = ...` conditional update for the Postgres path (same pattern already used
   for decision approve/reject - see `[[decision-store-concurrency]]` precedent in this
   file's earlier HITL race-condition fix).

## Broad domain survey (Phase 4) — 2026-07-23: logic-correctness + claims-vs-reality audit

Four parallel research passes across the highest-risk domains (decision-science math,
multi-tenant isolation/auth, agentic guardrails), asking two questions per finding: is the
logic actually right, and does the code/docstring/test claim more than it delivers. Three
concrete, well-scoped bugs found this way were fixed and regression-tested immediately
(see below); one larger structural finding was believed unfixed at the time this section was
first written, but was found already closed on re-verification later the same session — see
"Critic verdict self-report gap — RE-VERIFIED CLOSED" further down.

### Fixed and regression-tested

- **Training eval gate used naive substring matching** (`src/shelfwise/training/evaluate.py`)
  — `risk_classification` checked `risk in lower` (a bare `in`), so `"low"` matched inside
  unrelated text ("**low**er", "be**low**"); `actionability_score`/`evidence_grounding_score`/
  `finding_recall` only required the *first two tokens* of a reference phrase to appear
  anywhere in the output, so a generic two-word overlap could satisfy an entire recommended
  action. Since `_evaluation_summary` gates training acceptance on these scores, this
  overstated how good a model actually was. Fixed with `_contains_word` (word-boundary regex)
  for risk classification and `_phrase_covered` (majority of a phrase's non-stopword tokens
  must appear as whole words, not just the first two) for the phrase-overlap scores. No
  existing test locked in the old substring behavior; ran `tests/test_training_evaluation_gate.py`
  and `tests/test_mlops.py` clean after.
- **Write-rate limiter was tenant-blind** (`src/shelfwise_backend/security/gateway.py`,
  `deps.py`) — `WRITE_LIMIT_DEP` keyed its token bucket on the shared write API key or the
  caller's IP, never on the authenticated tenant. Since the write API key is one shared
  secret (not per-tenant) and tenants can share an egress IP/gateway, one tenant could
  exhaust another's write-rate budget with ordinary traffic — a real cross-tenant DoS, not a
  theoretical one. Made `rate_limit()`'s identity resolver injectable and added
  `_write_rate_limit_identity` in `deps.py`, which keys on the verified tenant_id from
  `_tenant_id_from_request` (already-verified JWT, never trusts a client-supplied header)
  when `SHELFWISE_AUTH_MODE=jwt`, falling back to the original IP/key identity otherwise
  (there's only one tenant to separate in non-jwt mode anyway). New test:
  `tests/test_write_rate_limit_tenant_isolation.py`.
- **Decision-access tenant check was fail-open on missing data**
  (`src/shelfwise_backend/decision_access.py`) — `decision_tenant_id`'s fallback-to-caller
  behavior (correct for *display*, e.g. labeling an untagged decision in a response) was
  reused for the *ownership* check too, so a decision row with no `tenant_id` (a legacy row,
  a code path that forgot to stamp it) silently matched *whichever* tenant happened to be
  asking — fail-open for exactly the row shape that's least trustworthy. Added a separate
  `_owned_by` helper used only for the ownership checks (`tenant_scoped_decisions`,
  `decision_belongs_to_other_tenant`) that treats an unstamped decision as belonging to
  nobody; `decision_tenant_id`'s original fallback behavior is untouched for its legitimate
  display call site (`app.py`). New test: `tests/test_decision_access_tenant_isolation.py`.

All three: `python -m ruff check` clean, full suite green (822 passed, 18 skipped) after,
`capabilities/manifest.json` regenerated (line/fingerprint-only diff, confirmed via
`git diff capabilities/manifest.json | grep -c '"name"'` → 0 both directions).

### Critic verdict self-report gap — RE-VERIFIED CLOSED, 2026-07-23 (corrects the entry below)

The "deferred, not fixed" entry originally written here for this finding was wrong by the
time it was re-checked the same day: `_server_verified_critic_passed`
(`src/shelfwise_backend/agentic_cascade.py:469`) already exists and is already wired into
all 4 cascades where it matters - `run_golden_cascade_via_agents` (markdown, line 587),
`run_procurement_cascade_via_agents` (line 848), `run_sales_cascade_via_agents` (line 1107),
and `run_cold_chain_cascade_via_agents` (line 1874) - each with its own domain-specific
evidence predicate (`_markdown_evidence_supports_action`,
`_procurement_evidence_supports_action`, `_price_evidence_supports_clean_sale`,
`_cold_chain_evidence_supports_dispatch`) computed from the real tool result, not the
model's self-report. It fails closed: `reported and evidence_supports_action`, so a Critic
that reports `critic_passed: true` without matching tool evidence is overridden. Confirmed
with a real adversarial unit test
(`tests/test_agentic_golden_cascade.py::test_server_evidence_overrides_a_critic_that_approves_a_loss`,
a model reporting `critic_passed=True` after `simulate_markdown` returned a loss is
overridden to `False`) plus full end-to-end critic-gate-override tests in all 4 cascade test
files. All 30 relevant tests pass.

The other 2 cascades (`run_catalog_price_check_via_agents`, `run_expiry_risk_check_via_agents`)
don't need this check structurally, not because of an oversight: both hard-require
`requires_human_review=True` and reject any executive action other than the one fixed safe
route (`review_price_exception`/`review_expiry_markdown`) regardless of `critic_passed` -
there is no autonomous-approval branch for a manipulated verdict to unlock, so
`critic_passed` is informational there, never a gate.

The smaller related gap noted in the original entry - grounding was enforced on the Critic's
conclusion only, never the Executive's - is now CLOSED too; see "Executive-conclusion
grounding — CLOSED" at the top of this file.

## `/scenarios/*` extraction complete — 2026-07-23 (final step of the app.py God-file campaign)

The `app.py` God-file extraction described below is now **finished**, not "explicitly not
finished" — this was the last and largest remaining piece
(`_agentic_unavailable`/`_agentic_deadline_exceeded`/`_cascade_deadline`/
`_production_execution_mode`, both `/scenarios/worldgen-runs*` routes, and the full
`_SCENARIO_MUTATION_DEPS` block through `demo_worldgen_drill` — ~700 lines) moved into a new
`routes_scenarios.py`, following the exact same `APIRouter` + `state.py`/`deps.py` sharing
pattern as `routes_catalog.py`/`routes_connectors.py`/`routes_mlops.py`. Two small prerequisite
modules were split out first to make this possible without a circular import:
`model_runs.py` (`record_model_run`, used by `/inference/smoke`, `/chat`, and every
`ExecutionContext` — not scenario-specific, so it couldn't move into `routes_scenarios.py`
itself) and the already-existing `decision_governance.py`/`twin_projection.py`/
`ingest_pipeline.py`/`decision_access.py` from earlier steps.

`app.py` is now **2,191 lines**, down from 3,766 at the campaign's start — genuinely done, not
an interim number.

**What broke and was fixed during this extraction** (all caught by the full test suite, not
assumed clean):
- 4 tests monkeypatched agentic-cascade functions (`run_golden_cascade_via_agents`,
  `run_cold_chain_cascade_via_agents`) and `_DEMO_DRILL_POLL_S` directly on
  `shelfwise_backend.app` — those names now live in `routes_scenarios.py`, so patching
  `app_module` silently no-opped instead of erroring (the route body reads the name from its
  own module's namespace). Fixed by monkeypatching `shelfwise_backend.routes_scenarios`
  instead, in `tests/test_agentic_http_errors.py`, `tests/test_agentic_operational_twin.py`,
  `tests/test_recall_workflow.py`. **This is a real, general risk for any future App→router
  extraction**: `grep` test files for `monkeypatch.setattr(app_module, ...)` / `setattr(app, ...)`
  targeting anything that moved, before declaring an extraction done — a wrong-module
  monkeypatch fails silently (the mock just never gets hit) rather than raising.
- 2 test files imported `_demo_event` / `_production_execution_mode` directly from
  `shelfwise_backend.app` — updated to import from `shelfwise_backend.routes_scenarios`
  (`tests/test_backend_observability_tools.py`, `tests/test_track3_contract.py`).
- `routes_scenarios.py` initially imported `operational_facts_for_query` from the wrong module
  (`.operational_facts`, where `OperationalFactsProvider` lives); the actual function is in
  `.state`. Fixed at import time (caught immediately — `python -c "from shelfwise_backend import app"` raised `ImportError` before any test ran).
- `capabilities/manifest.json` regenerated twice (once after the extraction, once after the
  `operational_facts_for_query` import fix shifted line numbers again) via
  `python scripts/compare_capability_manifests.py --write`; both times confirmed the diff was
  only `line`/`fingerprint` fields (`git diff capabilities/manifest.json | grep -c '"name"'` →
  0 additions/removals both times) — no capability was silently gained or dropped.

**Full suite status:** 816 passed, 17 skipped, 1 failed on the run right after the extraction —
the 1 failure (`test_committed_capability_snapshot_matches_deterministic_discovery`) was a stale
manifest from the import-path fix landing after the first regen; a second regen fixed it and it
now passes both in isolation and in the full run. A second full run turned up 5 more failures
(`test_full_system_harness`, `test_multimodal` x2, `test_provenance_boundaries` x2) — traced to
`OSError: [Errno 28] No space left on device`, not a code regression: **the C: drive has 152 MB
free out of 454 GB**, and these tests write real temp files. All 5 pass cleanly in isolation.
`test_worker_journal.py`'s 2 timing-sensitive tests remain the same known-flaky-under-load pair
documented earlier in this file — pass in isolation every time, occasionally fail under full-suite
system load.

**Docker verification was skipped for this step** — a `docker build` needs real disk headroom
this machine does not currently have (152 MB free); attempting it risked failing outright or
destabilizing the environment further. Every extraction before this one in the campaign got a
real `docker build && docker run` check; this is the one exception, and it's an environment
constraint, not a shortcut taken on the code. **Whoever picks this up next should free disk space
first, then run:** `docker build -t shelfwise-backend . && docker run --rm -p 8000:8000 shelfwise-backend`
and hit a real `/scenarios/*` route (e.g. `POST /scenarios/golden`) against the running container
before treating this extraction as fully closed.

## Library-driven technical-debt campaign — 2026-07-23

Two linked passes, both on `developers`, full backend suite green throughout
(**813 passed, 17 skipped** as of the last run in this campaign; every step below was
individually verified against the real suite, not assumed). Nothing here has been committed
yet — treat everything in this section as uncommitted working-tree state until reviewed.

### Method (read this before adding more work here)

1. Mined the 27-book engineering library at `Books-master` against the real running
   code — not generic advice, specific claims checked by reading the actual file, then
   fixed only what was confirmed real. Findings that turned out to already be correctly
   engineered (see "Verified correct, no bug" below) were left alone deliberately —
   manufacturing a fix for a non-bug is itself technical debt.
2. Every fix followed: read the code → confirm the defect is real → fix → add/extend a
   test that would have caught it → run the *actual* affected tests → run the *full*
   suite → regenerate `capabilities/manifest.json` if source moved → run the full suite
   again. Do not shortcut this loop for future debt-removal work; two of the real bugs
   found this session (the Dockerfile `data/` regression, the capability-discovery glob
   gap) were only caught *because* the full suite and manifest regen ran after every
   change, not just after the "big" changes.
3. "No half-finished implementations" was applied literally: several plausible-looking
   improvements (auto-adaptive critic thresholds fed from `learning_store`; automatic
   `PlanRunner` compensation execution; a full `/mlops/*` route extraction) were
   investigated and **deliberately not built** because they would have been genuinely new,
   unrequested behavior or required a prerequisite refactor not yet done — see "Explicitly
   deferred" below. Building them speculatively would have been new debt, not debt removal.

### Real bugs found and fixed

- **`critic_gate` receipt logic duplicated** across `agentic_cascade.py` and `cascade.py` —
  two independent research passes (Clean Code's DRY, the DI book's Constructor
  Over-Injection finding) converged on the same 4+ duplicated call sites for the
  Critic→Executive override contract. Consolidated into one shared `_enforce_critic_verdict`
  / `_critic_gate_receipt` pair in `cascade.py`; `agentic_cascade.py` now imports it.
  Regression risk this closes: the override rule could drift out of sync across cascade
  families if only some call sites were updated on a future change.
- **3 of 4 agentic cascade families had zero proof the critic gate can't be bypassed** —
  only the golden cascade had a test forcing a disagreeing executive through the gate.
  Added the same adversarial test to procurement, sales, and cold-chain
  (`tests/test_agentic_{procurement,sales,cold_chain}_cascade.py`).
- **`worker/plans.py` module docstring was stale** — claimed plan execution "is
  intentionally not wired to any route or worker yet," which stopped being true the day
  after that comment was written (`governed_execution.py` wires `POST /mlops/plans/execute`
  live). Rewritten to state the real, current wiring status and the real reason
  compensation is recorded but not auto-executed (every registered write capability today
  is single-step; there is nothing yet for automatic rollback to walk back through).
- **`_money()` in `shelfwise_multimodal/text_normalize.py`** (found in the prior session's
  bounded pass) silently dropped the fractional part of any monetary figure with a k/m
  multiplier suffix before being spoken by the voice interface — "R1.5k" was read aloud as
  "one thousand rand." Fixed to scale the full decimal value before splitting into
  rand/cents; covered by new test cases alongside the 5 existing ones.
- **Orphaned dead code in `shelfwise_mlops`** — `gate.py::release_gate` and
  `registry.py::release_gate` were two competing, unwired implementations of the
  blueprint's "release eval gate" concept (`plot/domains/11-mlops-finetuning-product.md`
  §1.2). Traced the real, live evaluation gate to `shelfwise/training/evaluate.py`'s
  `_evaluation_summary` (already wired, already tested, a better fit for the actual Gemma
  eval rubric) before concluding these two were superseded, not half-finished — deleted
  `gate.py` outright, removed `release_gate` from `registry.py`, updated
  `shelfwise_mlops/__init__.py` exports and `tests/test_mlops.py`.
- **Capability-discovery governance gap (self-inflicted, then fixed)** — extracting
  `routes_catalog.py` the same way `routes_twin.py` was previously extracted revealed that
  `_discover_openapi_routes` in `shelfwise_capabilities/discovery.py` scanned a
  *hardcoded* tuple of router filenames. The new router's 7 routes worked fine over HTTP
  and in pytest, but silently vanished from `capabilities/manifest.json` with no error.
  Fixed the root cause: glob `routes_*.py` instead of naming each file, so the next
  extraction can't repeat this. Added
  `tests/test_capability_contract.py::test_every_routes_module_contributes_its_routes_to_discovery`,
  verified it actually fails when the hardcoded-list bug is reintroduced.
- **Constructor Over-Injection in `agentic_cascade.py`** — all 6
  `run_*_cascade_via_agents` entry points took 9 keyword parameters spanning 4 unrelated
  concerns (event/execution-control/persistence/observability). Bundled
  `audit`/`model_run_recorder`/`deadline` into one `ExecutionContext` dataclass per the DI
  book's own prescribed fix (9 params → 7). Updated all 6 function signatures, `app.py`'s
  6 call sites, and the one test file that passed those kwargs directly
  (`tests/test_agentic_golden_cascade.py`); the other ~30 test call sites needed no
  change since they rely on defaults.
- **Docker production image shipped dead weight** — `Dockerfile` copied `tests/` (4.3MB,
  never read by any runtime path; confirmed CI runs pytest on the runner, never inside
  this image) into the deployable image. **Correction made mid-fix:** an initial pass also
  dropped `data/`, which broke
  `tests/test_infra_config.py::test_backend_image_contains_seeded_runtime_datasets` — a
  real, deliberate contract this session's own reasoning had missed. `data/` restored;
  only `tests/` removed. Verified three ways: full test suite, a real `docker build` +
  `docker run` + `/health` check, and direct inspection inside the running container.

### `app.py` God-file — real progress, explicitly not finished

`app.py` was 3,766 lines at the start of this campaign (89 routes, one file). Two clean
extractions landed, following the project's own pre-existing `routes_twin.py` /
`intelligence_api.py` precedent exactly (an `APIRouter` per cohesive domain, wired via
`app.include_router(...)`, sharing `state.py` singletons and `deps.py` dependencies):

- **`routes_catalog.py`** (158 lines) — the 7 `/catalog/*` product/variant/identifier CRUD
  + resolve routes. Zero coupling to app.py-local helpers.
- **`routes_connectors.py`** (54 lines) — the 3 read-only `/connectors/*` routes
  (`systems`, `me`, `inbound-records`). The 2 write routes
  (`/connectors/{system}/intake`, `/connectors/poll/status`) deliberately stayed in
  `app.py` — they depend on `_process_inbound_record`, an app.py-local helper shared with
  the CSV-intake routes; extracting only the read half would have relocated the coupling,
  not resolved it.

`app.py` is currently **~3,800 lines** (the extractions removed ~210 lines; the
`ExecutionContext` migration added some back). This is real, verified progress, not a
finished refactor — do not report "app.py is fixed" without doing the next step below.

**What's actually blocking further extraction** (checked directly, not assumed):

- `_auth_mode` and `_is_production_deployment` are **already** properly factored into
  `deps.py` and imported — they are not blockers.
- `_chat_data_domain` (defined at the bottom of `app.py`, ~34 call sites across the file)
  depends on nothing but `os.getenv` and the already-shared `_is_production_deployment` —
  it is a **safe, low-risk move** to `deps.py` right next to
  `_is_production_deployment`, and doing that is very likely the actual unblock for
  extracting more of `/mlops/*` and the remaining `/scenarios/*` routes. This was
  identified but **not yet done** when this note was written — pick it up first.
- `_process_inbound_record` (defined in `app.py`, used by both the connector-intake and
  CSV-intake routes) is the real blocker for the rest of `/connectors/*` and all of
  `/intake/csv/*` — **and, checked directly by reading the route bodies, also for most of
  `/scenarios/*`**: `demo_recall` and `demo_inventory_exception` (and, transitively, every
  agentic/demo scenario route via `_resolve_demo_pipeline_cascade`) call
  `_record_pipeline_event` too. This is the one shared core both remaining big groups
  actually need moved first.
  **Full call graph, traced function-by-function, not estimated:**
  `_record_pipeline_event` → `_cascade_for_event` → `_record_cascade` →
  `_attach_decision_governance` → `_measured_model_call_tokens` /
  `_estimate_cascade_tokens` / `_inference_rate`; plus `_project_twin_event`,
  `_same_event_payload`, `_resolve_demo_pipeline_cascade`, `_await_worker_cascade`,
  `_scenario_drill_wait_seconds`. That's **~12 interdependent functions across four
  concerns** (durable-store-first ingest idempotency, twin projection, cascade dispatch,
  decision economics/governance), plus the module-level singletons `event_store`,
  `open_order_store`, `event_bus`, `cascade_dispatcher`, `decision_store`,
  `trace_registry`, `twin_service`, and `worker_enabled()`.
  `_record_pipeline_event` specifically is the exact durable-store-first ingest core
  (event record → self-heal-safe publish → `mark_published`) this campaign's earlier
  session stress-tested for crash-window idempotency
  (`tests/test_event_ingest.py::test_ingest_self_heals_when_the_crash_lands_after_publish_but_before_mark_published`).
  **This is not a leaf helper move like `_chat_data_domain` was, and not a same-afternoon
  mechanical relocation like the catalog/connectors router extractions.** It is the
  application's safety-critical spine, spanning four distinct concerns that each deserve
  their own home rather than one dumping-ground module. **Deliberately not extracted this
  session** — attempting it under time pressure is exactly how "removing debt" becomes
  "introducing a subtle ingest bug in the one place that must never have one."
  **Recommended approach for whoever does this:** don't do it as one big move. Split by
  concern across 2-3 sessions — e.g. (1) **done, 2026-07-23:** decision
  governance/economics (`_attach_decision_governance` + its 3 helpers) moved into
  `decision_governance.py` (public names, no underscore prefix - `attach_decision_governance`,
  `measured_model_call_tokens`, `estimate_cascade_tokens`, `inference_rate`); `app.py`'s 3
  call sites and the 3 test files that imported it directly
  (`test_agentic_{golden,procurement,cold_chain}_cascade.py`) updated to the new module,
  verified against the two tests below plus the full suite (813 passed, 0 failed).
  `app.py` was ~3,716 lines after step 1. (2) **done, 2026-07-23:** twin projection
  (`_project_twin_event`) moved into `twin_projection.py` (public name
  `project_twin_event`, its own module logger rather than sharing `app.py`'s `_LOGGER`)
  next to `routes_twin.py`. `app.py`'s 1 call site and `test_live_tool_boundary.py`'s
  direct import updated; verified against the crash-window test, `test_decision_identity.py`,
  and `test_live_tool_boundary.py` first, then the full suite (813 passed, 0 failed).
  `app.py` was ~3,696 lines after step 2. (3) **done, 2026-07-23:** the ingest core
  (`_record_pipeline_event`, `_cascade_for_event`, `_record_cascade`, `_same_event_payload`)
  moved into `ingest_pipeline.py` (public names `record_pipeline_event`,
  `cascade_for_event`, `record_cascade`), now that decision-governance and twin-projection
  were already out of its way. `_scenario_drill_wait_seconds`/`_await_worker_cascade`/
  `_resolve_demo_pipeline_cascade` deliberately stayed in `app.py` - they depend on
  `_request_timeout_seconds`, which is genuinely app-wide config (used by the deadline
  middleware itself), not an ingest-core concern, and moving them would have recreated a
  tangle instead of resolving one. 16 `record_cascade(` call sites, 9
  `record_pipeline_event(` call sites, and 2 `cascade_for_event(` call sites in `app.py`
  updated. **Verified with more rigor than the previous two steps, given this was
  explicitly the highest-risk piece:** the crash-window self-heal test and
  `test_decision_identity.py` first (22 passed), then `test_event_store.py` +
  `test_worker_journal.py` + both golden-cascade test files (59 passed), then the full
  suite (813 passed, 0 failed), then — beyond what the previous two steps did — a real
  `docker build` + `docker run` + a genuine `POST /ingest` call against the running
  container, confirming a real event produces `status: accepted`, a real cascade, and a
  decision ID in the correct deterministic format
  (`dec_sa_retail_demo_world_simulation_evt_docker_verify_1`) end-to-end, not just inside
  pytest. **`app.py` is now ~3,623 lines**, down from 3,766 at the start of this
  campaign. All three steps of this sequence are complete.

  **What this unblocks:** `/scenarios/*` (21 routes) and the write half of
  `/connectors/*` can now be extracted as pure route-shuffling — the core logic they call
  (`record_pipeline_event`, `record_cascade`) is a clean import from `ingest_pipeline.py`,
  no longer entangled with app.py-local state.

  **`/mlops/*` blocker also resolved, 2026-07-23:** `_tenant_scoped_decisions`,
  `_decision_belongs_to_other_tenant`, `_reject_cross_tenant_decision_access`,
  `_decision_action`, `_decision_tenant_id` (5 functions, one cohesive concern - decision
  tenant-scoping and cross-tenant access control, zero test-file coupling) moved into
  `decision_access.py` (public names, no underscore). ~22 call sites in `app.py` updated.
  Verified against the tenant-scope/approve/reject-focused test selection first (13
  passed), then the full suite (813 passed, 0 failed). `app.py` is now **~3,578 lines**.
  `/mlops/*`'s 11 routes are now unblocked for the same kind of clean extraction the
  catalog/connectors routers got.

  **`/mlops/*` routes themselves extracted too, 2026-07-23:** all 12 routes
  (`model-runs`, `prompts`, `accountability`, `observability`, `tenant-facts`,
  `consolidate-memory`, `skills/mined`, `skills/mined/{id}/activate`, `skills`,
  `skills/{id}/promote`, `skills/{id}/retire`, `plans/execute`) plus their 4 local
  helpers (`_mined_skill_drafts`, `_learning_outcome_records`,
  `_memory_consolidation_worker`, `_memory_evidence_refs`) and the `SkillPromotionBody`
  model moved into `routes_mlops.py`. This one turned out tractable in the same session
  because, unlike `/scenarios/*`, its routes mostly just aggregate from many `state.py`
  singletons directly - only 4 small, already-simple local helpers, no intricate
  idempotency-key logic. 14 now-unused imports in `app.py` cleaned up via `ruff --fix`
  and verified they were genuinely unused (not silently broken). Verified: mlops/skill
  test selection first (28 passed), full suite (813 passed, 0 failed), manifest regen
  (confirms the glob-based discovery fix from earlier this session picked up the new
  router automatically, no manual list update needed), and - given the size of this
  change - a real `docker build` + `docker run` hitting all 7 GET `/mlops/*` routes
  against the running container, all 200 with correct response shapes. **`app.py` is now
  ~3,156 lines**, down from 3,766 at the start of this campaign (-610 lines, seven
  modules split out total).

  **Still genuinely open, deliberately not attempted this session:** `/scenarios/*` (21
  routes) is the one remaining large group. Read through its full cluster before
  attempting it: it has 11+ of its own helper functions (`_demo_occurrence_suffix`,
  `_demo_event`, `_agentic_cascade_context`, `_demo_catalog_price_event`,
  `_demo_expiry_risk_event`, and more) with genuinely intricate, load-bearing logic - in
  particular `_demo_occurrence_suffix`'s pending-vs-resolved decision-id reuse logic is
  precise and easy to subtly break if rushed. This is a materially larger, more
  intricate extraction than any of the seven done this session and deserves its own
  dedicated read-through and test pass. After each step: run
  `tests/test_event_ingest.py::test_ingest_self_heals_when_the_crash_lands_after_publish_but_before_mark_published`
  and `tests/test_decision_identity.py` specifically before trusting the full suite's
  green as sufficient - those two are the ones that would go red first on a subtle
  regression here, and they're fast enough to run after every single edit, not just at
  the end.
- `/mlops/*` (11 routes total, not contiguous in the file, interleaved with other groups)
  is the largest remaining opportunity by route count but was correctly **not** attempted
  this session — see the point above.
- `/scenarios/*` (21 routes, the largest single group) was surveyed but not started —
  expect deep coupling to `decision_store`, `learning_store`, `world_facts`, `tool_audit`,
  `_record_model_run`, `_cascade_deadline`, `_record_cascade`, `_agentic_cascade_context`,
  and several demo-event builder helpers. This is the highest-value, highest-effort
  extraction remaining; do not start it without budgeting for moving or sharing several of
  those helpers too, the same trap `/mlops/*` set.

### Explicitly deferred (investigated, real reasons not to build them now)

- **Auto-adaptive critic thresholds.** `learning_store.thresholds()` is read by agent
  tools but never consulted by the *deterministic* cascades' hardcoded critic bars
  (`incremental_profit.cents > 0`, `coverage >= 0.60`, etc.). `InMemoryLearningStore`'s own
  docstring calls it "memory layer for the demo's visible learning moment" — a
  proof-of-learning ledger, not a designed control input. Wiring it into the critic bar
  without a real spec for direction/magnitude/safety-floor would risk building a critic
  that gets quietly more permissive over time for the wrong reasons — worse than the
  current honest "records but doesn't yet act on" state. If this is wanted, it needs a
  real design pass, not a mechanical wire-up.
- **Automatic `PlanRunner` compensation execution.** `compensation` on a `PlanStep` is
  validated as required and journaled, but never automatically invoked when a later step
  in the same plan fails. Confirmed (via a new characterization test in
  `tests/test_worker_journal.py`) this matches the documented, current contract exactly —
  every write capability registered today is exercised by single-step plans only, so there
  is nothing yet for automatic rollback to walk back through. Build the rollback executor
  when the first genuinely multi-step write plan is added, not before.
- **Full `/mlops/*` and `/scenarios/*` route extraction.** See above — real prerequisite
  work identified, not done yet. Don't attempt either without first moving
  `_chat_data_domain` (mlops) and doing the bigger `_process_inbound_record` /
  demo-helper untangling (scenarios).

### Standing rule for whoever picks this up next

Every future debt-removal step must close with the same loop this session used: fix →
test → **full suite** → **manifest regen if any file moved** → full suite again. The
capability-discovery bug above is the concrete proof of what happens when that loop gets
shortened to "just run the file I touched." Do not skip the manifest regen step even for
changes that look purely mechanical.

## Phase C break campaign COMPLETE — 2026-07-15

Plan 006's Phase C ran to completion against the real production Compose topology
(Nginx → uvicorn → Postgres → Redis, `APP_ENV=production`, JWT auth) over public HTTP.
Completion report with the capacity table: **`reports/break_campaign_20260715T000000Z.md`**.

- **C1 ramp (new `scripts/phase_c_ramp.py`):** no crash up to **128 concurrent users** — zero
  5xx, zero dropped connections at every step. Sustained accepted-write capacity ~9-10
  events/s on this host (backend capped at 2 CPUs); from 64 users the write rate limiter
  sheds cleanly with 429s. Latency knee at 32 users (p95 ≈ 4.7s, still all-200).
- **C4 races (new `scripts/phase_c_races.py`, 32 threads):** HITL approve/reject and twin
  duplicate observations held. Connector duplicate intake surfaced **two real defects, both
  fixed**: (1) `schema.sql` still carried the 3-column inbound dedup key while the store's
  ON CONFLICT names 4 — every deployed-Postgres intake 500'd; migration ported into
  `schema.sql` and pinned by a static drift test in `tests/test_connectors.py` plus
  write-path coverage in `tests/test_postgres_schema_contract.py`. (2) Concurrent duplicate
  intakes raced the deterministic pkey ahead of the dedup arbiter (`UniqueViolation`,
  2/32 500s); `inbound_store.record()` now treats a concurrent duplicate as a duplicate.
- Data-loss check: 2,200 accepted ramp events == 2,200 Postgres rows. C2 (5-min saturation,
  100% 200s) and C3 (Redis stop / backend restart recovery) were banked 2026-07-14.
- `.env` note: LLM endpoints deliberately point at a dead local port for instant fail-closed
  503s. **Phase D must replace them with the new droplet's :8000/:8001** (comment in `.env`).

**Phase D is unblocked** pending GPU/credit authorization. Runbook: plan 006 Phase D
(D1 sanity + A3 <29s verification → D2 15-min soak with faults/blackout/Postgres → D3
concurrency through the app → D4 30-min endurance only if D2/D3 clean).

## Forensic audit of the full live campaign — 2026-07-14 (night)

The droplet is destroyed; all artifacts persisted locally. A no-sugarcoat forensic audit of the
15m/30m soaks and the concurrency benchmark is in **`reports/soak_audit_20260714.md`** — read it
before citing any headline number from today. Key corrections it makes to earlier framing:
the 504s are NOT warmup/CUDA-graph noise but an SLO arithmetic problem (~19 effective tok/s on
both tiers, dual-model single GPU → any >~500-token response breaks the 30s ceiling at any
concurrency); the timeout middleware sheds clients but not GPU work (sync threadpool routes →
zombie cascades after every 504); the app itself has never been run concurrently (soak is
single-threaded TestClient, benchmark bypassed the app); 88% of route receipts are one endpoint
eating clean generated input; the 30m run replays all 145 of the 15m run's seeds (not
independent); agentic coverage is one-shot and did not scale with duration (12 model calls in
both runs). P0 fixes identified: deadline-aware cascade execution + cancellation of zombie work
— both fixable in app code without a GPU.

## First live MI300X run + stress campaign: two real bugs found and fixed — 2026-07-14 (evening)

**Live run.** Ran the real 15-minute `shelfwise_eval.full_system` shakedown against a freshly
bootstrapped MI300X droplet (both Gemma tiers, dual-model routing). 145 world cycles, 11,600
events accepted, 145/145 chat calls got genuine live model answers, 1,271 approvals/135
rejections with zero HITL mismatches, 360/360 learning movements matched expected. Two findings,
both understood precisely (not just observed and shrugged at):
- `POST /scenarios/golden/agentic` and `POST /scenarios/cold-chain/agentic` consistently hit the
  server's 29s compliance deadline (`SUBMISSION_TIMEOUT_LIMIT_S = 30`) - reproduced identically
  in both a 15-second sanity check and the full 15-minute run, so not a cold-start artifact. All
  six agentic cascades have the same Critic→Executive role-count structure, so this isn't a code
  bug - most likely evidence-payload/response-length variance pushing generation time over
  budget on this droplet's `enforce_eager` (non-cudagraph) vLLM config. Real, documented, not
  fixed (fixing it would mean changing the droplet's inference-serving flags, not app code).
- The "decision reuse" audit failure was traced to the trail and is a **false positive in the
  test harness itself**: the deterministic `/scenarios/procurement` cascade and its agentic
  counterpart correctly converged on the same scenario-stable decision ID (working as designed,
  no duplicate decision minted) - the harness's own audit logic just isn't aware that's expected
  for a deterministic/agentic pair on the same scenario. Not fixed yet; low priority since it's a
  harness precision issue, not an app defect.

**Stress campaign** ("try to break it," at the user's explicit request). Two real, independent
bugs found and fixed, both through actually running load against the code rather than reading it:

1. **`scripts/fleet_scale_shakedown.py` silently under-delivered requested scale.** Requested
   2,000,000 rows; the fleet catalog (`FLEET_SKU_TARGET = 500_000` in
   `shelfwise_worldgen/catalog/generate.py`) can never supply more than 500,000, and
   `islice(..., rows)` just stopped early with zero warning - the CLI's own summary line
   (`wrote ...: 500000 rows`) gave no hint that 1.5M rows were silently missing. Fixed:
   `run_fleet_scale_shakedown` now returns `requested_rows_fully_processed`/`rows_shortfall`
   fields, and the CLI prints an explicit warning and exits 1 on a shortfall instead of a clean
   0. 2 tests (one exercising the real 500k ceiling implicitly via the field values, one via a
   monkeypatched small source for speed).
2. **`TaskWriteBackSink.create_task`/`complete_task` had no lock around a check-then-write
   sequence** - found by code audit while investigating what a concurrent duplicate-approve
   request does, then confirmed structurally (no `Lock` anywhere in the class) - a genuine defect
   regardless of whether one specific test run reproduces it. Honesty note: real concurrent-
   threads stress tests (up to 500 calls / 64 threads / 10 trials, even with
   `sys.setswitchinterval` forced to be extremely aggressive) never actually caught this race
   misfiring under CPython's GIL - so this is a structural fix for a provable design gap
   (relying on GIL timing for correctness is not portable, and free-threaded CPython exists),
   not a bug caught red-handed in the act. Fixed with a `Lock`, matching the pattern already used
   in `InMemoryDecisionStore`. The Postgres-backed `PostgresTaskWriteBackSink` was already safe
   (deterministic hash-derived task ID + `on conflict ... do nothing`), so this only affected the
   in-memory/default backend. New test:
   `test_task_writeback_sink_stays_idempotent_under_real_concurrent_approval_race` (200 calls,
   32 threads) in `tests/test_connectors.py`.

Full suite after both fixes: 618 passed, 6 skipped. Ruff clean. Capability manifest 201/201
(regenerated - `writeback.py`'s source hash changed).

## Full-system harness now covers the whole app, and resets cleanly — 2026-07-14 (later still)

Follow-up to the data-loss fix below: user clarified the upcoming 15-then-30-minute GPU run must
exercise "everything this application is connected to... at full capacity," using synthetic data
that is "easy to remove after." Checked both halves against `src/shelfwise_eval/full_system.py`
and found real gaps in both:

**Coverage gap.** The harness's probes predate this session's newer subsystems - zero coverage of
the digital twin, edge device ingestion, candidate lifecycle history, connector poll status, or
the catalog endpoints. Added four new probe methods (`_probe_operational_twin_and_edge`,
`_probe_candidate_lifecycle`, `_probe_connector_poll_and_catalog`) exercising: twin onboarding +
observation intake + store/snapshot/fidelity reads; a real HMAC-signed edge observation batch
(same signing code path as production); a candidate's full observed -> suppressed lifecycle via
its history endpoint; and the connector-poll-status/catalog-products reads. Registered as five new
`SUPPORT_FEATURES` and their routes in `REQUIRED_ROUTE_RECEIPTS`, so `report.passed` now actually
requires all of them to pass, not just the original golden/procurement/sales rotation. Verified
with a real local 30-second dry run: feature receipts went from 20 to 25, all five new ones
`passed: true`.

**"Easy to remove after" gap.** `_reset_in_memory_state()` (the function that wipes in-memory
state between/after runs) predates most of the stores the app now has - `candidate_store`,
`chat_store`, `open_order_store`, `inventory_position_store`, `connector_cursor_store`,
`world_snapshot_store`, `edge_device_registry`, and the twin's internal store/calibrations/
onboarding-manifest registries were all silently left un-cleared. Fixed `_load_runtime()` to
expose them and added them to the reset list. Verified with a dedicated test that populates every
one of these stores directly, runs the reset, and asserts each is empty afterward - not asserted
from reading the code, actually exercised.

Note: `_reset_in_memory_state` only ever ran for `SHELFWISE_STORE_BACKEND=memory` (unchanged,
correct - it must never auto-wipe a real Postgres database). For a droplet run using Postgres,
"easy to remove after" is the existing `persist/` bind-mount directory: stop the compose stack,
`rm -rf persist/`, done - no new mechanism needed there.

4 new tests, full suite 616 passed, Ruff clean, capability manifest unchanged (eval tooling, not
an app route).

## Fixed the real cause of lost soak-run data, before the next live GPU session — 2026-07-14 (later still)

User is about to create a real MI300X droplet ($6.17 AMD credit, 25-day expiry) to run a 15-minute
full-application shakedown, then decide on 30 minutes - and asked to make sure data isn't lost
this time. Investigated why `reports/` already had `soak_final`, `soak_final2`, `soak_final3`,
`soak_final4`, `soak_diag`, `soak_15m_retry`, `soak_postfix_final` sitting around - all but two
(`reports/soak` and `reports/soak_15min_20260711T042648Z`, the one documented "known-good" run)
have a `cycles.jsonl` with only 3 lines, meaning they crashed almost immediately.

Found the real bug in `src/shelfwise_eval/full_system.py`: `manifest.json` and every other
summary artifact (`feature_receipts.json`, `route_receipts.json`, `learning_events.json`,
`chat_samples.json`) were written exactly once, only after every probe phase in `run()` completed
successfully. Only `decision_trail.jsonl`/`cycles.jsonl` were written incrementally per cycle. So
any interruption - SSH drop, droplet timeout, Ctrl+C, an unhandled exception - meant the entire
summarized report was lost, leaving only a raw, unsummarized trail. This is exactly the pattern
that produced the pile of retry directories: each crash meant starting over with no report to
diagnose from.

Fixed two things:
1. `_FullSystemDriver.run()` now writes a best-effort `manifest.json` (and the other artifacts)
   on every exit path, including an exception or `KeyboardInterrupt` - verified with a real test
   that monkeypatches a probe phase to raise mid-run and confirms `manifest.json` still exists,
   `passed: false`, and totals reflect everything accumulated before the interruption.
2. Reusing an `--output-dir` that already has a `manifest.json` now fails fast
   (`FileExistsError`) instead of silently truncating the previous run's `decision_trail.jsonl`/
   `cycles.jsonl` - exactly the directory-name-collision pattern visible in the `soak_final*`
   history. New `--overwrite-artifacts` flag for the rare case that's actually intended.

Updated `docs/mi300x-recreate-runbook.md`'s Application Shakedown section: timestamped
`--output-dir` for every run (no more static `reports/soak_15m`), explicit "run 15 minutes,
inspect the result, then decide on 30 minutes" sequencing matching what the user asked for, and
both commands ready to copy-paste.

Verified for real, not just via unit tests: ran a local 20-second dry run end to end
(`SHELFWISE_STORE_BACKEND=memory`, no live model), confirmed a second run against the same
directory was refused with the new clear error, and confirmed
`scripts/validate_full_system_artifact.py` accepts the completed run's manifest cleanly. 3 new
tests (`tests/test_full_system_harness.py`). Full suite: 615 passed, Ruff clean, capability
manifest unchanged (this is eval tooling, not an app route).

Did not touch the existing `reports/soak_final*`/`soak_diag`/etc directories - they're evidence
of the bug just fixed, not something to clean up unilaterally; left for the user to decide.

## Training data matrix expanded (harness code only, no GPU run) — 2026-07-14 (later still)

Third item from the earlier "still external" list: "full training matrix." Confirmed the real
environment boundary first - actual training runs on the separate ROCm/Jupyter pod
(`docs/model-training.md` says so explicitly), which this session has no access to. Asked the
user what "expand" should mean given that; agreed scope: expand the harness code/config only,
verify via local dry run, no GPU training attempted.

Found the training data generator (`src/shelfwise/training/simulation.py`) only ever produced
procurement/delivery-exception scenarios (12 case types) - the model has never been trained on
the decision domains that generate most of this app's real recommendations today (expiry
markdown, cold-chain escalation, price-integrity guardrails). Added three case types (`expiry
markdown decision`, `cold-chain temperature breach`, `price integrity mismatch`), each wired into
`_case_type`/`_risk_level`/`_expected_output`/`EVIDENCE_BY_CASE`, and registered the three new
`case_type` values in `dataset.py`'s `VALID_CASE_TYPES` strict allowlist (missed this the first
pass - the dry-run tests caught it immediately). Verified with a real
`build_shakedown_datasets()` dry run: all three appear in the case breakdown at the expected
round-robin frequency, not just present in source. 3 new assertions in
`tests/test_shakedown_pipeline.py`; training harness suite 13/13.

## Automatic connector poll scheduling implemented — 2026-07-14 (later still)

Wired up the piece flagged earlier today as a genuine gap: nothing ever called the already-built
`PollingConnector.pull()`. Investigating why surfaced the real blocker - Odoo/SAP/SYSPRO
connectors need real per-tenant credentials (`base_url`, API keys) to construct, and no storage
for those existed anywhere. Asked the user how to handle credentials rather than inventing a
security-sensitive design unilaterally; chosen: env vars, single-tenant, matching how
`LLM_ROUTINE_BASE_URL` already works (this deployment is genuinely single-tenant today).

Built:
- `src/shelfwise_backend/connector_poll_service.py` - `ConnectorPollService`, a lifespan-managed
  background loop (mirrors `WorkerLoopService`'s start/stop/status pattern) gated by
  `CONNECTOR_POLL_ENABLED` (off by default). Polls whichever of Odoo/SAP/SYSPRO has its full env
  credential set present; a partially-configured system is skipped, not polled broken. Each
  yielded record goes through `app.py`'s existing `_process_inbound_record` (injected as a
  callback to avoid a backend->app.py circular import), off the event loop thread via
  `asyncio.to_thread` so a slow poll can't stall other requests.
- `PostgresCursorStore` in `src/shelfwise_connectors/connectors/poll.py` - durable poll-position
  persistence (new RLS-protected `shelfwise_connector_cursors` table) so a restart resumes
  instead of re-fetching a system's entire history. Verified with a direct round trip against a
  throwaway Postgres container: set a cursor, construct a *fresh* store instance (simulating a
  process restart), confirm it reads back the same cursor.
- `GET /connectors/poll/status` route; added to the frontend's endpoint registry and confirmed
  rendering live (`connected`) in the Operations workspace via the Browser pane, not just assumed.
- `.env.example` documents all 9 new vars (`CONNECTOR_POLL_ENABLED` +
  `SHELFWISE_CONNECTOR_{ODOO,SAP,SYSPRO}_*`).

9 new tests (`tests/test_connector_poll_service.py`, `tests/test_connector_poll_status_api.py`,
plus a `PostgresCursorStore` assertion added to `test_postgres_schema_contract.py`). Full
verification: 613 passed (1 known-flaky async worker-loop test re-confirmed in isolation), Ruff
clean, capability manifest 201/201, frontend typecheck/build clean, Playwright E2E 3/3.

Explicitly not built at the time this entry was written: real multi-tenant encrypted
credential storage - scoped out as its own future decision. Built later, 2026-07-23, per an
explicit user directive to stop deferring; see "Multi-tenant encrypted connector
credentials — BUILT" at the top of this file.

## Redis image CVE fix — 2026-07-14 (later still)

User flagged a Docker image scanner finding (CVE-2025-60876, medium 6.5) on the `redis:7-alpine`
image used by both compose files. Corrected an inaccurate first read of the CVE (initially
described as an `apk`/APKINDEX heap overflow) - it is actually a BusyBox `wget` HTTP
request-splitting bug. Confirmed no Alpine fix exists yet by re-pulling the exact same digest
already running (`6ab0b6e73817`, Alpine 3.21, busybox `1.37.0-r14`) - still unfixed. Real fix:
swapped both compose files to `redis:7-bookworm` (Debian-based, ships no BusyBox at all, so the
CVE is structurally impossible, not suppressed). `docker scout` shows the bookworm variant has
more total findings (1 critical + 2 high, all in `perl`, all also unfixed upstream) - noted
honestly rather than presented as a clean win, but none of those packages are ever invoked by the
`redis-server` process itself, same real-world exposure as what was replaced. Verified: both
compose files validate, `redis-cli ping` passes against the new image, and the full production
topology (Postgres/Redis/migrate/backend/frontend) came up healthy and passed the same
session/`/scenarios/golden` smoke this session's earlier readiness pass used.

## Candidate history and Playwright E2E implemented — 2026-07-14 (later same day)

The prior readiness pass below correctly identified candidate history/partitioning and browser
E2E as genuine gaps, not bugs - but on request, both are now actually implemented, not just
scoped:

- **Candidate history**: `src/shelfwise_backend/candidate_history.py` - append-only lifecycle
  transitions (observed, status_changed, suppressed, linked_decision) per candidate, memory +
  Postgres (RLS-protected `shelfwise_candidate_history`), `since`/`until` bounded queries. Wired
  into both `CandidateStore` implementations; new route `GET /candidates/{candidate_key}/history`;
  added to the frontend's `OPERATION_READ_ENDPOINTS` registry. 10 new tests (unit, API, and a real
  round-trip against a throwaway Postgres container via the established
  `MSYS_NO_PATHCONV=1 docker cp`/`psql` pattern). Capability manifest: 200.
- **Playwright E2E**: `frontend/playwright.config.ts` + `frontend/e2e/golden-path.spec.ts`. Three
  real tests, all verified passing against the actual running app (not asserted from code
  reading): console loads with chat input/approval-queue affordances; approving the seeded golden
  decision through the real UI clears the queue and logs the outcome (the exact flow
  `DEMO_RUNBOOK.md`'s three-minute story drives - confirmed by manually driving it once first via
  the Browser pane to get real selectors before writing the test, not guessing them); a direct
  chat question returns a real non-empty grounded answer. `npm run test:e2e` runs it locally;
  wired into CI right after the frontend build step with a report-artifact upload on failure. The
  Python executable path is resolved portably (prefers this repo's `.venv`, falls back to
  `python`/`python3` for CI where there is no venv) rather than hardcoded to one OS.
- Full verification after both: backend `605 passed, 6 skipped`, Ruff clean, capability manifest
  200/200, frontend `typecheck`/`build` clean, Playwright suite `3 passed`.

## Pre-testing readiness pass — 2026-07-14 (later same day)

Went through `IMPLEMENTATION_STATUS.md`'s "Still External / Not Claimable Yet" list item by item -
fixed what's actually fixable locally, verified the exact tooling needed tomorrow works, and left
alone what's a deliberate design posture or a genuinely large future feature (never rushed a
shallow version of either). Full detail is in `IMPLEMENTATION_STATUS.md`'s matching section;
summary:

- Confirmed both Docker images build clean for `linux/amd64` (`docker image inspect` verified
  `architecture=amd64`) - publishing to a public registry itself needs the user's own registry
  credentials, so that step is documented, not performed here.
- Actually brought up the full `docker-compose.production.yml` stack locally (Postgres, Redis,
  migrate, backend, frontend - all reached `Healthy`) and ran the exact CI smoke sequence by hand
  (session creation, `/scenarios/golden`, all response assertions) - passed clean against the real
  running stack, confirming the compose file and Nginx routing are genuinely correct right now.
  Cleaned up the stack and the host-mounted `persist/` directory it created afterward.
- Checked the configured MI300X droplet endpoints in `.env` - genuinely two distinct
  routine/strong ports and model IDs, but the droplet is not currently running (expected
  idle-cost behavior, not a bug) - confirmed via a direct, read-only reachability check.
- Validated the concurrency benchmark config offline (`--validate-config` → 11 agents, 4
  strategies, all valid) and added the missing 1/8/32 sweep loop to
  `docs/mi300x-recreate-runbook.md` (previously only had a `--peak 32` example).
- Re-verified every connector's catalog transport claim against its actual implementation class -
  all correct now (Lightspeed was the only mismatch, already fixed earlier today).
- Re-ran the training harness tests (7 passed) to confirm no regression from today's other
  changes.
- Confirmed Playwright/browser E2E is a genuine, currently-absent gap (no dependency, no script in
  `frontend/package.json`) - correctly left as a real future task rather than bolted on tonight.

## Digital twin plan audit: four real bugs found and fixed — 2026-07-14

Read `DIGITAL_TWIN_RESEARCH_AND_IMPLEMENTATION_PLAN.md` (all 44 sections, twice - once
categorizing narrative vs. technical claims, once tracing every specific technical claim into
the actual running code) and fixed every real bug that read-through surfaced, not just the ones
that were quick. Full detail, reproduction steps, and file/line references live in that document's
own dated audit entries; this is the summary for anyone picking this branch up next.

1. **Lightspeed connector capability mismatch.** `src/shelfwise_connectors/catalog.py` declared
   `transport="webhook_or_poll"` for Lightspeed; only a webhook receiver exists in
   `connectors/systems/lightspeed.py` (unlike SAP/Odoo/SYSPRO, which have real poll
   implementations). Corrected to `"webhook"`. Verified live via `GET /connectors/systems`.
2. **Twin projection hash was not actually deterministic across replay.** The hash
   `src/shelfwise_twin/service.py::snapshot()` computes for replay/recovery verification
   included `TwinEntity.created_at`, `TwinRelationship.valid_from`, and
   `TwinPropertyState.projected_at`/`confidence`/`freshness` - all stamped from wall-clock `now`
   at projection time. Replaying the identical event log at a different real time produced a
   different hash even with zero substantive state change, directly contradicting the "replay,
   recovery" claim the hash exists to support. Fixed by excluding those fields from the hashed
   canonical JSON (they remain present and correct in every API response that returns the
   actual objects - only the hash computation changed).
3. **Onboarding-created twin topology could not survive a real recovery scenario.**
   `POST /twin/onboarding` wrote entities/relationships straight into the twin projection store
   and never touched the durable event log, so `/twin/stores/{id}/bootstrap` (which replays only
   `operational_twin` events - the mechanism this document's Definition of Done means by "restart
   preserves the projection hash") could not reconstruct onboarded fixtures or the onboarded
   store's own display name/attributes; a real projected-state loss would have silently reverted
   them to generic defaults. Fixed by adding `src/shelfwise_twin/onboarding_store.py`
   (`OnboardingManifestRegistry`, memory + Postgres, mirroring the existing `calibration.py`
   pattern) - `TwinService.onboard()` now persists the manifest there, and
   `TwinService.bootstrap_events()` replays it before replaying events. New Postgres table
   `shelfwise_twin_onboarding_manifests` with RLS.
4. **Agentic chat reported the wrong model in its own response metadata.** `role="chat"` is not
   one of the routed agent roles in `src/shelfwise_backend/tools/model_runtime.py`, so it always
   falls through to the hybrid architecture's routine default - but
   `chat.py::build_chat_reply_with_meta` unconditionally set `meta["model"]` to the strong-model
   ID before either the agentic or non-agentic path had even run. Every agentic chat response (the
   primary path whenever `decisions`/`memory` are supplied - true in production) recorded the
   wrong model in its own audit trail. Fixed so both paths report whichever model the run that
   actually produced the answer used (`AgentRunResult.model_calls` for the agentic path,
   `InferenceResult.model` for the fallback).

Each fix has a dedicated regression test (`tests/test_twin_api.py`,
`tests/test_chat_model_metadata.py`) that reproduces the original failure and proves the fix,
not just an assertion that a symptom is gone. Capability manifest regenerated (199 capabilities)
for the new `onboarding_store.py` module. Full suite: **595 passed, 6 skipped**; Ruff clean.

No other bugs are known as of this pass. The rest of the plan document's findings (Section 6's
open "continuous synchronization" connector-scheduling gap, Section 9's onboarding manifest being
far simpler than its 13-file spec, Section 21's remaining Definition-of-Done gaps - historical
time-travel reconstruction, Store Twin UI-driven fidelity display, a consolidated onboarding
receipt pack - and Section 30's file-tree divergence) are real, precisely-located, but are feature
gaps and product/UI decisions, not bugs; they are recorded in the plan document as follow-up work,
not silently built mid-audit.

## Deployment reproducibility update — 2026-07-13

- The authoritative fresh-droplet path is `DROPLET_BOOTSTRAP.md` plus
  `scripts/bootstrap_mi300x_vllm.sh`. The script resolves `/opt/shelfwise` from its own location,
  validates host tools, ports, ROCm devices, secrets, and `VLLM_ALLOWED_CIDR` before downloading
  weights, and does not depend on the operator's current directory.
- After authenticated `/v1/models` checks pass for both tiers, it writes the secret-free
  `/root/shelfwise-mi300x-bootstrap.json`. Keep that file with the exact Git commit and the public
  application shakedown receipt; it is the deployment handoff proof for model identity, ports,
  allowlist, vLLM version, and readiness.
- A timeout now prints the correct Quick Start vLLM log for the failing port. The historical
  `docker start rocm` snippets remain recovery commands for an existing container only; do not
  use them as the fresh-droplet install path.

## Frontend/system bug audit pass (2026-07-12, this session)

Goal: act as a debugger, find and fix real bugs across frontend + backend, no redesign,
no hardcoded/cached answers (evaluation uses unseen variants).

Confirmed and fixed:

1. **Duplicate approval-queue notifications (the reported symptom).** `src/shelfwise_backend/app.py`
   `_demo_event` / `demo_recall` / `demo_inventory_exception` minted a fresh random `uuid4()` suffix
   on every call, so every click of a demo trigger (or every reload that replays it) created a brand
   new pending decision for the identical underlying scenario - the approval queue filled up with
   near-identical "Apply 20% markdown ... Selati Flour Low Fat" cards (verified live: 3 repeated
   `POST /scenarios/golden` calls produced 4 separate pending decisions before the fix). Fixed by deriving
   the event id deterministically from `(tenant, event_type, sku, day)` via a new
   `_demo_occurrence_suffix()` helper that reuses a still-pending decision's id (dedupe) but advances
   to a new occurrence once the prior one is resolved (approved/rejected) - so a new day's scan or a
   fresh incident after resolution is still a genuinely new decision, matching
   `tests/test_golden_cascade.py::test_demo_golden_read_does_not_reset_resolved_decision`. Verified
   live: repeated calls now collapse to exactly one decision per demo trigger type.
2. **Stale field names in the Products search receipt.** `frontend/src/App.tsx`'s "Search receipt"
   panel read `source_counts.synthetic_scanned` / `synthetic_scan_budget` / `synthetic_total_estimate`
   / `seed` / `synthetic_catalog` - all left over from the old CSV-seed + synthetic-catalog-blend
   design. The real `/products/search` response only ever returns `source_counts.generated_world`
   (the generated world is the whole catalogue now, no separate synthetic layer), so the panel always
   showed "0 rows scanned" / "0 seed matches · 0 catalogue matches" even when real results came back.
   Fixed to read the real field and reworded the two receipt rows honestly ("Generated-world scan" /
   "Query"). Updated `tests/test_frontend_attention_contracts.py` and
   `tests/test_frontend_product_contracts.py` to match the corrected copy/field name.
3. **"To order" workspace only ever showed 0-1 products despite the sidebar badge saying "16
   products".** `renderToOrder` in `frontend/src/App.tsx` built its list solely from
   `intel.store_intelligence.supplier_cover` (the single hero-SKU object), never from the real
   per-tenant `ops.productAttention.to_order` array the backend already returns (confirmed via
   `GET /products/attention`: `to_order` has 16 real rows, matching `totals.to_order_products`).
   Fixed to render `apiToOrderItems` (the real list) first, falling back to the single
   supplier-cover line only when that list is empty - mirroring the working pattern already used
   by "Sell first" (`apiSellFirstItems` over the single `batch` object). Verified live: "To order"
   now lists all 16 real products instead of one stale line.
4. **One store's catalogue mixed six competing SA retail chains' own private labels together.**
   `src/shelfwise_worldgen/catalog/brands.py`'s `PRIVATE_LABEL` pool appended *every* major SA
   supermarket chain's house brand (PnP, Checkers Housebrand, Shoprite, Woolworths, SPAR, Boxer) to
   every category's brand pool - so one store's shelf showed products from six competing retailers'
   own labels simultaneously (reported as "products from different shops... we didn't give the data
   much focus"). No real store stocks a competitor's private label. Fixed by splitting the pool into
   chain-neutral generics ("No Name", "Ritebrand") plus exactly one retail-chain house brand chosen
   deterministically per world seed (`house_brand_name(seed)` / `private_label_pool(seed)`), threaded
   through `pool()`, `generate_catalog()`, and `count_estimate()`. Verified: for the demo seed
   (20_260_710) the house brand is "Boxer"; a live `/products/attention` scan now shows only
   `Boxer SA (Pty) Ltd` as the chain-brand supplier, alongside real manufacturer brands (Clover,
   Tastic, Ace, etc.) and chain-neutral generics (No Name, House, Select, Ritebrand, Premium
   Choice) - never a mix of Woolworths+Shoprite+SPAR+PnP+Checkers at once. Updated
   `tests/test_catalog_worldgen.py`'s `count_estimate` calls for the new `(seed, scale)` signature.
5. **Deliveries workspace had no drill-down** - the one delivery-exception row showed only
   ordered/received/accepted/short-dated units and a "short" count, with no way to see ASN quantity,
   over-delivery, rejected units, or the supplier fill rate (all already computed by the backend's
   `delivery_reconciliation`, just never surfaced). Reported as "when there is an issue with delivery
   you can't click the thing you're supposed to click to see what is really happening... that last
   information you need to see." Fixed by making the row clickable (same `active`/`onSelect`
   pattern the Products workspace already uses) to reveal a "Reconciliation detail" panel with the
   remaining fields. Verified live: clicking the delivery row now expands ASN vs. receiving detail
   and the supplier fill rate (67% in the running demo).
6. Regenerated `capabilities/manifest.json` after each frontend/backend change (no route/tool shape
   changed - just the id-generation helper, workspace rendering, and worldgen brand-pool logic).

Historical snapshot from the 2026-07-12 frontend pass, superseded by the current verification
baseline below: 454 tests passed, 3 skipped; Ruff and TypeScript were clean; the running app was
manually driven in-browser with no console errors.

Not yet done / lower priority: full line-by-line review of the rest of `App.tsx` (3300+ lines) beyond
the workspaces actually exercised above; a wider audit could still turn up more latent issues if asked
to continue.

## CURRENT UPDATE — disposable-droplet recovery and frontend pass — 2026-07-12

Read this section before continuing. The worktree contained active application/frontend changes
when this recovery pass began. They were preserved, tested, and are intended to be saved on
`developers`; do not reset or discard them.

### New MI300X provisioning path

- `scripts/bootstrap_mi300x_vllm.sh` is the authoritative new-droplet command. It requires a
  user-supplied Hugging Face token with accepted Gemma licences and a vLLM API key, uses the
  provider's preinstalled Quick Start container when present (otherwise pulling the pinned
  official Gemma 4 ROCm vLLM image), starts E4B routine on `8000` and 31B strong on `8001`, and
  blocks until both authenticated `/v1/models` responses prove the intended models are loaded.
- `DROPLET_BOOTSTRAP.md` contains the exact clone, secret, firewall, application configuration,
  and Track 3 prescreen sequence. Do not use the historical `docker start rocm` commands for a
  newly created droplet; those only apply to the old pre-existing container.

### Recovery setup now implemented

- `scripts/session_capsule.py` creates a safe recovery capsule and archive. It captures Git HEAD,
  status, staged/unstaged binary diffs, untracked files, redacted environment metadata,
  Python/pip, Docker, ROCm/GPU, apt, and systemd diagnostics, PostgreSQL/Redis exports when
  configured, the complete `SHELFWISE_PERSIST_ROOT` contents except the capsule itself, training
  runs/adapters, harness runs, generated data, uploads, logs, reports, and SHA-256 checksums.
- The command has `create`, `verify`, and guarded `restore` subcommands. It never destroys a
  Droplet or deletes an existing restore target. `--strict` fails when configured DB exports fail.
- `src/shelfwise_runtime/paths.py` centralizes durable paths. Training, evaluation, benchmark
  reports, and full-system harness artifacts honor the persistence root when configured.
- Both Compose files bind Postgres, Redis, and `/app/persist` below
  `${SHELFWISE_PERSIST_ROOT:-./persist}` instead of relying only on anonymous Docker volumes.
  Redis AOF is enabled in the local Compose profile too.
- Accepted voice/image uploads are content-addressed into `UPLOAD_DIR` when configured; the API
  returns a safe `upload_ref`, never a machine path.
- `.env.example` documents `/workspace/persist` for durable state and `/scratch` for rebuildable
  Hugging Face, Torch, Triton, and temporary caches.

### Exact capsule commands on the Droplet

Run these before destroying or powering down the disposable environment. Do not run a destroy
command from the application; destruction remains a manual, separately approved operation.

```bash
export SHELFWISE_PERSIST_ROOT=/workspace/persist
export TRAINING_OUTPUT_DIR=/workspace/persist/training
export HARNESS_RUN_DIR=/workspace/persist/harness/runs
export TRACE_DIR=/workspace/persist/runtime/agent-traces
export EVENT_STORE_DIR=/workspace/persist/runtime/events
export UPLOAD_DIR=/workspace/persist/application-data/uploads
export LOG_DIR=/workspace/persist/logs
export HF_HOME=/scratch/huggingface
export TORCH_HOME=/scratch/torch
export TRITON_CACHE_DIR=/scratch/triton
export TMPDIR=/scratch/tmp

python scripts/session_capsule.py create \
  --repo /workspace/shelfwise \
  --root /workspace/persist \
  --strict \
  --archive /workspace/persist/capsules/shelfwise-session-$(date -u +%Y%m%dT%H%M%SZ).tar.gz
```

The command must exit successfully and print an empty `failures` list. Verify the capsule before
downloading it:

```bash
python scripts/session_capsule.py verify /workspace/persist/capsules/shelfwise-session-<timestamp>
sha256sum /workspace/persist/capsules/shelfwise-session-<timestamp>.tar.gz
```

Only after API/training shutdown, database dumps, Redis persistence, capsule creation, checksum
verification, download, and local checksum verification have succeeded may the Droplet be
destroyed. Restore into a new MI300X with:

```bash
python scripts/session_capsule.py restore shelfwise-session-<timestamp>.tar.gz \
  --target /workspace/recovery
```

Then restore Postgres from `databases/postgres.dump` with `pg_restore` and restore Redis by
placing `databases/redis.rdb` in the configured Redis data directory before starting Redis.
Inspect the restored manifest and rerun application health, database row-count, latest decision/
event ID, checkpoint readability, harness receipt, frontend connectivity, and AMD inference
checks before resuming training. Do not resume training after a failed recovery check.

### Frontend/application fixes in the current worktree

- Operations lists now use generated product names and per-product delivery reconciliation,
  rather than displaying only a hero SKU or a single delivery exception.
- The attachment control advertises only implemented image and voice endpoints. PDF is no longer
  offered as if it were supported.
- Attachment failures show a safe user-facing message instead of raw exception text.
- The backend persists accepted media when `UPLOAD_DIR` is configured.
- Demo trigger IDs deduplicate repeated pending clicks but create a new occurrence after a prior
  decision is resolved.
- Chat offline delivery answers and the live delivery tool use the same generated-world data.
- Current verification: `454 passed, 3 skipped`; Ruff clean; frontend typecheck and build pass.

### Save state before credit exhaustion

- Current branch is `developers`; only `main` and `developers` should remain.
- The implementation and handoff are saved and pushed in commit `c475d5b`. Only the existing
  untracked run artifacts remain; do not stage them unless intentionally packaging evidence.
- Before the next cloud run, create the capsule and keep the archive off the Droplet.
- Remaining external blockers are public `linux/amd64` image publication, actual AMD cloud
  startup/latency receipt, and final merge to `main` after those proofs. Do not claim these are
  complete from local tests.

### Concurrent uncommitted work requiring explicit follow-up

`src/shelfwise_backend/agentic_cascade.py` currently has an uncommitted partial change adding
expiry/price scenario imports and Critic/Executive schemas. It does not yet add the corresponding
agentic runner functions, routes, result builders, or regression tests, and Ruff currently reports
unused imports for those additions. Do not stage it as complete. The next implementation pass
must either finish both agentic conditional checks end-to-end and add live-required tests, or
intentionally revert only that partial change after confirming it is not needed.

## Active Objective — Track 3 Prescreen

Track 3 requires all of the following:

1. A Docker image is present in the GitHub repository.
2. Container images are publicly pullable at submission time and include a `linux/amd64`
   manifest. A local image or a private registry image is not sufficient.
3. The deployed application demonstrably uses AMD compute. The production path must use
   AMD vLLM (`provider=vllm_mi300x`), not Fireworks or offline fallback.
4. The container is ready within 60 seconds after images are built.
5. Every request returns within 30 seconds.
6. Model responses are in English.
7. Answers are generated for unseen variants; there is no question-to-answer cache or
   hardcoded answer table.

The repository-side implementation is complete and locally verified. The dual-tier cloud benchmark
now proves model execution on the AMD host, but Track 3 still needs public image pullability and a
public-origin deployment receipt proving items 2-4 together. Do not mark the objective complete
until that receipt exists.

## Current Git State

- Current branch: `developers`
- Remote branch: `origin/developers`
- `main` has not been merged from the current work.
- Latest committed change before this handoff update: `4ee1abc fix: report submission proof boundaries honestly`.
- Existing untracked run artifacts are intentional evidence and must not be deleted or staged
  casually: `reports/`, `full_capacity_v2.log`, `backend_verify.log`, `tmp/`, dated run folders,
  `shelfwise-gemma-final-adapter/`, and stress-run folders.
- This update also adds two new files that must be committed: `scripts/track3_prescreen.py`
  and `tests/test_track3_prescreen.py`.

## Implemented Track 3 Gates

### Docker

- Root `Dockerfile` builds the backend as a non-root `appuser` and contains a Docker
  `HEALTHCHECK` against `/health`.
- `frontend/Dockerfile` builds the production frontend image.
- `docker-compose.production.yml` defines Postgres, Redis, migration, backend, and frontend;
  only the frontend publishes a public port.
- `/submission/readiness` now reports `docker_image_required: required`.
- The evaluation harness enforces that same value.
- Local image build was completed successfully:
  - `amdactii-backend:latest` (~402 MB)
  - `amdactii-frontend:latest` (~75.7 MB)
- Those local images are **not submission-ready**: they are not publicly pullable and their
  pushed registry location has not yet been assigned. Do not claim this requirement is done.
- The judging VM is `linux/amd64`. Every published image must be built and pushed with an
  explicit `linux/amd64` platform and must expose an amd64 manifest.
- Local Compose configuration passed. Local Compose services were not left running.
- CI builds images first, then measures `docker compose ... up --build -d --wait` and fails if
  readiness takes 60 seconds or more.

### AMD inference

- Production `APP_ENV=production` rejects providers other than `vllm_mi300x` with HTTP 503.
- Production chat requires live inference and cannot use offline fallback.
- All four agentic demo routes force `LIVE_REQUIRED` in production even if a caller requests
  `live_required=false`.
- Production inference smoke also requires AMD vLLM.
- Default production model configuration is routine `google/gemma-4-E4B-it` and strong
  `google/gemma-4-31B-it`; distinct endpoints/model IDs are required for
  `ready_for_amd_demo=true`.
- The model contract is OpenAI-compatible vLLM with Gemma tool calling. The application does
  not run local models and must not claim local GPU evidence as AMD evidence.

### Latency

- `LLM_TIMEOUT_SECONDS` is bounded by the remaining request budget.
- `SHELFWISE_REQUEST_TIMEOUT_SECONDS` defaults to 120 seconds; its outer middleware deadline
  protects the service while per-call inference budgets fail closed earlier.
- HTTP middleware returns 504 at the whole-request deadline, including multi-call agentic
  requests.
- CI measures post-build production topology readiness under 60 seconds.
- No actual cloud request latency receipt exists yet; the AMD run below is mandatory.

### English output

- Chat prompts explicitly require English.
- Chat output rejects clearly non-Latin responses.
- Agentic JSON payloads are recursively checked for clearly non-English writing systems.
- This is an enforcement guard, not a substitute for the final live response receipt.

### Unseen inputs and caching

- Chat persistence is keyed by `(tenant_id, user_id, conversation_id, message_id)`.
- Replay is limited to the exact message ID for idempotent retries.
- A different message ID does not replay an earlier answer; regression coverage is in
  `tests/test_track3_contract.py`.
- No question-to-answer cache exists.
- Production responses include `X-ShelfWise-Replayed`, correlation, provider, model, and answer
  source headers.
- Generated-world facts are data snapshots, not cached model answers.

## New Cloud Prescreen Command

`scripts/track3_prescreen.py` is the authoritative end-to-end probe. It performs the following:

- polls `/health` for at most 60 seconds;
- checks `/inference/readiness` for AMD vLLM and Google Gemma 4 routine/strong models;
- creates a session through `/auth/session`;
- sends two fresh unseen chat questions with unique conversation/message IDs;
- allows each chat response the documented live-model request budget (130 seconds in the
  prescreen, above the 120-second application deadline);
- requires `X-ShelfWise-Provider: vllm_mi300x`, `X-ShelfWise-Answer-Source: model`, and
  `X-ShelfWise-Replayed: false`;
- requires English-compatible output and unique correlation IDs;
- writes a JSON receipt when `--output` is provided.

Run it only after the AMD endpoint and production application are live:

```bash
python scripts/track3_prescreen.py \
  --base-url https://<public-app-origin> \
  --startup-deadline 60 \
  --request-deadline 130 \
  --output reports/track3_prescreen_<timestamp>.json
```

Expected result is `"verdict": "PASS"`. A configuration/readiness response without this
receipt is not proof of cloud startup or response latency.

## Public Image Packaging — Required Before Submission

This is a separate blocker from local Docker build success. Choose a public registry and a
stable public image namespace before submission. Do not put registry credentials in the repo.
For GHCR, the package visibility must be changed to **public** after the first push.

Build and publish both production images explicitly for the judging VM architecture:

```bash
export IMAGE_NAMESPACE=ghcr.io/<public-owner>/shelfwise
export IMAGE_TAG=<immutable-submission-tag>

docker buildx build \
  --platform linux/amd64 \
  --tag "$IMAGE_NAMESPACE-backend:$IMAGE_TAG" \
  --push .

docker buildx build \
  --platform linux/amd64 \
  --tag "$IMAGE_NAMESPACE-frontend:$IMAGE_TAG" \
  --push ./frontend
```

Verify the manifest and public pullability from a clean environment before submitting:

```bash
docker buildx imagetools inspect "$IMAGE_NAMESPACE-backend:$IMAGE_TAG"
docker buildx imagetools inspect "$IMAGE_NAMESPACE-frontend:$IMAGE_TAG"
docker pull --platform linux/amd64 "$IMAGE_NAMESPACE-backend:$IMAGE_TAG"
docker pull --platform linux/amd64 "$IMAGE_NAMESPACE-frontend:$IMAGE_TAG"
docker image inspect "$IMAGE_NAMESPACE-backend:$IMAGE_TAG" \
  --format '{{.Architecture}}'
docker image inspect "$IMAGE_NAMESPACE-frontend:$IMAGE_TAG" \
  --format '{{.Architecture}}'
```

Required evidence to save in the handoff/submission folder:

- public backend image reference and immutable tag;
- public frontend image reference and immutable tag;
- `imagetools inspect` output showing `linux/amd64`;
- clean `docker pull --platform linux/amd64` output;
- registry visibility confirmed without credentials;
- the exact image references used by the production Compose deployment.

Do not use `--load` as the final publication step. `--load` only places a local image in the
builder; `--push` is required for a publicly pullable submission image. Do not use an untagged
`latest` image as the only submission reference; retain the immutable tag for the judges.

## Exact Resume Procedure

1. Read this section and run `git status --short`; preserve all untracked evidence.
2. Commit the current prescreen implementation and this handoff update on `developers`.
3. Assign and publish the public `linux/amd64` backend/frontend images using the packaging
   procedure above. Verify clean pulls before spending AMD cloud credits.
4. Confirm the AMD cloud endpoint is on. For the existing MI300X vLLM droplet, first check
   `/v1/models`; do not assume a previous IP/process is still alive.
5. Configure production with distinct Gemma tiers, for example:

```bash
export APP_ENV=production
export SHELFWISE_AUTH_MODE=jwt
export LLM_ROUTINE_BASE_URL=http://<routine-amd-endpoint>:8000
export LLM_STRONG_BASE_URL=http://<strong-amd-endpoint>:8000
export LLM_ROUTINE_MODEL=google/gemma-4-E4B-it
export LLM_STRONG_MODEL=google/gemma-4-31B-it
export LLM_COMPUTE_RESOURCE="AMD Developer Cloud"
export LLM_ACCELERATOR="AMD Instinct MI300X"
export LLM_TIMEOUT_SECONDS=25
```

6. Build once, then measure startup separately from image build:

```bash
docker compose -f docker-compose.production.yml build
started=$(date +%s)
docker compose -f docker-compose.production.yml up --build -d --wait
elapsed=$(( $(date +%s) - started ))
test "$elapsed" -lt 60
```

7. Run `scripts/track3_prescreen.py` against the public origin and retain its JSON receipt.
8. Run the live-required full-system harness and inspect row-level receipts. Fail on offline
   answers, reused decision IDs, HITL mismatches, empty model answers, or zero model-backed
   chat calls.
9. Verify the browser frontend against the same live backend, then record the demo while the
   AMD endpoint is warm.
10. Before merging, run `python -m pytest -q`, `python -m ruff check src tests scripts`,
   `npm run typecheck --if-present` from `frontend/`, and `git diff --check`.
11. Only after public image pulls, the cloud receipt, and demo proof are saved, merge `developers` into `main` and
    verify both local and remote branch state. Do not delete evidence folders or force-reset
    either branch.

## Current Verification Baseline (2026-07-13, Plan 001)

- Full Python suite: pending the Plan 001 verification run in this checkout.
- Real Postgres world integration: `3 passed` against an ephemeral `pgvector/pgvector:pg16`
  container on local port `55433`.
- Ruff: clean.
- Frontend TypeScript typecheck and production build: previously passed; not rerun to completion
  in Plan 001.
- Capability manifest: regenerated from deterministic discovery (`178 capabilities`,
  `sha256:0b9a617e2c1c48a4ded6a1a706f1b4c79c977b4b0d9e1189c0790bc8707ac147`).
- Focused Track 3 prescreen test is present in `tests/test_track3_prescreen.py`.
- Workflow contract: optional live proof uses an environment shell guard; step conditions no longer
  inspect secrets directly.
- Final hybrid cloud receipt: `reports/soak/mi300x_hybrid_concurrency_fixed/benchmark.json` records
  `1,045` successful model calls across 1/8/32 stages with both E4B routine and 31B strong model IDs.
- Final 15-minute sequential E4B soak: `reports/soak_15m_retry/manifest.json` records `158/158`
  model-backed chats, `1,520` unique decisions, zero HITL mismatches, and `381` expected learning
  movements. It is product validation, not dual-tier capacity proof.

## Remaining Risks / Do Not Claim As Done

- The AMD cloud endpoint may be powered off or unreachable; verify it before spending credits.
- Public `linux/amd64` image publication is not complete until a public registry namespace is
  chosen, both images are pushed, and clean unauthenticated pulls succeed.
- The local Docker image build passed, but local CPU build/start is not AMD evidence.
- Actual container readiness under 60 seconds and actual model responses under 30 seconds need
  the cloud receipt.
- AMD-SMI host GPU/VRAM telemetry is not available from the provider; never invent utilization.
- The final hybrid receipt measures 1/8/32 concurrency against the live AMD endpoint; it is
  benchmark evidence, not a claim of broad production capacity.
- Routine/strong routing and two distinct serving endpoints are evidenced by the final hybrid
  receipt; public-origin readiness and judge-facing deployment still require the prescreen receipt.
- Catalog-price and expiry-risk guardrail proof routes now exist as
  `/scenarios/catalog-price/agentic` and `/scenarios/expiry-risk/agentic`. Normal ingest still keeps the
  deterministic guardrail functions for uptime; only the explicit `/agentic` route receipts should
  be claimed as model-agent evidence.
- Historical sections below may mention old branches, counts, IPs, or deadlines. Treat them as
  archival evidence only; this current section controls the next actions.


## EXECUTION CHECKLIST — Postgres-backed world (2026-07-12) — goal: kill all hardcoded seed data

User's explicit goal: the app must genuinely pull from Postgres, not seed CSVs / hardcoded
literals. "It doesn't matter if we have risk, make sure we test and we fix. Just implement."
This checklist is written BEFORE implementation per instruction. Tick items as they land.
Full research/design context: `IMPLEMENTATION_PLAN.md` TASK 4.

- [x] 1. Docker Desktop started; real Postgres running standalone on `localhost:5433` (compose's
      own 5432 was already taken by another project) with the actual `schema.sql` +
      `init_app_role.sh` init scripts applied (not mocks — genuine `psql`-verified 19 tables +
      `shelfwise_app` role). Gotcha hit and resolved: Git Bash/MSYS mangles any `/`-leading
      docker arg into a Windows path — every `docker exec`/`docker run` touching container
      paths needs `MSYS_NO_PATHCONV=1` prefixed, or the bind mount/exec silently no-ops.
- [x] 2. New table `shelfwise_world_snapshot` (tenant_id PK, seed, policy, generated_at,
      payload jsonb) in `src/shelfwise_storage/schema.sql`, RLS policy, added to
      `TENANT_SCOPED_TABLES` in `src/shelfwise_storage/rls.py`. Verified live: re-applied
      schema.sql against the running container, `tests/test_database_schema.py` passes.
- [x] 3. New store module `src/shelfwise_worldgen/world_store.py`:
      `InMemoryWorldSnapshotStore` / `PostgresWorldSnapshotStore` / `create_world_snapshot_store()`,
      same shape as `shelfwise_inventory/store.py` (get/save/clear, tenant-scoped). Verified
      live: real save+get round-trip against Postgres on :5433, confirmed missing-tenant
      returns `None`.
- [x] 4. New population service `src/shelfwise_worldgen/populate.py`:
      `GenerationPolicy` dataclass (seed, catalog_scale, assortment_size, min_near_expiry,
      min_low_stock, min_delayed_orders, min_price_anomalies) + `DEMO_POLICY` preset;
      `populate_world(policy, tenant_id, store) -> PopulationReceipt` — generates products via
      `shelfwise_worldgen.catalog.sample.sample_assortment`, derives stock/sales/suppliers/sites
      deterministically from the same seed, runs a guarantee pass that SELECTS which generated
      SKUs satisfy each constraint (never hardcodes which SKU), records the selection
      transparently in the receipt, writes through the store interface. Verified live against
      Postgres on :5433: 200 real generated products persisted, guarantee pass selected 2
      near-expiry / 5 low-stock / 2 delayed-supplier / 2 price-anomaly SKUs from the generated
      set (not hardcoded), hero SKU chosen dynamically. Determinism re-confirmed (same seed →
      byte-identical receipt).
- [x] 5. New facts provider `src/shelfwise_backend/world_facts.py`: `WorldFactsProvider` with
      `get_scenario_facts(tenant_id, sku)`, `get_store_intelligence(tenant_id)`,
      `get_sourcing_candidates(tenant_id, sku, units_needed)`, `search_products(tenant_id, query,
      limit)`, `get_hero_sku(tenant_id)`. Lazy-populates a tenant's snapshot on first access
      (via `DEMO_POLICY`) if none exists yet, so zero-config flows keep working. Every method
      round-trips through the store (real query per call, no long-lived cache) — Postgres must
      genuinely be hit per request, not just at boot. Verified live against Postgres: hero-sku
      lazy population, scenario facts, full store_intelligence (batch split, delivery
      reconciliation, supplier cover, stock sourcing, learning summary), sourcing candidates
      (branches correctly fall through to supplier when they have no stock for that SKU), and
      product search all produced coherent, non-hardcoded, genuinely-computed results.
- [x] 6. Rewire call sites — ALL DONE, verified live against real Postgres:
      - [x] 6a. `mcp_surface.py` — all 8 tools now call `facts.get_scenario_facts`/
        `facts.get_sourcing_candidates`; `build_platform_tools` takes required `facts` +
        `tenant_id` params (moved tenant_id to build-time, not per-call). Also fixed two
        pre-existing hardcoded-input bugs found along the way: `get_reorder_policy` was
        ignoring the real scenario (hardcoded on_hand=20/avg_daily_demand=10/lead_time=3);
        `get_supplier_ranking`'s "backup supplier" was a hardcoded literal — both now derive
        real numbers via `facts`/`get_alternate_supplier`.
      - [x] 6b. `product_catalog.py` — fully rewritten: `_world_product_items` merges
        `facts.list_products`/`facts.list_stock`; dropped the old CSV+synthetic-generator
        blend entirely (the generated world already has hundreds of real products, no
        separate "synthetic filler" layer needed). `tenant_id` required, no default.
      - [x] 6c. `cascade.py` — all 5 cascades (golden/procurement/sales/cold-chain/
        critic-rejection) use `facts.get_scenario_facts`/`get_store_intelligence`; each has
        its own `_default_facts()` lazy singleton for callers that don't inject one.
      - [x] 6d. `agentic_cascade.py` — all 4 agentic cascades take `facts:
        WorldFactsProvider | None`, same lazy-default pattern. Found and fixed 3 real
        `F821 undefined name 'tenant_id'` bugs in `_build_result`/`_build_procurement_result`/
        `_build_sales_result` (leftover from the tenant-id threading refactor) — now all
        correctly use `event.tenant_id if event is not None else
        default_tenant_context().tenant_id`, matching the cold-chain one that was already
        correct.
      - [x] 6e. `app.py` — `world_snapshot_store`/`world_facts` module-level singletons
        wired alongside every other `create_*_store()`; readiness, `/data/seed/summary`,
        `/products/*`, `/tools/platform`, and the chat route all pass `facts=world_facts`.
        Route paths unchanged.
      - [x] Bonus: found and fixed stale evidence-source labels in `cascade.py` that still
        said `"stock.csv"`/`"sales.csv"`/`"products.csv"`/`"suppliers.csv"` in the decision
        evidence trail even after the data source changed — a judge reading the evidence
        would have seen literal CSV filenames and concluded nothing had changed. Now all say
        `"generated_world"`.
- [x] 7. `world_snapshot_store`/`world_facts` wired into `app.py` startup (see 6e above).
- [x] 8. Test fixes — 26 initially-broken tests (mostly hardcoded `"4011"` SKU literals in
      event payloads that no longer resolve in the generated world) fixed via a shared
      `tests/_world_test_support.py` helper (`demo_sku()`/`demo_facts()`) resolving a real
      generated SKU instead. Files touched: `test_tenant_auth.py`, `test_detective.py`,
      `test_connector_intake.py` (also fixed a stale hardcoded "30.00" price assertion that
      no longer matched the real generated catalogue price), `test_backend_observability_tools.py`
      (removed a stale `on_hand == 240` literal assertion), `test_golden_cascade.py`'s
      profit assertion (see the populate.py fix below). `test_product_catalog_api.py` had
      already been rewritten for the new API shape by earlier work; removed one now-dead
      `_synthetic_product` helper left over from the old CSV+synthetic-blend design.
      One real generation-logic gap found and fixed: the generated hero SKU had no
      guarantee its markdown would actually be profitable (the old CSV "planted story" had
      guaranteed this implicitly). Added `_prefer_profitable_markdown` to `populate.py` —
      reorders the near-expiry candidates so a genuinely profitable one (verified via the
      real `simulate_markdown` function, not a hardcoded guarantee) leads and becomes
      `hero_sku`. Two pre-existing, unrelated test failures (`test_default_tenant_context_matches_demo_tenant`
      and a couple in `test_mlops.py`) turned out to be a red herring from running single
      test files outside the full suite — `conftest.py` forces `SHELFWISE_TENANT_ID=sa_retail_demo`
      and only applies correctly when pytest's own conftest discovery runs, not in raw
      `python -c` reproductions; all pass in the full suite.
- [x] 9. Real Postgres verification — genuinely done, not skipped. Stood up a real Postgres
      16 container (`docker run pgvector/pgvector:pg16`, real `schema.sql` +
      `init_app_role.sh` init scripts, restricted `shelfwise_app` role, not the superuser)
      on `localhost:5433` since the docker-compose default port 5432 was already taken by
      an unrelated project. **Gotcha:** Git Bash/MSYS mangles any `/`-leading argument
      (including `-v host:/container/path` and `docker exec ... /path`) into a Windows path
      — every such command needs `MSYS_NO_PATHCONV=1` prefixed or the mount/exec silently
      no-ops with no error. Booted the real FastAPI backend with
      `SHELFWISE_STORE_BACKEND=postgres` + `SHELFWISE_AUTO_SCHEMA=false` (schema already
      applied) and drove real HTTP requests through it: `/data/seed/summary` (lazy-populated
      a 200-product world into Postgres on first hit), `/products/search`, and all 4
      deterministic cascades (`/scenarios/golden`, `/scenarios/procurement`, `/scenarios/sales`,
      `/scenarios/cold-chain`) — all produced genuine, non-hardcoded results. Confirmed via
      direct `psql` query that the decisions (6 rows) and the world snapshot (200 products)
      are real persisted rows in Postgres, not in-process state.
- [x] 10. Added `tests/test_postgres_world_integration.py` — 3 tests gated on
      `SHELFWISE_TEST_DATABASE_URL` (skip cleanly without it, verified both ways): a real
      `populate_world` round-trip through Postgres, `WorldFactsProvider` reading from a real
      connection, and tenant isolation between two snapshot rows. The fixture auto-forces
      `SHELFWISE_AUTO_SCHEMA=false` so it only needs the one env var to work against the
      restricted app role. Follow-up closed in this continuation: CI now boots an ephemeral
      `pgvector/pgvector:pg16` container with the real schema and restricted app role, then
      runs the test with `SHELFWISE_TEST_DATABASE_URL`.
- [x] 11. Full suite green: 444 passed, 3 skipped (the new Postgres integration tests
      without the env var) — zero failures. Ruff clean. Capability manifest regenerated
      (175 capabilities). Follow-up closed in this continuation: README, DEMO_RUNBOOK, and
      IMPLEMENTATION_STATUS now describe the generated-world/Postgres model instead of the old
      seeded-CSV framing.
- [x] 12. Commits landed incrementally per phase (schema+store+populate, facts provider,
      call-site rewiring, evidence-label fix, test fixes, integration test) — see git log
      on the `developers` branch. This entry is that final summary update.

**Bottom line: the app now genuinely pulls from Postgres.** No more hardcoded CSV seed data
or literal demo fixtures anywhere in the live request path — `load_seeded_scenario`/
`build_store_intelligence_demo` are no longer called from any production code path (only
`shelfwise_data`'s own internals/tests still reference them, which is fine — they're the
low-level building blocks the old CSV loader was built from, now superseded).
**Follow-ups closed in the 2026-07-12 continuation:** CI now runs the Postgres world
integration test against a real pgvector container, and README/DEMO_RUNBOOK/IMPLEMENTATION_STATUS
now describe the generated-world model instead of the old seeded-CSV framing.

## Coordination note (2026-07-11 ~12:00) — judge-readiness pass on main, doc-only

While the public-demo/dual-model branch (`codex/public-demo-dual-model-readiness`, PR #2)
was in progress, a docs-only judge-readiness pass landed directly on main. **No code files
were touched** — nothing in `src/`, `frontend/src/`, `tests/`, or `capabilities/` changed,
so PR #2 should merge without conflicts. What landed on main:

- `README.md`: rewrote the stale top section (now leads with the agentic MI300X system and
  an explicit "Built on AMD" proof section), fixed em-dash mojibake that rendered as garbage
  on GitHub, corrected Inference Strategy to state only MI300X/vLLM was used, refreshed
  Current Scope/Next lists. The `Connected API endpoints:` list and `## Smoke` heading are
  untouched (test contract).
- `DEMO_RUNBOOK.md`: Three-Minute Story now matches the recorded demo; Cloud Proof section
  now documents the real MI300X-only deployment and droplet restart runbook.
- `reports/soak_15min_20260711T042648Z/`: committed the compact soak receipts (summary,
  feature receipts, chat samples, cycles) that the README cites.
- `reports/SUBMISSION_EVIDENCE_REPORT.md` + `reports/ORIGINAL_PROBLEM_COVERAGE.md`:
  committed (README linked them but they were untracked = dead links).
- `submission/`: slide deck PDF + 16:9 cover image used in the hackathon form.
- GitHub repo description + topics set (amd, mi300x, vllm, rocm, gemma, agentic-ai, ...).


## Latest update — real multi-source stock sourcing decision (not a bare transfer number)

User's specific complaint, verbatim: chat was recommending "transfer 18 units now" with
no logic behind *where* that stock comes from. Correct - `plan_supplier_cover` (the old
function backing this) took a single caller-supplied `transfer_available_units` number
and just did `min(gap, that_number)`; there was never any real sourcing comparison.

Added `plan_stock_sourcing` (`src/shelfwise_decision_science/sourcing.py`, new, tested):
given a shortage and a set of candidate sources (nearby branches, the regional
distribution centre, approved suppliers), it filters to sources with any stock, ranks by
lead time then distance then cost, selects the best, and explains *why* in the
conclusion text (e.g. "chosen over branch store_09_midrand (4.00h away) for a faster
delivery"). If nothing has stock, it recommends a purchase order with a stated reason
instead of a transfer. If the best source only partially covers the shortage, it says so
and recommends a PO for the remainder rather than silently under-covering it. 7 new unit
tests cover the ranking, tie-break, partial-cover, and no-source-available paths.

Wired in two places: (1) a new read-only platform tool `get_stock_sourcing_options` so
live chat can call it for any SKU/shortage - chat's system prompt now explicitly forbids
recommending a stock transfer without calling this first; (2) an additive
`"stock_sourcing"` field in `build_store_intelligence_demo()` so the same reasoning
grounds answers even without a live tool call (existing `supplier_cover` field is
untouched, so nothing in the frontend UI card broke).

**Verified live against the real model** - asked "we are short on SKU 4011, where should
the replacement come from?": it named the specific branch (store_02_sandton), cited real
distance/lead-time figures (5.00 km, 2.00 hours), explained why that branch beat the
alternative, and correctly flagged a purchase order for the 12-unit uncovered remainder.
Confirmed both via curl and an actual browser round-trip, zero console errors.

415/415 tests pass, capability manifest regenerated, ruff clean. Commit `938c9e1`.
Backend restarted (no `--reload`, same gotcha as always) to pick this up before
verifying live.

**Known scope limit, honest for the deck**: the branch/DC/supplier network (distances,
lead times, stock levels) is deterministic seeded demo data for SKU 4011, same pattern as
every other demo fixture in this codebase (delivery reconciliation, supplier ranking,
etc.) - it is not a live multi-branch inventory feed. The *decision logic* is real and
general (works for any candidate set you hand it, has its own unit tests independent of
the demo data); the *data* behind today's demo is fixture data, same honesty bar as
everything else already flagged in "Known honest gaps" below.

## Prior update — chat is now genuinely agentic across the whole store + markdown formatting

User's ask: chat needs to read cleanly (not dense paragraphs) AND be able to talk about
"every little thing in our application" (stock, procurement, cold-chain, pricing,
approvals, learning), not just the one product/delivery slice it happened to be told
about. Two real changes, both live-verified, not cosmetic:

1. **Chat is now a real tool-calling agent**, not a single static-state completion.
   `build_chat_reply_with_meta` in `chat.py` now runs through the same
   `AgentOrchestrator` + read-only `PlatformToolRegistry` the production cascades use -
   11 tools: `get_stock`, `get_demand_forecast`, `get_expiry_risk`, `get_reorder_policy`,
   `get_supplier_ranking`, `get_cold_chain_status`, `check_price_integrity`,
   `simulate_markdown`, `list_open_decisions`, `explain_decision`, `get_thresholds`. The
   model decides which to call per question - verified live calling 2-4 tools in a single
   turn for a "give me a report" question (approvals, stock, delivery reconciliation,
   supplier cover all correctly cited with real numbers in one answer). Every answer is
   grounded the same way cascades are (`assert_conclusion_grounded_in_tool_results`) - a
   computed number a tool returns must be cited or the run is rejected. Tenant isolation
   carries through automatically (`trusted_overrides={"tenant_id": ...}` is already
   applied per tool call inside `AgentOrchestrator.run_messages`, using whatever
   `tenant_id` chat passes in - no new code needed there). Falls back to the original
   single-completion path when no decision/memory store is supplied (keeps
   `test_gateway_security.py`'s prompt-injection test working completely unchanged - it
   doesn't pass a store, so it exercises the old path on purpose) and to the offline
   reply when live inference is unavailable or fails.
2. **Chat renders real markdown now.** Added `react-markdown` + `remark-gfm` (both MIT,
   free) and switched `AssistantBubble` in `App.tsx` to render through them, with a new
   `.bubble .md` CSS block in `index.css` styling headings/bullets/bold/code/tables for
   the existing dark/light themes. System prompt in `chat.py` explicitly asks for
   headings + bullets + bold-the-key-numbers on multi-part answers, short paragraphs for
   single facts. Verified live in-browser: a "give me today's report" question rendered
   as real `<h3>`/`<ul>`/`<strong>` elements, not one text blob - screenshot confirms
   clean structured output, zero console errors.

408/408 tests pass, capability manifest regenerated, frontend `tsc --noEmit` clean.
Commit `e3a84f4`. Backend was restarted (no `--reload`, same gotcha as always) to pick
this up before verifying live.

**Known limitation, honest gap for the deck**: the model sometimes declines to call a
tool it lacks a required argument for (e.g. asked "how's the cold chain?" with no area
named, it said plainly it didn't have that data rather than guessing an `asset_id`) -
this is correct grounded behavior, not a bug, but means very vague questions get an
honest "I don't have that specific data" instead of a guess. Not fixed further given
remaining time - a real fix would mean giving tools sensible default-area resolution,
which is a bigger, separate task.

## Historical update — 15-min live soak test PASSED + a real chat bug found and fixed by screen-testing

Ran the actual `shelfwise_eval.full_system` harness for 15 real minutes against the live
droplet with `--live-required` (any offline chat fallback would hard-fail the whole run,
unlike the old v2 marker this file already flagged as invalid). Result: **PASSED, zero
failures.** 333 world cycles, 333/333 chat calls model-backed (0 offline, 0 errors), 4,618
decisions all unique, 2,934 approvals / 56 rejections with 0 HITL mismatches, 34/34 expected
learning movements landed. Artifact: `reports/soak_15min_20260711T042648Z/manifest.json`
(also `.log` next to it). This is the strongest evidence yet that the chat-scaling and
offline-fallback bugs fixed earlier this session hold up under sustained real load, not just
in isolated tests.

While screen-testing chat right after, found a real, user-visible bug: asking "deliveries
issue" returned the literal string `The tool result for the subject "deliveries issue" is
`null`.` - the live model dumping a raw null tool result instead of answering. Root cause:
`_new_chat_response` in `app.py` only ever gave chat the product-catalog search result plus
decisions/learning/traces - it had **zero visibility into delivery reconciliation, supplier
cover, or FEFO batch data**, even though that exact data (`build_store_intelligence_demo()`
from `shelfwise_data`) already powers the "Deliveries / To order / Sell first" sidebar tiles
the user was looking at when they asked the question. Fixed two ways: (1) added
`"store_intelligence": build_store_intelligence_demo()` to the chat state dict so real
answers are possible, (2) hardened the chat system prompt in `chat.py` to explicitly forbid
describing raw tool_results/state_json shape and require a natural-language answer, falling
back to whatever real state exists rather than describing an empty result. Verified live -
same question now returns "the order was for 50 units, but we only received 38... short 12
units, and the supplier fill rate was 76%..." - both via curl and an actual browser
round-trip, zero console errors. 408/408 tests pass, capability manifest regenerated. Commit
`909f42e`.

**This was found by actually using the product as a user would, not by reading code or
running the harness** - a reminder that live click-testing catches gaps that pass every
automated check (the harness above passed 100% right before this bug was found, because the
harness's own chat questions are template-generated product questions that happen to always
have a catalogue match).

## Prior update — agentic cascades are now clickable in the UI (not just curl-testable)

Found and closed a real gap: the 4 agentic routes below existed and worked, but were only
listed in a read-only catalog in the Operations workspace - no way to see one run without a
terminal, which would have forced the demo video to cut to curl output for the single most
impressive capability in the app. The "Gated operational endpoints" list's 4 `/agentic` rows
are now real buttons: click one, it shows a live "calling the live Gemma tool-calling
loop..." state, then the row's detail replaces with the actual result (conclusion, routed
action, real model-call count). Verified live in-browser for all four, zero console errors:
golden, procurement, sales, cold-chain all produced genuine results from the real MI300X
endpoint through an actual click, not a fixture.

While verifying this, found the running local backend (started earlier this session) was
serving stale code from before the sales/cold-chain routes existed (started without
`--reload`) - restarted it, confirmed all four resolve now. **If you restart the backend
again, remember it does NOT auto-reload** - `set -a && source .env && set +a && python -m
uvicorn shelfwise_backend.app:app --host 0.0.0.0 --port 8000 --app-dir src` from repo root.
Frontend: `npm run dev` in `frontend/`. Both currently running and healthy alongside the
live droplet as of this handoff.

Where to see it: Operations workspace (sidebar → "Operations") → scroll to "Gated
operational endpoints" → the four rows ending "(agentic) - click to run live".

## Prior update — 4 of 5 production cascades are now genuinely agentic

User goal: "fix all if you can." Extended the proven golden-cascade pattern to procurement,
sales, and cold-chain. All four now have a real Gemma tool-calling path, verified live:

- `POST /scenarios/procurement/agentic` — Critic calls `get_reorder_policy` +
  `get_supplier_ranking`, cites real reorder quantity (23.70 units) and measured supplier
  choice; Executive routes reorder/monitor.
- `POST /scenarios/sales/agentic` — Critic calls `check_price_integrity` against a deliberately
  mismatched till price (20% over catalogue, outside the deterministic cascade's own 15%
  tolerance); genuinely caught the exception (36.0 vs 30.00, delta 6.00) and routed to
  manager review.
- `POST /scenarios/cold-chain/agentic` — Critic calls `get_cold_chain_status` for a measured
  refrigeration alert; routes dispatch/monitor based on the real measured risk figure.

Each is additive - the original deterministic routes (`/scenarios/procurement`, `/scenarios/sales`,
`/scenarios/cold-chain`) are unchanged and still work. Each new route defaults `live_required`
so a broken endpoint 503s instead of silently faking success. 408/408 tests pass (12 new
tests: 3 cascades x offline-success/live_required-hardfail/ungrounded-rejection).

**Closed in the 2026-07-12 continuation**: the two smaller conditional checks now have explicit
agentic proof routes, `/scenarios/catalog-price/agentic` and `/scenarios/expiry-risk/agentic`, backed by
regression tests for real tool calls, live-required hard-fail behavior, and grounded conclusions.

While building this, fixed a real precision bug in the calculator-grounding check below
(it required citing bare echoed identifiers, e.g. a SKU digit, not just genuinely computed
values) - see that section for detail.

## Prior update — enforced calculator-grounded reasoning across every agent

User's explicit requirement: agents must use tools as their calculator for any math, and
must be able to genuinely explain the math (cite real figures), not just assert a verdict.
This was previously only a prompt instruction ("never invent numbers") with no verification.

Added `extract_salient_numbers`/`assert_conclusion_grounded_in_tool_results` in
`tool_calling.py`: after any agent run, checks that the final conclusion text actually cites
at least one real numeric value from each tool it called, raising `UngroundedAnswerError`
(a `ToolCallingError`, so existing failure handling already covers it) if not. Wired into
the golden cascade's Critic verdict and all 11 roles in `agent_role_coverage.py`. The shared
`guarded_system` text in `AgentOrchestrator.run` now tells every caller "tools are your
calculator... cite the specific figures," so this applies automatically to any future agent
wiring too, not just these two call sites.

**Verified live against the real MI300X endpoint: 11/11 agent roles pass with grounding
enforcement active** — every conclusion now genuinely cites real computed figures (e.g.
"incremental profit of 109.44 ZAR", "240 units on hand", "0.58 cold-chain risk", "41.04
units demand forecast"). 399/399 tests pass (2 new tests added: positive + negative
grounding cases). Commit: check `git log --oneline -1` on this branch.

## Prior update — frontend E2E verified, droplet is LIVE not off

Despite the prior note saying "user turned the droplet off," `/v1/models` and `/health` on
`165.245.130.225:8000` both returned 200 with `google/gemma-4-E4B-it` loaded when checked
just now. **It is live and billing right now** — either it was never actually stopped or it
was restarted without a status update reaching this file. Verify current state before
assuming either way.

**Frontend end-to-end against the live backend is now VERIFIED** (the biggest previously-
untested demo risk is closed): started `uvicorn shelfwise_backend.app:app` with `.env`
loaded + the Vite dev server, drove the actual UI in a browser.
- Chat: typed a real question in the UI, got a real answer; confirmed via direct curl that
  `/chat` responses carry `x-shelfwise-answer-source: model`, `x-shelfwise-model:
  google/gemma-4-E4B-it`, `x-shelfwise-provider: vllm_mi300x`, `x-shelfwise-replayed: false`.
- HITL: clicked Approve on one pending decision (confirmation dialog → "Yes, apply it" →
  real `POST /decisions/{id}/approve` → 200, UI updated to "Approved... 1 approval still
  waiting"), then Reject on the other (→ `POST /decisions/{id}/reject` → 200).
- Zero browser console errors throughout.
- Both servers were left running (not stopped) so the next session can go straight to
  recording. Backend log: `backend_verify.log` in repo root (gitignored, harmless to delete).

A Haiku-model read-only audit (to save tokens) compared the original plan docs
(CLAUDE.md, plot/domains/*.md, README.md, capability manifest) against actual code. Full
coverage matrix was reported in-session; headline findings:
- Confirms what was already known: only the golden cascade's Critic/Executive run through
  real Gemma reasoning; procurement/sales/cold-chain cascades are deterministic math only.
- Flagged that Postgres RLS policies would be bypassed if run under a superuser DB role —
  **verified this is NOT currently relevant**: `.env` has `SHELFWISE_STORE_BACKEND=memory`,
  so no Postgres/RLS is in the loop for the current demo deployment at all. Only matters if
  the Postgres profile is ever actually used — note as a known gap for that profile, don't
  chase it now.
- Batch/lot expiry is now represented in the generated-world snapshot: perishable SKU rows have
  two or three active lots with receipt, expiry, quantity, and source-confidence fields; FEFO reads
  those lots and preserves compatibility with earlier aggregate-only snapshots. The repeatable
  `scripts/fleet_scale_eval.py` proof streams 500,000 product-location-lot rows in 1,000-row chunks,
  retains only the top 200 exceptions, and writes a receipt. Persisting score history/deltas for a
  real retailer remains a production data-layer follow-up, not a gap in the demo's scale proof.
- Historical routing snapshot: dual-model routing was code-complete
  (`base_url_for_agent`/`api_key_for_agent`) but only one model endpoint was deployed
  (`dual_model_configured: false` confirmed live); the final hybrid receipt above supersedes it.

Two more commits landed this session on top of the prior handoff (chat multi-user identity,
dual-model routing config) — see updated commit log below.

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
- `6965473` route routine/strong agent tiers to independently configured model endpoints
- `45fec59` chat multi-user: conversation/message identity, idempotent replay, tenant isolation
  (this commit also swept in the full_system.py stricter live_required audit + revalidation
  script + HANDOFF.md, since they were pre-staged when committed - all content is real and
  tested, the commit message just under-describes scope; not worth rewriting history over)
- `5b30d15` tenant-isolation fix + full 11/11 tool coverage
- `561c50b` bound /chat state (unbounded-prompt scaling bug)
- `c615399` drop strict json_schema decoding (vLLM/Gemma whitespace-loop bug) + 11-role harness
- `afb7c8c` force tool_choice=required on opening agent call
- `c7fbdaf` wire golden cascade Critic/Executive through real Gemma tool calling
- earlier: merge of gpu-notebook-testing, docker-compose env_file fix

394/394 tests pass. `python -m pytest -q`, `python -m ruff check .`, and the production frontend
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

## Historical live verification record (superseded by Current Verification Baseline)

- `/inference/smoke`, `/chat`, `POST /scenarios/golden/agentic` all hit real MI300X Gemma.
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

1. ~~Restore/verify the droplet~~ DONE this session - it's live (`165.245.130.225:8000`,
   `google/gemma-4-E4B-it`). Just confirm it's still up before recording (`curl
   http://165.245.130.225:8000/v1/models`) since availability has flip-flopped already.
2. ~~Frontend end-to-end against the live backend~~ DONE this session - chat (real model
   answers, verified via response headers), HITL approve, and HITL reject all confirmed
   working through actual browser clicks against the live backend, zero console errors.
   Both servers were left running: backend on :8000 (`uvicorn`, `.env` loaded), frontend on
   :5173 (`npm run dev` / vite). If either died, restart: backend -
   `set -a && source .env && set +a && python -m uvicorn shelfwise_backend.app:app --host
   0.0.0.0 --port 8000 --app-dir src`; frontend - `npm run dev` in `frontend/`.
3. **Record the demo video now, while the droplet is hot and the app is verified working.**
   This is the top remaining priority - everything else is secondary to actually capturing it.
4. **Merge this branch to `main`** (required for submission).
5. Only if time remains, in priority order:
    a. ~~Deploy a second model on a second endpoint and set `LLM_STRONG_BASE_URL`/
       `LLM_STRONG_API_KEY`~~ DONE in the final hybrid receipt; retain the public-origin
       prescreen as the remaining deployment proof.
   b. ~~Wire the two smaller conditional checks (`run_catalog_price_check`,
      `run_expiry_risk_check`) through the agentic pattern too~~ DONE in this continuation via
      `/scenarios/catalog-price/agentic` and `/scenarios/expiry-risk/agentic`.
    c. ~~Run `shelfwise_benchmark` at 1/8/32 concurrency against the live endpoint for the
       architecture-comparison report.~~ DONE; receipt is
       `reports/soak/mi300x_hybrid_concurrency_fixed/benchmark.json`.

## Historical known gaps (archived; do not overclaim in the deck/video)

- UPDATE: golden, procurement, sales, cold-chain, catalog-price, and expiry-risk proof routes are
  now genuinely agentic (`/scenarios/{golden,procurement,sales,cold-chain,catalog-price,expiry-risk}/agentic`,
  `live_required` default). The original deterministic routes/functions are still present for the
  normal ingest path and should not be described as model agents unless an explicit `/agentic`
  route receipt proves it.
- Training matrix snapshot: E2B/12B W7900 shakedown was blocked (Jupyter portal down); at that
  time only E4B was live. The current dual-tier receipt above supersedes the serving-topology claim.
- Benchmark architecture comparison (shared/replicated/per-agent/hybrid) is built + tested
  offline but has no real cloud measurements yet.
- Historical serving snapshot: only google/gemma-4-E4B-it was deployed; the final hybrid receipt
  above now records separate E4B routine and 31B strong serving endpoints.
- Batch/lot expiry state and a repeatable 500k synthetic scoring proof are implemented in this
  worktree. Do not overclaim it as a production retailer data platform: score-history persistence,
  real-source ingest, and live operational scale measurements still need their own deployment proof.
- Postgres RLS policies exist in `schema.sql` but are irrelevant to the current demo
  deployment (`SHELFWISE_STORE_BACKEND=memory` - no Postgres in the loop at all); only
  matters if/when the Postgres profile is actually used in a future deployment.
- MI300X operator-side AMD-SMI telemetry: not collected (provider gives no host access);
  report as missing evidence, never estimated. vLLM /metrics IS available on the droplet.

## House rules (unchanged, binding)

No AI attribution anywhere (commits/PRs). Free-tier/open-source only. Cloud inference only
(MI300X/vLLM + Fireworks fallback) — never local models. MIT-clean deps. No temporary fixes.
Read `CLAUDE.md` for the full mandate (full MVP, not a demo slice).
