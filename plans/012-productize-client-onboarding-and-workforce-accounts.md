# Plan 012: Productize client onboarding and workforce accounts

> **Executor instructions**: Deliver this in the numbered phases below. Preserve the
> dedicated-stack deployment model until an explicit shared-hosting decision replaces it.
> Do not retain game/demo vocabulary or fake setup completions. Every destructive or
> privileged action must be authorized server-side and have a recovery path.
>
> **Drift check (run first)**: `git diff --stat 83d17fc..HEAD -- src/shelfwise_backend frontend/src tests CLIENT_INTAKE_RUNBOOK.md README.md`

## Status

- **State**: IN PROGRESS (Phases 1-4 partially implemented, 2026-07-24)
- **Priority**: P1
- **Effort**: L (deliver in the phases below; do not attempt one giant PR)
- **Risk**: HIGH (authentication, authorization, tenant data, and production onboarding)
- **Depends on**: none
- **Category**: direction / security / architecture
- **Planned at**: commit `83d17fc`, 2026-07-24

## Why this matters

ShelfWise can authenticate exactly one owner configured through deployment environment variables,
but a real shop needs an owner to create and manage work accounts for managers, inventory staff,
analysts, auditors, and executives. Its intake runbook also still requires technical API calls for
CSV import and physical-store onboarding. That is a project handoff, not a product onboarding
experience. This plan turns the existing dedicated-client stack into a browser-led client journey
while retaining current JWT/RLS/HITL protections and the existing connections UI.

## Current state

- `src/shelfwise_storage/accounts.py` now provides memory and Postgres workforce-account stores;
  `shelfwise_work_accounts` has central-schema RLS. `app.py` accepts active work-account login
  before retaining the configured owner as a recovery bootstrap. Owner-only `/accounts` list/create
  APIs exist, but invitations, deactivation, recovery, and platform-admin bootstrap do not yet.
- `src/shelfwise_backend/tenant.py` defines the existing work roles: `owner`, `executive`,
  `manager`, `inventory`, `analyst`, `auditor`. Reuse these names; do not introduce gaming roles.
- `src/shelfwise_storage/tenant_profiles.py` persists tenant business profiles with Postgres RLS;
  it is a suitable adjacent pattern for a tenant-scoped account store, not a substitute for one.
- `frontend/src/App.tsx` now has People & access (account list/create), Connections CSV preview/
  import, existing connector credentials, and Store Twin self-service store setup. These are
  working initial UI flows, not substitutes for invitation activation or full onboarding status.
- `CLIENT_INTAKE_RUNBOOK.md` now names the browser UI as the normal path; API calls are recovery/
  automation interfaces.
- `README.md:573-688` commits to an operational assistant with tenant/user isolation, bounded
  evidence, and human approval; the new journey must not bypass these boundaries.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Backend tests | `.\\.venv\\Scripts\\python.exe -m pytest -q` | all tests pass |
| Lint | `ruff check --no-cache src tests` | no violations |
| Frontend typecheck | `npm run typecheck` (in `frontend`) | exit 0 |
| Frontend build | `npm run build` (in `frontend`) | exit 0 |
| Capability contract | `python scripts/compare_capability_manifests.py --write` | manifest regenerated |

## Scope

**In scope**: product-language cleanup, platform-admin bootstrap, client owner signup/invitation,
staff account lifecycle, role-based browser UX, UI-driven CSV/twin/connector onboarding, and
adaptively planned evidence retrieval for chat.

**Out of scope**: public self-service hosting, billing, SSO/SCIM, automatic ERP write-back,
raw camera/video ingestion, passwordless authentication, or reinforcement-learning training.

## Delivery sequence

### Phase 1: Define the identities and bootstrap boundary

**Progress:** tenant-scoped account persistence and work-account fields are implemented. The
platform-admin and legacy-owner migration work below remains required.

1. Add a durable tenant-scoped `UserAccount` store (memory/Postgres implementations, RLS,
   migration/schema contract) with opaque user ID, normalized email, given name, surname,
   work position/title, role, status, password hash, created/updated timestamps, and an optional
   invitation/activation record. Never store raw passwords, invitation secrets, or personal data
   in event/audit payloads.
2. Define exactly two bootstrap modes in documented config: a one-time platform administrator
   bootstrap for creating a client tenant, and a first client owner bootstrap/invite. Remove the
   configured single-owner login as the steady-state account source after a safe migration path
   exists; retain it only as an explicit emergency recovery mechanism with audit logging.
3. Add a migration/compatibility path that creates the configured legacy owner as an owner account
   on first startup without weakening authentication or duplicating users.

**Verify**: unit and Postgres contract tests prove email uniqueness per tenant, RLS isolation,
password hashes never appear in API responses, and legacy-owner migration is idempotent.

### Phase 2: Build secure account and invitation APIs

**Progress:** active work-account login; owner list/create; role change; deactivation; and
reactivation are implemented with browser controls. Invitation activation, session invalidation,
and recovery are still required.

1. Replace single-account login lookup with active account lookup, scrypt verification, and the
   existing strict HTTP-only JWT cookie. JWT claims remain tenant ID, user ID, role, and expiry.
2. Add owner-only APIs to list staff, invite/create a staff account, change a permitted role,
   deactivate/reactivate an account, and invalidate that account's active sessions. Require an
   account to change its own password on first activation.
