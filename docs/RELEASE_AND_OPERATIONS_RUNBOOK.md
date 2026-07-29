# ShelfWise Release and Operations Runbook

Status: application procedure implemented; deployment owner acceptance remains external.

## Release evidence gate

A release candidate is the exact Git commit that passes all required GitHub checks. Record
the commit SHA, workflow URL, image digest, operator, and intended client tenant before
promotion. Do not promote a working tree, an unreviewed image tag, or a commit whose live
inference check was skipped without explicitly recording that boundary.

Required software evidence:

1. Backend lint, wheel import, full tests, eval gate, and smoke pass.
2. Frontend typecheck/build and browser E2E pass.
3. Compose validation, production image build, public-origin smoke, and deployment
   shakedown pass.
4. Database migration runs with the least-privilege application role and RLS checks pass.
5. Backup is recent enough for the 24-hour RPO and the latest monthly restore drill passed.

Live release evidence, once the client endpoint exists:

1. Direct `/v1/chat/completions` probes succeed for routine and strong model routes.
2. `/health` and `/readiness` pass through the same public origin clients use.
3. Login, assigned approval queue, approval, task completion, and value receipt work for a
   disposable release-test account.
4. No production client data is copied into simulation or training datasets.

## Deploy and rollback

Deploy only a saved image digest produced from the verified commit. Run database migrations
before starting application replicas, then wait for public readiness before routing traffic.
Keep the previous application image and database backup until the release is accepted.

Rollback when readiness fails repeatedly, authentication cannot complete, tenant isolation
evidence fails, write-back receipts cannot be recorded, or error rates exceed the operator's
agreed threshold. Stop new write actions, restore the previous image, and restore data only
when a migration made the current database incompatible. A data restore is a separate,
destructive decision and must follow `scripts/client_restore_verify.sh`; never use it as the
default application rollback.

## Monitoring

Run the dependency-free probe from an external scheduler every five minutes:

```powershell
python scripts/health_monitor.py `
  --base-url https://CLIENT_HOST `
  --incident-log reports/operations/incidents.jsonl
```

Set `SHELFWISE_ALERT_WEBHOOK_URL` only to an approved HTTPS endpoint if automated incident
delivery is required. The probe stores bounded metadata—endpoint path, status code, and
failure class—not response bodies, credentials, or stack traces.

The deployment owner must choose the real scheduler, alert receiver, availability target,
and escalation contacts. Code cannot verify those external arrangements.

## Incident handoff

For every material incident record: start/end time, affected client and capability, release
SHA/image digest, customer-visible symptoms, data-integrity impact, containment, recovery,
and follow-up owner. Never paste tokens, raw connector payloads, personal information, or
full exception dumps into the incident log. Preserve relevant structured trace and audit IDs.

Security or privacy incidents take priority over availability recovery: disable the affected
integration, preserve audit evidence, notify the designated information officer, and follow
the client-approved notification procedure. Do not claim regulatory notification completion
without the responsible human's evidence.

## Recurring controls

- Daily: verify monitor delivery and backup completion.
- Weekly: review unresolved incidents, inactive accounts, failed connector polls, and stale
  write-back tasks.
- Monthly: perform and record a restore drill; review account roles and policy templates.
- Per release: execute the evidence gate above and retain the exact-head workflow link.
