# Client Intake Runbook — Taking a Real Shop Live on a Dedicated Stack

Date: 2026-07-28. Deployment model: **one dedicated stack per client** (owner decision,
2026-07-23). The first owner is created through the browser's one-time platform-authorized
setup; environment-based owner credentials now exist only for idempotent legacy migration
or explicitly enabled emergency recovery. Multi-tenant plumbing stays internal until there
are enough clients to justify shared hosting. Every step below is an operator action against
software that already exists; where a step still requires a purchase, it says so explicitly.

Target: **first useful recommendation within 48 hours of receiving the client's data.**
Any step that fights you here is a product bug — file it, fix it in the product, keep
this runbook shrinking.

## 0. Prerequisites (per client, before day one)

- [ ] Hosting purchased: an application droplet/VM (Linux, Docker) reachable over public
      HTTPS, per `DROPLET_BOOTSTRAP.md`.
- [ ] Inference endpoints live: AMD cloud vLLM per `docs/mi300x-recreate-runbook.md`
      and/or the Fireworks fallback, with `scripts/track3_prescreen.py` + one live
      agentic cascade passing as acceptance.
- [ ] Signed pilot agreement + POPIA processing basics (what data we hold, why, for how
      long; sales data only, no customer PII).
- [ ] The client's data export in hand: product list, current stock (with expiry dates
      where they have them), and 90 days of sales. Any spreadsheet works — the CSV
      intake maps their column names.

## 1. Provision the stack

1. Clone the release commit on the host; copy `.env.example` to `.env`.
2. Set the storage backend and database:
   - `SHELFWISE_STORE_BACKEND=postgres`, `DATABASE_URL` pointing at the restricted
     `shelfwise_app` role (never a superuser — the app refuses BYPASSRLS connections).
3. Set the client identity:
   - `SHELFWISE_TENANT_ID=<client-slug>` (e.g. `masekos_grocery`) — one tenant per
     stack, used by every store and the connector poll loop.
4. Mint secrets (unique per client, never reused across stacks):
   - `TENANT_AUTH_SECRET` (JWT signing), `API_KEY` (write-path guard),
     `SHELFWISE_WORKER_API_KEY`, and a one-time `SHELFWISE_PLATFORM_BOOTSTRAP_KEY`.
5. Configure account delivery:
   - `SHELFWISE_PUBLIC_APP_URL`, `SHELFWISE_SMTP_HOST`, `SHELFWISE_SMTP_FROM`, and
     the SMTP port/credentials required by the selected mail service.
   - ShelfWise uses standard SMTP and does not require a code dependency on one vendor.
     Until delivery is configured, invitations and recovery fail closed before claiming
     that email was sent.
6. Configure inference: `LLM_PROVIDER`, `LLM_*_BASE_URL/API_KEY/MODEL` per the
   production profile (`vllm_mi300x` identity or the deliberate fallback).
7. `docker compose -f docker-compose.production.yml up -d` — the migrate job applies
   idempotent migrations; verify `/health` and `/readiness` report every lifespan
   service green.
8. Open the application, choose **Set up first company owner**, and give the authorized
   owner the one-time bootstrap key out of band. Confirm a second bootstrap attempt is
   rejected. Remove or rotate the bootstrap key after the owner exists.

## 2. Complete the in-product Setup guide

The normal client path is now **System → Setup guide**. It resumes from server state and leads
the owner through company identity, store creation, and one required data source. The owner may
connect an ERP with encrypted credentials or choose Products, Stock, Expiry, or Sales CSV,
preview the inferred mapping and validation result, then import the approved file. In **People
& access**, owners invite executives, managers, inventory staff, analysts, and auditors by real
work identity and position. Each worker receives a single-use activation link and chooses their
own password. Owners do not need curl, a database login, or an environment-file edit.

Verify `GET /onboarding/status` reports `ready_for_operations: true` before leaving setup. The
API details below remain the recovery/automation contract, not the required client workflow.

The browser is the product path. `POST /intake/csv/preview` then `POST /intake/csv/commit`
remain the recovery/automation contract (ingest-role key or owner JWT). Order matters —
products first so identity resolution exists before stock and sales arrive.

For each file, in this order — `products`, `stock`, `expiry`, `sales`:

1. **Preview** with the raw file text. The response reports the inferred column
   mapping, unmapped required columns, per-row errors, and a sample of the canonical
   rows. Fix mapping with the `mapping` override (`{"sku": "Item Code"}`) rather than
   editing the client's file; fix data errors with the client (their file, their
   truth — we do not silently repair it).
