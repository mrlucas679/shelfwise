# Plan 016: Close product operations controls without new paid dependencies

> **Executor instructions**: Reuse existing `/health`, backup, restore, deployment, and runbook
> contracts. Add only portable operator controls; never commit customer contact data or secrets.
>
> **Drift check**: `git diff --stat 041510f..HEAD -- scripts docs CLIENT_INTAKE_RUNBOOK.md README.md`

## Status

- **State**: DONE
- **Priority**: P1
- **Effort**: M
- **Risk**: LOW
- **Depends on**: Plans 013-015
- **Category**: reliability / security / docs
- **Planned at**: commit `041510f`, 2026-07-28

## Why this matters

Nightly backup and restore-drill scripts already exist, but product operations still lack a
portable uptime probe, structured incident handoff, release checklist, and one explicit POPIA
data inventory. These controls must work before a paid monitoring vendor is chosen.

## Current state and boundaries

- `client_backup.sh` and `client_restore_verify.sh` already define RPO 24h / RTO 1h.
- `/health` reports service state; operators currently have to remember to inspect it.
- `CLIENT_INTAKE_RUNBOOK.md` defines a manual support channel and incident-log discipline.
- In scope: a stdlib health monitor with optional generic webhook, bounded JSONL incident receipt,
  operator tests, release runbook, and POPIA inventory/retention/breach checklist.
- Out of scope: choosing a vendor, legal sign-off, a public SLA, or storing customer PII in Git.

## Done criteria

- [x] Monitor exits non-zero on HTTP/service failure and never logs webhook credentials.
- [x] Incident receipts are bounded, timestamped, and contain no raw response bodies.
- [x] Backup/restore, release/rollback, support, and POPIA responsibilities are documented.
- [x] Focused tests, Ruff, and full repository gates pass.

## STOP conditions

- Stop if alert delivery needs a new paid dependency or account.
- Stop if legal language would be represented as reviewed counsel rather than an operator draft.

