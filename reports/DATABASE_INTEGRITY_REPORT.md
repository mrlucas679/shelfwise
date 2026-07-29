# Database integrity report

Audit date: 2026-07-29
Canonical schema: [`src/shelfwise_storage/schema.sql`](../src/shelfwise_storage/schema.sql)

## Inventory and checks

| Check | Result |
|---|---|
| Canonical tables | 41 |
| Explicit indexes | 35, plus primary/unique constraints |
| Tables with forced tenant RLS | 39 |
| Deliberate non-RLS identity tables | 2 (`shelfwise_edge_devices`, `shelfwise_webhook_endpoints`) |
| Foreign-key declarations | 4, covering twin relationships/observations and cascade steps |
| Repository SQL references | 471 source references across active repositories |
| Local schema/config tests | 32 passed |
| Environment-gated Postgres tests | 11 skipped locally because `SHELFWISE_TEST_DATABASE_URL` is not set |

The full local Python suite passed 951 tests with 21 total environment-gated skips. Exact
implementation commit `abe6924` then passed GitHub CI with fresh Postgres/Redis, canonical
migration, 971 tests, and one skip. No local skip is represented as a database pass.

## Integrity findings

- Tenant-owned tables use tenant keys in primary/unique/index shapes and force RLS.
- `shelfwise_edge_devices` and `shelfwise_webhook_endpoints` intentionally resolve an opaque
  signed identity before a tenant is known. Their owner/list/revoke queries include tenant ID,
  secrets are encrypted/HMAC-bound, and the reason for not using RLS is documented in code/schema.
- Canonical events, inbound records, inventory projections, learning events, decisions,
  write-back tasks, conversations, twin observations, and scenario branches use idempotency or
  composite identity constraints appropriate to their paths.
- Timestamps use `timestamptz` in the canonical schema and UTC-aware application timestamps.
- Operational and simulation data domains are present on stores where cross-domain contamination
  matters.
- Connector/device secrets are encrypted or one-way verified; plaintext application secrets are
  not schema fields.
- Audit and terminal decision records are updated through guarded repository methods; approved/
  rejected decision transitions are idempotent.
- Fresh-database creation and production migration are owned by the canonical schema/Compose
  migration service; module-local schema creation remains a local zero-config compatibility path.
- Historical schema statements were not removed: the canonical file contains additive/repair
  statements required for existing database upgrades.

## Changes in this pass

Adaptive attribution adds no table, column, index, or migration. It reuses the bounded
`TraceRegistry` and existing durable `ModelRun`/Decision/Event receipts, so feature-disabled
deployments have no database behavior change.

## Recovery boundary

Production upgrades require a backup/session capsule, migration job completion, readiness
checks, and rollback/restore procedures in the release runbook. CI verified a fresh exact-head
database and migration; a real existing-database upgrade still requires the controlled operator
procedure and rollback drill.
