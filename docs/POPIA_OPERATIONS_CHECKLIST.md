# POPIA Operations Checklist

Status: engineering inventory and operating checklist implemented. Legal interpretation,
client contracts, information-officer appointment, and regulatory acceptance remain external.

This is an engineering control checklist, not legal advice.

## Data inventory

| Data class | Purpose | Primary location | Retention/deletion control |
|---|---|---|---|
| Work identity (name, surname, email, position, role) | Authentication and accountability | Tenant-scoped work accounts and account audit | Owner deactivation; documented retention review |
| Store/product/stock/sales/expiry facts | Retail operations and decisions | Tenant-scoped Postgres records and twin | Retention service, client backup, client-approved deletion |
| Connector credentials | Read-only source connection | Encrypted tenant credential store | Owner delete; never returned by API |
| Decisions, approvals, tasks, receipts | HITL evidence and outcome accounting | Tenant-scoped decision/task stores | Audit retention policy; source references stay bounded |
| Derived edge observations | Store state and exceptions | Tenant-scoped twin observations | Raw camera/video is rejected; derived facts only |
| Chat and model-run metadata | User assistance and accountability | Tenant-scoped conversation/MLOps stores | Bounded retention and deletion process |
| Simulation worlds | Testing and evaluation | `world_simulation` domain | Must remain separate from operational client data |

## Before onboarding a client

- [ ] Identify the responsible party, operator roles, information officer, and incident contacts.
- [ ] Record the lawful purpose and minimum fields for every connected system.
- [ ] Approve retention periods for accounts, events, chats, decisions, receipts, backups, and logs.
- [ ] Approve subprocessors, hosting region, cross-border handling, and model providers.
- [ ] Confirm the client agreement, privacy notice, data-subject request route, and breach process.
- [ ] Verify TLS, JWT authentication, least-privilege database role, RLS, encrypted credential
  storage, backup/restore, and external monitoring.
- [ ] Confirm operational data cannot enter training or simulation pipelines.

## Recurring operator checks

- [ ] Review named accounts and least-privilege roles monthly; deactivate leavers promptly.
- [ ] Review connector permissions and rotate/revoke credentials under the client policy.
- [ ] Execute the documented retention/deletion jobs and record exceptions.
- [ ] Test one backup restore monthly without overwriting the live tenant.
- [ ] Review audit and incident receipts for unexpected access or tenant-boundary failures.
- [ ] Verify edge integrations send derived observations only, never raw media.
- [ ] Track each data-subject request from identity verification through completion evidence.

## Data-subject and deletion handling

Authenticate the requester before lookup. Search by stable tenant/account identifiers, not a
broad production export. Record which stores were searched, what was corrected/exported/
deleted, any lawful retention exception, who approved it, and when completion was communicated.
Backups require the client-approved expiry/restore procedure; do not silently rewrite historical
backup media.

## External acceptance boundary

The deployment owner and qualified South African privacy/legal advisers must approve the final
retention schedule, agreements, notices, cross-border basis, regulator-notification procedure,
and client-specific risk assessment. These boxes remain open until signed evidence exists; a
passing software test cannot close them.
