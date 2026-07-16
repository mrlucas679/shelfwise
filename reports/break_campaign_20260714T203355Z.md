# Phase C break campaign — partial evidence

Run date: 2026-07-14

## Scope and topology

The approved local production topology was started from `docker-compose.production.yml` using the
checked-in local-only database defaults and an ephemeral, process-only tenant signing secret.
The live public origin was `http://localhost` (Nginx to Uvicorn to Postgres and Redis).  No GPU
or hosted-model work was started.

## Results

| Stage | Result | Evidence |
| --- | --- | --- |
| Compose startup | Partial | Postgres and Redis became healthy; the first migration attempt raced Postgres and exited `2` with `connection refused`. Retrying the same migration container after Postgres health completed successfully. |
| Baseline public HTTP | Red result (expected campaign finding) | `GET /health` and `POST /auth/session` returned `200`, but the established session was not sent back over `http://localhost`; authenticated `GET /readiness` returned `401`. Receipt: `break_campaign_20260714T223354Z.baseline.json`. |
| C3 Redis chaos | Pass at health boundary | Redis was stopped for 30 seconds. Backend health was `200` before, during, and after restart; Redis returned to healthy. |
| C3 backend restart | Pass at health boundary | `amdactii-backend-1` was restarted and public `/health` recovered to `200`. |
| C1 concurrent ramp | Blocked | The public authenticated session cannot operate over the Compose HTTP origin. |
| C2 five-minute ingest saturation | Blocked | Same authentication defect; no unauthenticated substitute was used. |
| C4 approval/rejection and idempotency races | Blocked | Same authentication defect; no decision mutations were attempted without a valid session. |

## Defects found

1. **Production Compose is not usable for authenticated local HTTP testing.** The stack publishes
   only port 80 while production session cookies default to `Secure`.  The session endpoint issues
   the cookie successfully, but an HTTP client cannot return it, so protected routes return 401.
   The application intentionally rejects the insecure cookie override outside disposable CI.  A
   Phase C-compatible remedy needs either a TLS-enabled local Compose origin or an explicitly
   isolated, documented disposable-test profile; this campaign did not weaken the production
   configuration.
2. **Migration startup race.** Compose reported Postgres healthy but the initial migration
   connection still received `connection refused`. A single retry succeeded. The migration service
   should include bounded connection retry/backoff before its schema command so the normal Compose
   dependency graph is reliable.

## Completion boundary

Phase C is **not complete**. It produced required red evidence and completed the approved
Redis/backend recovery checks, but the capacity ramp, ingest saturation, and HITL race stages have
not yet run. Phase D remains gated behind a repaired and rerun Phase C.

## Repair verification (same day)

The migration command now retries its initial PostgreSQL connection with bounded backoff. The
HTTP harness now uses its own in-memory Secure session token as an ephemeral bearer credential
only when exercising an HTTP-only test origin; it never serializes the token and does not alter
browser or production cookie behavior.

The repaired baseline receipt is `break_campaign_20260714T231100Z.fixed-baseline.json`. It
reached authenticated readiness (`200`), produced three unique decisions, and performed two
approvals plus one rejection. It remains a red receipt because all three chat calls failed closed
with `503` in the no-model local topology, and one approved decision had no durable learning event.
Those are the next defects/acceptance gaps for the remaining C1, C2, and C4 execution.