3. Add public-but-single-use activation/signup only for an unexpired signed invitation. Required
   fields: work email, given name, surname, work position, password, confirmation; role and tenant
   come from the invitation and must never be accepted from the form.
4. Add owner-safe password reset initiation/consumption. If a production email provider is not
   configured, fail closed with an operator-visible configuration error; never return reset tokens
   to a browser API response.
5. Enforce last-owner protection, self-deactivation prevention, tenant ownership checks, rate
   limiting, generic auth errors, audit records, expiry, and replay-safe invitation consumption.

**Verify**: API tests cover signup success, invalid/expired/replayed invite, cross-tenant access,
manager attempting owner actions, last-owner deletion, inactive login, password-change requirement,
and no password/token leakage.

### Phase 3: Build the browser-led workforce experience

**Progress:** the owner-facing **People & access** workspace now lists staff and provides create,
role-change, deactivate, and reactivate controls. The account form collects work email, first
name, surname, work position, role, password, and confirmation. A dedicated invitation-activation
screen remains required before a staff member can set their own first password.

1. Replace the login-only surface with sign-in, invitation activation, first-owner setup, reset,
   and forced-password-change screens. Use plain operational wording and accessible field labels,
   validation, focus management, loading, empty, and error states.
2. Add an owner-only **People & access** workspace: staff list, role/title/status, invite form,
   deactivate/reactivate, and session/password recovery actions. Explain capabilities by role;
   do not present technical JWT, API-key, or environment-variable controls.
3. Remove/rename game, demo, simulation-only, or developer-endpoint language in user-facing
   navigation and empty states. Keep `world_simulation` only as an internal/evaluation data domain;
   user-facing operations must say store, data import, recommendations, approvals, and outcomes.

**Verify**: frontend typecheck/build and Playwright coverage for owner setup, staff invitation
activation, role restrictions, deactivation login failure, and keyboard-only completion.

### Phase 4: Convert client intake into a guided setup workspace

**Progress:** Connections now has company-profile, encrypted connector-credential, and CSV
preview/import forms; Store Twin has self-service store setup with initial areas. Persistent
onboarding progress and recovery state remain required.

1. Add a persistent onboarding checklist/status model per tenant/store with explicit states,
   evidence, resumability, and recovery guidance: company profile, owner/staff, data import,
   store topology, connector, first grounded recommendation, and go-live review.
2. Create UI flows over existing backend capabilities: CSV upload/preview/mapping/commit, inbound
   error review, store topology onboarding, connector credential entry, device registration, and
   readiness checks. Do not claim a connection/import completed until its existing server receipt
   confirms it.
3. Keep unsupported webhook connector setup and raw hardware/video integration visible as honest
   operator-assisted steps, with copyable non-secret instructions—not fake connected states.
4. Rewrite `CLIENT_INTAKE_RUNBOOK.md` as an exception/recovery runbook after the browser flow is
   real; it must no longer be the standard client path.

**Verify**: API + frontend E2E tests can complete a fresh client setup without curl, environment
editing, or direct database access; interruption/resume and invalid CSV/connector failure paths
are covered.

### Phase 5: Apply the RAG research as bounded adaptive evidence planning

1. Extract an auditable retrieval plan before chat state is built. It decides, from deterministic
   intent/risk/domain signals, whether to request live twin facts, decisions, learning, traces,
   conversation memory, and promoted skills. It must permit no more than one follow-up retrieval.
2. Record a retrieval receipt: selected partitions, counts, omissions, freshness, conflicts, and
   why a follow-up was/was not allowed. Insufficient or conflicting evidence escalates to the
   existing strong route/Critic; it must not be hidden by a fluent answer.
3. Keep retrieval policy deterministic and evaluation-driven first. Add a labelled evaluation set
   and compare usefulness, grounding, latency, and context cost before considering learned/RL
   policies. Do not add a vector database merely because the research mentions one.

**Verify**: characterization tests prove simple account/help questions do not load operational
evidence; multi-hop operational questions retrieve the required partitions; conflict/inadequacy
creates an escalation receipt; prompt budget remains under the existing ceiling.

## Done criteria

- [ ] A platform administrator can create a client and first owner through supported product flows.
- [ ] A client owner can create/invite/deactivate work accounts, with given name, surname, work
  position, email, and least-privilege role, entirely in the browser.
- [ ] A new client can complete the supported onboarding path in the browser without technical
  credentials, curl, direct database access, or environment-file edits.
- [ ] Every account, invitation, onboarding, and retrieval boundary has tenant isolation,
  failure-path tests, and an auditable receipt where appropriate.
- [ ] Existing connector encryption, JWT cookie protections, RLS, Critic/Executive/HITL flow,
  and dedicated-stack deployment model remain intact.

## STOP conditions

- Stop if enabling external email/SMS delivery requires a vendor account, billing, or credentials
  the operator has not authorized; implement the internal contract and document the integration
  boundary, but do not choose a vendor.
- Stop if moving from dedicated stacks to shared hosting is required to make a workflow work;
  that is a product/operations decision, not an implementation shortcut.
- Stop if a proposed UI route would expose passwords, invitation/reset secrets, connector secrets,
  tenant data, or raw hardware media.
