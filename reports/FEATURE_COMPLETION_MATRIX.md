# Feature completion matrix

Audit date: 2026-07-29
Record-level source: [`capabilities/manifest.json`](../capabilities/manifest.json)

“Complete” means the supported start-to-finish path has an implementation plus the automated
evidence linked by the manifest. “External proof” means the receiving code and fail-closed
boundary exist, but current provider, hardware, retailer, or credential evidence cannot be
created by this local repository.

| Product area | Entry-to-outcome path | State and side effects | Automated evidence | Status |
|---|---|---|---|---|
| Chat-first assistant | composer → `/chat` or `/chat/stream` → retrieval plan → role routing → read-only tools → validated answer → conversation memory | Tenant/user-scoped conversation, route/context/model receipts | chat, streaming, memory, grounding, idempotency, browser tests | Complete |
| Workforce identity | first-owner bootstrap/invitation/login/reset → account store → signed session → role guard | Durable opaque account IDs, session-version invalidation, audit records | account, auth, role, activation, recovery, Playwright tests | Complete |
| Guided onboarding | company → store → CSV preview/import → policy confirmation → optional devices/accounts → status | Tenant profile, twin onboarding, source records, exact policy template IDs | onboarding, import, policy, tenant-isolation, browser tests | Complete |
| Connector onboarding | Connections UI → encrypted credentials or signed webhook endpoint → mapper → canonical inbound record | Encrypted secrets, opaque endpoint IDs, dedupe records; secrets never returned | connector transport, credential, webhook, browser tests | Complete |
| Canonical ingest | API/connector/device → `Event.parse_wire` → durable event store → projections → bus → cascade | Idempotent event, inventory/twin projections, published marker | contract, replay, crash-window, tenant/domain tests | Complete |
| Redis/event processing | event stream → worker claim/reclaim → dispatcher → journal → decision/trace | Bounded stream, stale-consumer reclaim, durable run/step journal | Redis contract, worker, retry, dead-letter, concurrency tests | Complete |
| Deterministic decisions | event → typed facts → decision science → Critic/Executive gates → evidence → pending decision | EvidenceObjects, TraceSpans, governed economics, stable IDs | every cascade, guardrail, grounding, identity tests | Complete |
| Agentic decisions | scenario/operational twin → model router → bounded Gemma loop → read-only tools → schema/grounding verification → decision | Exact provider/model/token/tool receipts; live-required fails closed | six cascade suites, runtime routing, tool failure, HTTP error tests | Complete in code; live endpoint proof external |
| HITL and task write-back | role queue → approve/reject → learning event → task receipt → physical completion evidence | Terminal atomic decision, idempotent learning, task-only source-system boundary | auth, race, idempotency, rollback/compensation tests | Complete |
| Adaptive attribution | existing cascade/model receipts → structural success verification → bounded representation → scoped one-class score → existing trace API/UI | Optional trace field only; no replay/write/training side effect | disabled compatibility, calibration, failure, isolation, harness, browser tests | Complete; disabled by default |
| Verified value | completed task receipt → decision annotation → monthly report → Verified value UI | Operational verified outcomes separated from estimates/simulation | report month, tenant, domain, receipt-link tests | Complete |
| Inventory and receiving | normalized stock/sale/shipment → position projection → order/exception queues | Non-negative positions, idempotent receipts, explicit unsupported-unit failures | projection, receiving, procurement, replay tests | Complete |
| Exact-store twin | onboarding/observations → identity/state/provenance projections → fidelity/scenario compare → topology UI | Observed and predicted lanes isolated; no raw media | twin store, projection, calibration, scenario, browser tests | Complete |
| Product identity/search | source product records → product/variant/identifier stores → search/resolve/attention UI | Tenant-scoped master and aliases; bounded results | catalog, search, route/frontend tests | Complete |
| MLOps governance | model/prompt/evaluation receipts → accountability/observability → skill promotion/mining | Promotion requires recorded evaluation; history tenant/domain scoped | registry, promotion gate, playbook, provenance tests | Complete |
| Governed plans/schedules | validated plan → capability registry → journaled runner → read/write policy → compensation | Only registered capabilities execute; write-back remains HITL/task-bound | plan runner, scheduling, compensation, role tests | Complete |
| Multimodal intake | protected voice/image routes → bounded validation → configured service or reviewed fallback | No silent production fallback; upload size/type guarded | multimodal auth, validation, config, failure tests | Complete; optional |
| Cold-chain resilience | structured edge observation → signature/replay checks → diagnosis → event/twin/cascade | Opaque device identity, encrypted secret, no raw media | edge signature, batch recovery, diagnosis, browser registration tests | Complete |
| Retention/recovery | scheduled retention/session capsule → bounded member checks → scoped prune/restore | Simulation history pruned; archive links/special members rejected | retention and safe-extraction tests | Complete |
| One-command shop startup | setup script → preserved/generated secrets → Compose → health wait → first-login output | Refuses unsafe regeneration and reports Docker/health failures | startup/provisioning/config tests | Complete in code; local Docker run not repeated in this pass |
| Production topology | migration → Postgres/Redis/backend/frontend/Nginx → shakedown | RLS stores, Redis worker, secure cookie/origin checks | Exact-SHA CI Compose, migration, Postgres/Redis, public smoke, deployment shakedown | Complete and CI-verified |
| Training pipeline | config/preflight/dataset/collator/train/eval/serving checks | Simulation/training domain guard; immutable revisions | deterministic config/data tests | External hardware proof |
| Live MI300X/Fireworks inference | validated provider config → `/v1/chat/completions` → exact model receipt | No offline fallback in live-required profiles | fail-closed local tests | External endpoint proof |

## Individual capability coverage

All 249 IDs are listed in the canonical JSON manifest with source and test node IDs. The only
non-verified records are the seven external-proof rows listed in `CAPABILITY_MANIFEST.md`;
there are no declaration-only records, orphaned supported routes, unregistered agents, or
unverified supported frontend surfaces in the current snapshot.
