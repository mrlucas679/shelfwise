# Plan 015: Report monthly value recovered from verified outcome receipts

> **Executor instructions**: Do not relabel expected exposure or simulated uplift as recovered
> money. The owner statement must separate verified, estimated, and unverified amounts.
>
> **Drift check**: `git diff --stat 041510f..HEAD -- src/shelfwise_backend/app.py src/shelfwise_backend/observability.py src/shelfwise_connectors/writeback.py frontend/src/App.tsx tests`

## Status

- **State**: DONE
- **Priority**: P1
- **Effort**: M
- **Risk**: HIGH
- **Depends on**: Plan 014
- **Category**: direction / correctness / reporting
- **Planned at**: commit `041510f`, 2026-07-28

## Why this matters

The product-readiness plan names “rand value recovered this month, with receipts” as ShelfWise's
primary commercial measure. Current approval learning derives a modeled outcome immediately, so
summing it as recovered value would overclaim reality. A verified statement needs an explicit
completion receipt and must show the remaining estimated exposure separately.

## Current state and boundaries

- Decisions carry expected economics and approval outcomes.
- Approved actions create write-back tasks with `decision_id` in rollback metadata.
- Task completion already captures operator/source evidence but no actual recovered amount.
- In scope: optional non-negative actual value on task completion, decision annotation, a
  month-bounded tenant report API, owner-facing UI, receipts, and tests.
- Out of scope: accounting integration, automated billing, tax treatment, or fabricated outcomes.

## Done criteria

- [x] Only explicitly verified actual amounts contribute to `verified_recovered`.
- [x] Estimated/unverified amounts are separately labeled and never summed into verified value.
- [x] Month parsing, tenant isolation, currency, and receipt IDs are tested.
- [x] Backend, frontend, capability, and Playwright gates pass.

## STOP conditions

- Stop if a report would need to infer actual money from expected outcome fields.
- Stop if an outcome cannot be tied to a tenant-scoped decision and completion receipt.

