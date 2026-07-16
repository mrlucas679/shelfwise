# Plan 002: Enforce the production security perimeter

> **Executor instructions**: Preserve API-key authentication and JWT tenant isolation. Do not embed
> addresses, tokens, certificates, or secrets. Verify in a disposable environment.
>
> **Drift check**: `git diff --stat 9c907b3..HEAD -- docker-compose.production.yml frontend/nginx.conf src/shelfwise_backend/app.py scripts/bootstrap_mi300x_vllm.sh DROPLET_BOOTSTRAP.md tests/test_droplet_bootstrap_contract.py`

## Status

- **State**: COMPLETE (2026-07-13, local verification)
- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: `plans/001-restore-ci-and-evidence-truth.md`
- **Category**: security
- **Planned at**: commit `9c907b3`, 2026-07-13

## Why this matters

The production Compose stack publishes HTTP port 80 and explicitly defaults the JWT session cookie
to non-Secure. Separately, the MI300X bootstrap inserts source-unrestricted DNAT/ACCEPT rules for the
model ports even though the deployment guide requires an application-host-only firewall. Bearer auth
reduces unauthorized use, but it does not prevent Internet scanning, denial-of-service pressure, or
plaintext cookie capture.

## Current state

- `docker-compose.production.yml:66-68` enables public demo sessions and defaults
  `SHELFWISE_COOKIE_SECURE` to `false`.
- `docker-compose.production.yml:107-108` publishes only `80:80`; the repo supplies no TLS endpoint.
- `src/shelfwise_backend/app.py:422-429` correctly supports HttpOnly, Secure, SameSite=Strict cookies,
  but Compose disables Secure.
- `scripts/bootstrap_mi300x_vllm.sh:103-111` accepts any non-docker0 source for ports 8000/8001.
- `DROPLET_BOOTSTRAP.md:6-7` says only the application host may reach those ports.
- Follow the fail-fast production configuration pattern at `app.py:156-173`.

## Commands

| Purpose | Command | Expected success |
|---|---|---|
| Focused tests | `python -m pytest -q tests/test_droplet_bootstrap_contract.py tests/test_tenant_auth.py tests/test_infra_config.py` | all pass |
| Compose | `docker compose -f docker-compose.production.yml config --quiet` | exit 0 |
| Full gates | Plan 001 commands | all pass |

## Scope

**In scope**: production Compose, auth startup validation, MI300X bootstrap firewall rules, deployment
docs, and focused tests.

**Out of scope**: implementing a certificate authority, changing JWT claims/roles, replacing vLLM,
or changing model memory fractions.

## Steps

### Step 1: Fail closed on insecure production cookies

Default production cookies to Secure. Add an explicit, clearly named local-CI escape hatch for the
HTTP-only disposable Compose smoke; production startup must reject `SHELFWISE_COOKIE_SECURE=false`
unless that escape hatch is set. Configure the CI production smoke to use the escape hatch only in
CI. Document that real deployment requires HTTPS termination before Nginx.

**Verify**: add tests for production rejection, HTTPS-safe default, and explicit CI override.

### Step 2: Require a GPU source allowlist

Add a required `VLLM_ALLOWED_CIDR` (or equivalently precise source address) for public Quick Start
port publication. Include `-s "$VLLM_ALLOWED_CIDR"` in both check and insert rules. Fail before model
startup when the value is missing or syntactically invalid. Preserve container-internal readiness
probes and host-container benchmark access.

**Verify**: bootstrap contract tests assert the source restriction and reject a broad rule lacking
`-s`; `bash -n scripts/bootstrap_mi300x_vllm.sh` exits 0.

### Step 3: Align docs and operator checks

Update both droplet runbooks with cloud-firewall and host-iptables requirements, HTTPS termination,
Secure cookies, key rotation, and a verification command that confirms non-allowlisted access fails.
Use placeholders only.

**Verify**: documentation contract tests pass and secret scanning finds no credential values.

## Test plan

- Production auth startup: secure default, rejected insecure config, accepted explicit CI override.
- Bootstrap source allowlist: required value, constrained iptables rules, rerun idempotency.
- Existing same-origin auth/session and tenant isolation tests remain green.

## Done criteria

- [ ] Named production cannot silently run a non-Secure session cookie.
- [ ] Model ports are source-restricted by the bootstrap itself.
- [ ] Compose validation and focused/full tests pass.
- [ ] No credential or real address is committed.

## STOP conditions

- The intended deployment has no HTTPS terminator available.
- Restricting model ports prevents the declared application host from connecting after two attempts.
- The fix requires disabling API-key or JWT validation.

## Maintenance notes

Provider cloud firewalls remain defense in depth; bootstrap rules must still fail closed because cloud
firewall state is external and easy to misconfigure.
