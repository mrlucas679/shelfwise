# Plan 013: Make category policy confirmation part of guided onboarding

> **Executor instructions**: Implement the persisted confirmation flow described below without
> making browser state authoritative. Keep the built-in policy templates as the one source of
> decision rules. Update this plan and `plans/README.md` only after every gate passes.
>
> **Drift check**: `git diff --stat 041510f..HEAD -- src/shelfwise_backend/product_policies.py src/shelfwise_backend/routes_onboarding.py src/shelfwise_backend/state.py src/shelfwise_storage frontend/src/App.tsx tests`

## Status

- **State**: DONE
- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: Plan 012
- **Category**: direction / persistence / frontend
- **Planned at**: commit `041510f`, 2026-07-28

## Why this matters

The Setup guide can connect a store and its data, but it does not ask the owner to acknowledge
the product-family rules ShelfWise will use. `product_policies.py` already owns stable bakery,
produce, dairy, frozen, meat, seafood, and ambient rules; the missing product step is a durable,
tenant-scoped confirmation of those existing templates, not a second configurable policy system.

## Current state and boundaries

- `product_policies.py` is the authoritative policy registry used by deterministic and agentic
  cascades. Do not duplicate its numeric rules in routes or React.
- `routes_onboarding.py` derives progress from server stores. Browser completion flags are invalid.
- `tenant_profiles.py` is the persistence pattern: memory/Postgres implementations, central schema,
  and RLS.
- In scope: template discovery, durable confirmations, onboarding status, UI step, API/tests/docs.
- Out of scope: arbitrary rule editing, a policy DSL, billing, or learned policy mutation.

## Steps

1. Expose serializable built-in templates from `product_policies.py`.
2. Add a tenant-scoped memory/Postgres confirmation store and central-schema RLS.
3. Add owner-only list/confirm APIs and make at least one current-template confirmation a required
   server-derived onboarding step.
4. Add an accessible Policies step to the Setup guide and update browser coverage.

## Done criteria

- [x] Confirmations persist by tenant/category/template and cannot cross tenants.
- [x] Stale template IDs do not count as current confirmations.
- [x] Setup readiness requires company, store, data source, and policy confirmation.
- [x] Backend, schema, frontend, capability, and Playwright gates pass.

## STOP conditions

- Stop if implementation requires storing arbitrary executable rules supplied by a browser.
- Stop if a template change can silently reuse an old confirmation.

