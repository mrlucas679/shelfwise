# Plan 014: Personalize decision queues by workforce role

> **Executor instructions**: Preserve the complete tenant decision ledger. Add a deterministic
> assigned-queue view; do not hide records from owners/auditors or rely on client-only filtering.
>
> **Drift check**: `git diff --stat 041510f..HEAD -- src/shelfwise_backend/decision_access.py src/shelfwise_backend/app.py frontend/src/App.tsx tests`

## Status

- **State**: DONE
- **Priority**: P1
- **Effort**: S
- **Risk**: MED
- **Depends on**: Plan 012
- **Category**: direction / authorization / frontend
- **Planned at**: commit `041510f`, 2026-07-28

## Why this matters

Workforce accounts exist, but every signed-in worker receives the same tenant decision list.
Decisions already carry a target `role`; ShelfWise needs one server-owned assignment matrix so
inventory staff, managers, executives, analysts, and auditors receive useful queues without
duplicating decisions or weakening the full audit ledger.

## Current state and boundaries

- `tenant_scoped_decisions()` owns tenant filtering and must remain the first boundary.
- `GET /decisions` returns the full ledger and must remain backward compatible.
- `TenantContext.role` is the verified role; a query parameter must never select another role.
- In scope: a central assignment function, `queue_view=assigned`, response scope receipt, tests,
  and using the assigned view for the browser approval queue.
- Out of scope: new roles, per-user manual assignment, notifications, or source-system writes.

## Done criteria

- [x] `queue_view=all` retains the complete tenant ledger.
- [x] `queue_view=assigned` is derived only from the authenticated role.
- [x] Owner and auditor visibility is explicit; unsupported role values fail closed.
- [x] Backend, frontend, and browser tests pass.

## STOP conditions

- Stop if any implementation relies only on React filtering.
- Stop if queue personalization changes approve/reject role authorization.