2. Iterate preview until the invalid-row count is understood (a few dirty rows are
   normal; they quarantine with provenance and appear in inbound records as
   `invalid` — they do NOT block the file).
3. **Commit.** Commits are idempotent (content-keyed dedup) and capped at 1,000 rows
   per request — split big files and send parts; re-sending a part is safe.
4. Verify: `GET /connectors/inbound-records` shows the rows; `GET /catalog/resolve`
   answers for a spot-checked SKU; `GET /products/search` finds their products.

Notes that matter:

- Dates must be ISO (`2026-07-21`). Ambiguous regional formats are rejected per row by
  design — a silently mis-parsed expiry date corrupts the exact math this product sells.
- Sales files without receipt/order numbers import fine but dedup only by row content;
  the preview warns about it. Prefer exports that include a receipt id.
- `identifier_conflict` on a products commit means the same item code arrived under two
  different product names. That is a data-quality finding to resolve WITH the client,
  never auto-merged.

## 3. Extend the physical store twin when needed

The Setup guide creates the initial store without exposing an internal tenant identifier.
Use **Store Twin** afterward for advanced topology and operational review. The API form below
remains useful for automated or detailed fixture imports.

1. `POST /twin/onboarding` with the store's fixtures: fridges, freezers, shelves,
   backroom (type + label is enough to start).
2. Verify the operations workspace renders the topology and the fidelity receipt.
3. If the client is on Odoo/SAP/SYSPRO/Dynamics Business Central (poll-based ERP), the
   *client's own owner login* can now self-serve connect it: sign in, open Connections,
   click the system, enter its credentials. Stored encrypted (`shelfwise_connectors.
   credentials`, requires `SHELFWISE_CREDENTIAL_ENCRYPTION_KEY` set on the stack), no
   operator involvement needed. The `SHELFWISE_CONNECTOR_*` env vars remain a shared
   fallback default if you'd rather configure it yourself instead. Either way, enable the
   poll loop (`CONNECTOR_POLL_ENABLED=1`).
   If the client is on Yoco/Square/Shopify/Lightspeed (webhook-based POS), those still
   need operator-side setup - they authenticate inbound webhooks via a shared secret you
   configure for them, not a credential the owner enters themselves.
   CSV remains the backfill and fallback path either way.

## 4. Acceptance before the client touches it

- [ ] Owner can log in from their own device over HTTPS; cookie session works.
- [ ] Setup guide reports ready from server state; reload the page and confirm progress resumes.
- [ ] Required staff have named work accounts with the correct position and least-privilege role.
- [ ] Chat answers a grounded question about THEIR data ("what's expiring this week?")
      with tool citations, on the live model.
- [ ] At least one real candidate/recommendation exists in the approval queue and its
      evidence trace reads correctly against their imported rows.
- [ ] Cross-checks: `/health` green, worker consuming, decision receipts populated.
- [ ] Backup verified per section 6 (a stack with client data and no tested restore is
      not client-ready — this gate is not skippable).

## 5. Operate the pilot

- Shadow mode first (2 weeks): recommendations flow, owner treats them as read-only;
  we log precision/noise weekly. Then live HITL approvals.
- Weekly 30-minute owner call: every wrong recommendation becomes a policy fix, Critic
  rule, data-completion task type, or eval scenario — through the existing workflow
  slots, never a bespoke patch on their stack.
- Track the one number: rand value recovered this month, with receipts (decision
  economics + accountability joins already compute the inputs).

## 6. Backup and tested restore (RPO 24h, RTO 1h)

- Nightly `pg_dump` (custom format) of the client database to off-host storage, via
  `scripts/client_backup.sh` on a cron/systemd timer; 14 daily + 8 weekly retained.
- **Restore drill is part of intake, not an emergency skill**: before the client goes
  live, restore the latest dump into a scratch database with
  `scripts/client_restore_verify.sh` and confirm row counts for events, decisions, and
  inbound records match the source. Repeat the drill monthly; log each drill's date and
  result in the client's ops record.
- Secrets (`.env`) are backed up separately in the password manager, never inside the
  database dump.

## 7. Incident and support discipline

- One support channel the owner actually uses (WhatsApp/phone), response target: same
  business day.
- Every incident gets a line in the client ops log: date, symptom, cause, fix,
  product change filed. An incident fixed only on the host and not in the repo is a
  regression waiting for client #2.
