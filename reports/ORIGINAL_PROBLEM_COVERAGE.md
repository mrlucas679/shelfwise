# Original Retail Problem Coverage Audit

This matrix compares the initiating retailer problems with executable implementation evidence. A
status of partial is not presented as solved.

| Problem group | Evidence in current application | Status | Remaining production gap |
|---|---|---|---|
| Expiry, waste, and FEFO | Expiry cascade, FEFO split, markdown simulation, cold-chain routing, HITL | Partial | Canonical batch/lot lineage and donation/write-off/transfer actions |
| Stockouts and overstock | Procurement cascade, reorder policy, supplier ranking, multi-source stock sourcing | Partial | Fleet-wide candidate jobs, open-PO awareness, alert suppression |
| Dirty inventory and source lag | Provenance, quarantine, connector intake, event log | Partial | POS/WMS/ERP/count reconciliation and stale-source correction events |
| Product identity and variants | Canonical products, identifiers, variants, catalogue resolution | Partial | Batch lineage and uncertain-merge review workflow |
| Supplier delays | Supplier cover, ranking, delivery reconciliation, procurement HITL | Partial | Partial deliveries, SLA history, substitution eligibility |
| Cold-chain and outages | Temperature/outage risk, ZAR-at-risk, facilities review | Partial | Sensor coverage and produce-quality state |
| Price and promotion pressure | Price-integrity checks, sales cascade, manager review | Partial | Promotion calendar, margin floor, promo-baseline split |
| Shrink, returns, recalls, misplaced stock | Generic stock states and anomaly plumbing only | Missing | Dedicated events, quarantine/recall/transfer candidates, shelf/backroom/bin state |
| Manager overload and accountability | Bounded attention UI, Critic, HITL, writeback tasks, learning, audit | Partial | Candidate deduplication, SLA aging, suppression receipts |
| Affordability, regulatory, omnichannel | Connector and location foundations | Partial | Staple/regulatory tags and channel reservations/allocation |
| Core cascade spine | Scan -> inventory -> expiry -> demand -> opportunity -> simulation -> critic -> executive -> HITL | Proven | Preserve this spine while adding candidate types |
| Multi-user chat | Trusted tenant/user conversation key, message IDs, idempotency, bounded history, isolation tests | Proven in one process | Postgres/distributed idempotency for multi-replica backend deployment |
| Dual Gemma routing | Independent routine/strong endpoints and credentials, distinct-ID readiness gate | Partial | Load/probe E4B and 31B concurrently on cloud and retain telemetry |

The recorded demo should lead with the proven cascade, agentic tool use, Critic/HITL controls,
learning, tenant isolation, and live sequential soak. It should not claim that recall/shrink,
omnichannel allocation, or dual-model capacity are complete.

