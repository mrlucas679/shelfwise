# Things that needs to be implemented

Last updated: 2026-07-08

## Purpose

This document captures the implementation and research backlog needed to move ShelfWise from a strong hackathon demo into a useful retail operations system that can handle real workload: many products, many stores, many source systems, many daily events, and many possible actions.

This is not about adding subagents. The issue is workload shape.

- A demo can reason over 4 planted products.
- A real retailer can carry hundreds of thousands of products, variants, pack sizes, aliases, barcodes, GTINs, batches, suppliers, and store-level inventory positions.
- Each perishable product can have multiple batches with different expiry dates, different locations, different cold-chain exposure, and different sell-through risk.
- The system must not call an LLM or run a full agent cascade for every product every day.

The production harness needs to become a scalable decision factory:

`ingest all data -> normalize identity -> update state -> generate candidates -> compute math evidence -> rank exceptions -> run Critic/Executive only where action is valuable -> create HITL task or monitor -> learn from outcome`

## Research Stance

The research stance for this file is adversarial.

Do not assume ShelfWise works because the golden cascade works. The golden cascade proves one path:

`stage4_loadshedding_x_payday_yoghurt -> evidence -> critic -> executive -> HITL`

That is useful, but it is not enough to prove the application solves a real retailer's daily problem.

The question for every feature is:

Does this reduce waste, prevent stockouts, expose bad data, save manager time, protect margin, improve availability, or create a safer operational decision?

If the answer is no, it is noise.

ShelfWise should not win by showing "more agents". It should win by showing:

- fewer bad recommendations
- fewer irrelevant alerts
- fewer unsupported actions
- faster discovery of real exceptions
- clearer evidence for every decision
- lower model cost per useful recommendation
- proof that it can handle many SKUs without asking a model to reason over everything

## Hackathon Compliance And Judging Gates

Track 3 is the Unicorn/Open Innovation track. The participant guide says Track 3 requires:

- GitHub repository URL
- demo video
- slide deck
- live demo or hosted URL optional but recommended
- no Docker image required for Track 3

Track 3 is pre-screened automatically for:

- AMD resource usage
- originality

Then human judges review the project.

The FAQ says a live API endpoint is not required for judging. A demo video, presentation deck of about five slides, and GitHub URL are enough. This means the proof package is not optional. The repo and deck must carry the evidence if the hosted app is unavailable.

Hard constraints:

- Submission deadline: CET 6 pm July 11, 2026.
- Demonstrate AMD compute usage.
- Do not hardcode or cache answers to specific inputs.
- All judge-facing responses should be in English.
- Do not introduce paid services or non-MIT-clean dependencies.
- Do not add secrets to the repo.
- Do not overclaim unsupported features.
- If a live endpoint exists, it should be cheap to keep alive or easy to restart.

Judging usefulness gate:

- If a manager would still have to inspect 10,000 rows after using ShelfWise, the application has failed.
- If the system cannot explain why one product was prioritized over another, it has failed.
- If it produces recommendations without source quality, batch/lot identity, and math evidence, it has failed.
- If it cannot run against data it did not see during the demo, it has failed the originality/usefulness test.
- If it cannot prove AMD-backed inference or AMD-backed benchmark work, it risks failing pre-screening.

## AMD Notebook And vLLM Evidence

New hackathon operating facts:

- AMD notebook quota is now 8 GPU hours per 24 hours, raised from 4 hours.
- `notebooks.amd.com/hackathon` access is team-based. Even solo builders must create or join a team on lablab.ai. "Team not registered" means the team record is missing, not that the notebook is broken.
- Persistent notebook storage is `/workspace`.
- The terminal warning `groups: cannot find name for group ID 109` is harmless container/user metadata noise unless it blocks a command.

Observed notebook environment from the attached terminal log:

- `rocm-smi` reports one AMD GPU with about 48 GiB VRAM.
- `rocm-smi` reports `GFX Version: gfx1100`, so this specific pod is ROCm-compatible AMD hardware, not literal MI300X/gfx942.
- `/opt/rocm` and `/opt/rocm-7.2.1` exist.
- Default `/usr/bin/python3` does not import `vllm`.
- vLLM exists under `/opt/venv/bin/vllm`.
- After `source /opt/venv/bin/activate`, Python imports:
  - `vllm 0.16.1.dev0+g89a77b108.d20260318`
  - `torch 2.9.1+gitff65f5b`
  - `torch.cuda.is_available() == True` through ROCm/HIP compatibility
- Hugging Face is not logged in and `HF_TOKEN` is empty, so prefer ungated models unless a token is deliberately configured.
- `Qwen/Qwen2.5-14B-Instruct` started successfully through vLLM with OpenAI-compatible routes.
- `/v1/models` returned the served aliases `shelfwise-routine` and `shelfwise-strong`.

Implementation implications:

- The AMD proof script must always activate `/opt/venv` before checking or starting vLLM.
- Do not use `/usr/bin/python3` to check vLLM availability on the notebook.
- Use ungated, license-compatible models unless a token is deliberately supplied.
- Record actual GPU facts honestly. Say "AMD Developer Cloud ROCm/vLLM proof" unless the hardware is confirmed MI300X.
- Treat `vllm_mi300x` in code as a provider-kind name for the AMD Developer Cloud/vLLM path, not as a literal hardware claim unless confirmed.
- Keep Fireworks as the reliable public-demo fallback.

Known command pitfall from the log:

The failing launch used `-- host` and `-- port` with spaces after the dashes, which vLLM parsed as invalid arguments. Use normal flags:

```bash
source /opt/venv/bin/activate
mkdir -p /workspace/vllm_run
cd /workspace/vllm_run
python -c "import secrets,pathlib; k=secrets.token_hex(24); pathlib.Path('api_key.txt').write_text(k); print('KEY_PREFIX', k[:8])"

setsid nohup vllm serve Qwen/Qwen2.5-14B-Instruct \
  --host 0.0.0.0 \
  --port 8001 \
  --dtype bfloat16 \
  --max-model-len 8192 \
  --gpu-memory-utilization 0.85 \
  --served-model-name shelfwise-routine shelfwise-strong \
  --api-key "$(cat /workspace/vllm_run/api_key.txt)" \
  > /workspace/vllm_run/vllm_server.log 2>&1 < /dev/null &
```

Smoke checks:

```bash
tail -n 60 /workspace/vllm_run/vllm_server.log
curl -s http://127.0.0.1:8001/v1/models \
  -H "Authorization: Bearer $(cat /workspace/vllm_run/api_key.txt)"
```

Backlog requirements from this evidence:

- Add an AMD notebook quickstart to the runbook.
- Add an AMD proof receipt containing GPU info, ROCm version, vLLM version, model id, served aliases, route URL, and smoke output.
- Add a cost/quota note: plan benchmark and recording work around the 8-hour window and stop idle notebooks.
- Add a warning not to leak `api_key.txt`.
- Add a step to use `/workspace` for persistent proof logs.

## External Research Grounding

Patterns that matter for the next implementation phase:

- GS1 Application Identifiers support product data beyond GTIN, including batch/lot and expiration dates. Expiry risk cannot be modeled only as `sku + expiry_date`; product identity and batch identity are separate facts.
- GTINs identify trade items across packaging levels and variations. "Milk" is not enough; low-fat milk, full-cream milk, 1L, 2L, case pack, and pallet pack can be separate identifiers.
- Retail systems distinguish product-level inventory from local/store inventory and model primary products, variants, and local inventory.
- Variant attributes must be complete and consistent for filtering, search, and recommendations.
- PostgreSQL partitioning is needed for large event/history/model-run tables.
- Redis Streams consumer groups are appropriate for event logs, but need batching, retry, pending-message recovery, and dead-letter handling.
- SAP/Odoo-style shelf-life and batch-management patterns show that perishables need batch-level shelf-life expiration date data, remaining shelf life, and FEFO stock-removal rules.
- Retailer problems include food waste, stockouts, overstock, shrink, dirty inventory, supply-chain disruption, cold-chain/logistics, margin pressure, labor overload, and data silos.

## Current Implementation Reality

### Strong Foundation

- The demo cascade is coherent: scan -> inventory -> expiry -> demand -> opportunity -> simulation -> critic -> executive -> HITL.
- Evidence contracts exist: `EvidenceObject`, `SourceRef`, `Decision`, and `TraceSpan`.
- Decision-science functions are pure and testable.
- Redis Streams and Postgres-backed stores exist as optional backends.
- Worker journal, plan validation, model/prompt registry, MLOps accounting, tenant facts, writeback tasks, observability snapshot, and synthetic data scaffolding exist.
- Critic rejection and HITL-gated writeback posture exist and should remain central.

### Internal Research Warnings

- The demo is the presentation path, not the application ceiling.
- The system must cover many SKUs, stores, roles, and concurrent scenarios.
- Static seeded data is not enough; the app needs virtual-store simulation with many SKUs and evolving state.
- Source data is a claim, not truth. It needs provenance, validation, source quality, and raw hashes.
- SKU, barcode, GTIN, variant id, internal item id, material id, and stock code are not interchangeable.
- Product identity, batch/lot identity, and expiry data are foundational.
- Agents must not invent forecasts, spoilage probabilities, reorder quantities, markdown economics, or action rankings.
- Mathematical tools should produce numerical truth; agents explain, critique, prioritize, and route.
- The Critic should reject missing math evidence, missing source coverage, stale data, high-risk writeback without approval, and contradictions.
- Alert quality is part of usefulness. Too many recommendations is as bad as no recommendations.

### Current Scale Gaps

- Seed data is tiny: 4 products, 4 stock rows, 8 sales rows, and 3 suppliers.
- Current synthetic catalog estimates fall short of 500k in the running implementation.
- Product state is mostly JSONB at `(tenant_id, sku, location_id)` grain.
- `ProductMaster` is too thin for variants, aliases, pack sizes, units, GTIN hierarchy, shelf-life policy, storage policy, allergens, supplier rules, and regulatory attributes.
- The worker consumes one event at a time.
- Redis consumption uses small single-message reads.
- Failed worker events can be acknowledged after failure unless replay/dead-letter discipline is added.
- The cascade router still contains demo-specific keys such as `sku == "4011"` and `supplier == "dairyco"`.
- Event and inbound tables are not partitioned.
- Hot query fields are not consistently promoted from JSONB into indexed columns.
- Decision listing and observability rely on list-style APIs that will not hold for millions of rows.
- The frontend now has the correct chat-first shape: the sidebar is a bounded attention entrypoint,
  and product-scale work opens in a workspace. The remaining scale gap is the production data layer
  behind that workspace: indexed/paginated catalogue search, batch/lot expiry rows, FEFO movement
  history, and exception ranking over real store volumes.

## Scale Assumption

Design against:

- 500,000 product and variant records.
- 20 to 100 stores or locations.
- 1 to 10 active inventory positions per product per store.
- Multiple active lots for perishable products.
- Millions of sales rows per month.
- Continuous inventory updates, price changes, deliveries, waste, returns, transfers, and supplier events.
- A small percentage of product-location-batch rows becoming recommendations.

The system should process most work as cheap data operations and reserve agentic reasoning for ranked exceptions.

## Retailer Problem Coverage Matrix

All problems should enter the existing workflow:

`source data -> canonical state -> candidate generation -> math/tool evidence -> Critic -> Executive -> HITL/task/monitor -> outcome learning`

Do not create a separate workflow for each problem. Add candidate types, policies, tools, source fields, evidence rules, and UI views around the existing cascade.

| Retailer problem | Why it matters | Existing ShelfWise coverage | Missing implementation inside existing workflow |
|---|---|---|---|
| Expiring perishables and food waste | Direct margin loss, food-security issue, brand risk | Golden expiry/markdown cascade, FEFO split tool, expiry risk tool, markdown simulation | Batch/lot expiry state, product shelf-life policy, donation/transfer/write-off actions, expiry-data quality scoring |
| Overstock | Ties up cash, drives markdowns, creates waste | Inventory and opportunity cascade can reason over demo overstock | Fleet-wide overstock scoring, margin-aware markdown ladders, suppression of low-value alerts |
| Stockouts and shelf availability | Lost sales and disappointed customers | Reorder math and stockout synthetic category exist | Daily stockout candidate job, shelf/backroom distinction, committed/reserved stock, open PO awareness, substitution rules |
| Inventory distortion | Bad inventory creates both stockouts and overstocks | Event ingest, source refs, inventory state | POS/WMS/ERP/count reconciliation, stale-source detection, typed hot fields, correction events |
| Product identity confusion | Wrong product mapping corrupts every downstream decision | Thin ProductMaster, GS1 parser, synthetic catalog | Canonical product/variant/identifier/alias model, fuzzy matching, human review for uncertain merges |
| Variant and pack-size confusion | "Milk" can mean many sellable units | Synthetic catalog has variants/pack grammar | Runtime variant/pack model, unit conversion, pack hierarchy, case-pack rules |
| Missing batch/lot/expiry capture | Expiry intelligence becomes approximate | Basic expiry fields and GS1 parser | Data-completion tasks, scanner-assisted expiry capture, perishable-category evidence requirements |
| Dirty source data | Bad data creates confident bad recommendations | Inbound records, validation, source quality, raw hashes | Source-quality ranking, quarantine dashboard, source-specific scorecards |
| POS sync delay and offline sales | Demand and stock can be stale | POS sale events and sales cascade exist | Source-lag detection, late-event correction, stockout-adjusted sales |
| Supplier delays | Causes stockouts and unreliable replenishment | Supplier cover demo and supplier ranking tools exist | Open PO model, fill-rate history, partial delivery handling, supplier SLA score |
| Poor supplier substitution | Unsupported supplier switches create risk | Critic rejection blocks unsupported supplier switch | Supplier profile completeness, MOQ/case-pack rules, substitute eligibility |
| Cold-chain failure | Perishables lose shelf life and can become unsafe | Cold-chain demo and stock-at-risk tools exist | Map products/batches to cold assets, sensor coverage score, cold-chain penalty in every expiry candidate |
| Energy and generator events | South African retail has outage/operating-cost exposure | Cold-chain resilience and outage simulation exist | Current energy status source, generator maintenance state, tariff/cost-aware scheduling |
| Produce quality and blemished stock | Culling safe produce causes waste | General expiry/markdown path | Produce-quality state, blemish grade, donate/discount/repurpose policy |
| Promotion spikes | Promotions distort demand and replenishment | Synthetic category and calendar uplift concept exist | Promotion calendar, baseline-versus-promo demand split, post-promo overstock detection |
| Price and margin pressure | Wrong discounts hurt narrow margins | Markdown simulation and Money contracts | Margin floors, category elasticity, price-change audit trail |
| Shrink, theft, voids, and operational loss | Loss prevention is a retail priority | Anomaly detector can be extended | Refund/void/shrinkage feeds, robust anomaly jobs, evidence rules to avoid accusing people without proof |
| Returns and damaged goods | Return/damage changes sellable stock | StockState has damaged/expired states | Return/void/damage event models, sellable-versus-unsellable state |
| Recalls and compliance holds | Unsafe stock must be found quickly | No complete recall workflow | Recall candidate, affected product/batch search, quarantine task, audit receipt |
| Store transfers | Move stock to avoid waste or stockouts | Multi-store transfer synthetic category exists | Transfer simulation, transport cost/time, receiving capacity, cold-chain suitability |
| Misplaced stock/backroom stock | Product exists but is not on shelf | Basic inventory state only | Shelf/backroom/bin model, shelf-gap candidate, staff task before reorder |
| Labor and manager overload | A useful system reduces work | Executive emits one prioritized action | Workload caps, role queues, alert suppression, SLA aging, useful-actions-per-day metric |
| Alert fatigue | Weak alerts destroy trust | Critic can downgrade claims | Candidate suppression, duplicate detection, "why not shown" receipts, false-urgency eval |
| Approval and accountability | High-risk actions need review | HITL approve/reject exists | Role-scoped approval policies, escalation matrix, approval SLA, audit export |
| Writeback safety | Wrong ERP/POS writes cause harm | Recommend-only, task-style writeback | Per-system writeback capability profiles, rollback instructions, dry-run receipts |
| Customer affordability | Staple availability matters | Not directly modeled beyond markdown | Product strategy tags: staple, zero-rated, premium, private label |
| Health/allergen/regulatory attributes | Food retail needs safe product handling | Synthetic catalog includes some attributes | Runtime regulatory/allergen flags and recall/compliance hooks |
| Omnichannel inventory | Online/store inventory can diverge | Shopify/Square connectors exist | Channel inventory state, reservations, pickup/delivery allocation |
| Data silos | ERP/POS/WMS disagree | Connector layer and SourceRef exist | Reconciliation jobs, source priority policy, source disagreement evidence |
| Integration cost | Independent retailers cannot afford heavy integration | CSV/webhook/mock strategy is correct | BYO-data import, connector capability matrix, mapping wizard, sample fixtures |
| Hostile inputs | Source payloads and scans can attack AI paths | Gateway validation exists | Threat tests per connector/multimodal path, prompt-injection checks |
| Forecast drift and seasonality | Old demand assumptions become wrong | Demand baseline and TSFM guardrail exist | Backtesting, WAPE by category, drift alerts, promotion covariates |
| Outcome measurement | Value must be tied to avoided loss | Decision log, learning events, recovered money metric | Attribution rules, counterfactual baseline, disputed-outcome handling |

Coverage rule:

If a problem does not fit one of these workflow slots, do not invent a new architecture. First ask whether it is:

- a new source field
- a new candidate type
- a new deterministic tool
- a new policy
- a new Critic rule
- a new HITL task type
- a new UI filter/view
- a new eval scenario

Only after those fail should the architecture be reconsidered.

## Existing Agents Need More Skills, Not More Agents

### Inventory Agent Skills

- Reconcile SKU, GTIN, barcode, variant, pack size, and source-system item ids before using stock numbers.
- Separate on-hand, available, reserved, damaged, expired, in-transit, committed, and counted quantities.
- Understand inventory by product, variant, store, storage location, shelf/backroom/bin, and batch/lot.
- Compute stockout risk from demand, lead time, service level, open orders, and committed units.
- Detect stale inventory feeds and source lag.
- Detect contradictions across POS, WMS, ERP, and stock counts.
- Recommend reorder only when supplier, lead-time, MOQ, case-pack, and current open orders support it.
- Create data-completion tasks when stock state is missing or contradictory.

### Expiry Agent Skills

- Score risk per batch/lot, not only per SKU.
- Use FEFO ordering.
- Use product shelf-life policy by category and storage requirement.
- Use received date, production date, best-before date, expiry/SLED, and source confidence.
- Degrade confidence when expiry data is absent, old, inferred, or contradictory.
- Ask for missing expiry capture instead of recommending markdown when evidence is weak.
- Combine sell-through risk with cold-chain penalty and source quality.
- Distinguish markdown, transfer, donate, hold, quarantine, and write-off actions.

### Demand Agent Skills

- Build demand from stockout-adjusted sales, not raw sales only.
- Handle intermittent demand and long-tail SKUs.
- Account for weekday, payday, seasonality, holidays, promotions, local events, and source lag.
- Separate baseline demand from promotion-driven demand.
- Group related variants only when substitution is valid.
- Emit forecast uncertainty, not only a point estimate.
- Shadow-test TSFM/foundation forecasts against the transparent baseline before using them in decisions.

### Opportunity Agent Skills

- Rank opportunities by expected value, urgency, reversibility, confidence, and workload.
- Avoid actions that create more manager work than value.
- Respect margin floors, markdown ladders, promotion calendars, and category policies.
- Compare markdown, transfer, reorder, hold, write-off, quarantine, and data-completion actions.
- Suppress duplicate or overlapping recommendations.
- Explain why an opportunity was not selected.
- Prefer monitor when evidence quality is thin.

### Simulation Agent Skills

- Run deterministic what-if calculations for multiple candidate actions.
- Simulate markdown, transfer, reorder, hold, write-off, and quarantine where data supports it.
- Include uncertainty bands for demand and cold-chain risk.
- Include operational capacity: staff can only process so many markdowns/transfers/supplier calls.
- Persist simulation inputs and outputs as evidence.
- Refuse simulation when core inputs are missing.

### Critic Agent Skills

- Reject numerical claims without math tool evidence.
- Reject unsupported source-system claims.
- Reject actions based on stale data.
- Reject recommendations where product identity is ambiguous.
- Reject markdown when expiry is not batch-level or source quality is low.
- Reject supplier switch when backup supplier evidence is missing.
- Reject high-risk writeback without HITL.
- Detect contradictions between demand, expiry, stock, supplier, and cold-chain evidence.
- Penalize alert noise and downgrade low-value or duplicate recommendations.
- Force data-completion task when missing data is the true blocker.

### Executive Agent Skills

- Produce one prioritized action per role, not an unbounded list.
- Explain why the action matters today.
- Show expected ZAR impact, urgency, reversibility, and evidence quality.
- Show what evidence would change the recommendation.
- Show what was suppressed and why.
- Route work to inventory, procurement, cold-chain, store manager, analyst, or auditor roles.
- Respect role workload and HITL backlog.
- Prefer monitor or collect-missing-data over weak action.

### Orchestrator And Worker Skills

- Generate candidates from state changes before invoking expensive reasoning.
- Route by candidate type, risk, source quality, value, and urgency.
- Use context assembly per candidate.
- Enforce token/cost budgets per decision.
- Batch low-risk scoring work.
- Retry and dead-letter failed events.
- Preserve idempotency by product/location/batch/action.
- Track queue lag and backpressure.
- Stop repeated recommendations while a HITL task is pending.

### Procurement And Supplier Skills

- Track supplier lead time reliability, fill rate, partial deliveries, open purchase orders, and SLA.
- Respect MOQ, order cycles, delivery days, and case packs.
- Compare alternatives only when backup supplier evidence exists.
- Account for product substitution rules.
- Avoid supplier switch actions where evidence is thin.

### Sales/POS Skills

- Reconcile POS sales with inventory decrement.
- Handle returns, voids, discounts, shrinkage, and offline POS sync.
- Detect price anomalies and receipt mismatch.
- Distinguish true demand from promotional uplift.
- Use receipt names and barcode/PLU data to help identity resolution.

### Cold-Chain And Facilities Skills

- Map products and batches to storage assets.
- Track asset temperature, door events, generator status, power events, and maintenance state.
- Compute time-to-unsafe and stock-at-risk.
- Separate measured cold-chain evidence from schedule assumptions.
- Trigger move-stock, dispatch, quarantine, or monitor actions.
- Link cold-chain penalties into expiry risk.

### Scanner And Multimodal Skills

- Parse GS1 Application Identifiers for GTIN, lot, and expiry where available.
- Parse receipt names and product labels into uncertain candidates, not final truth.
- Emit source confidence and missing fields.
- Create human data-completion tasks for low-confidence scans.
- Never auto-act from OCR/VLM output alone.
- Support manual correction and learning from corrections.

### MLOps, Memory, And Skill Ratchet

- Convert repeated validated outcomes into review-gated skills or policy changes.
- Keep tombstones and rollback for learned rules.
- Track which skill/policy version produced each recommendation.
- Evaluate learned rules before activation.
- Detect drift in product policies, source data, demand patterns, and model output.
- Reject self-improvement without evidence.

## P0 Implementation Work

### 1. Product Master And Variant Model

Implement normalized product identity tables/models:

- `products`
- `product_variants`
- `product_identifiers`
- `product_aliases`
- `product_packaging_levels`
- `product_categories`
- `product_storage_rules`
- `product_shelf_life_rules`
- `product_supplier_links`

Requirements:

- Support internal SKU, source-system SKU, barcode, GTIN, PLU, supplier item code, and aliases.
- Represent primary product versus variant.
- Represent pack size, unit, case pack, pallet pack, and sellable unit.
- Support exact and fuzzy search for product names.
- Preserve source mappings across SAP, Syspro, Odoo, POS, WMS, CSV, scanner inputs.
- Add human review for uncertain merges.

### 2. Batch, Lot, And Expiry Model

Implement:

- `inventory_positions`
- `inventory_batches`
- `batch_lots`
- `expiry_observations`
- `waste_events`
- `stock_adjustments`

Requirements:

- tenant, store/location, SKU/product, variant, batch/lot, GTIN/barcode
- on-hand, reserved, damaged, expired, in-transit
- received date, production date, best-before date, expiry/SLED
- source system, source record id, source confidence, last observed timestamp
- FEFO candidate selection
- expiry conflict detection
- batch merge/split handling

### 3. Bulk And Incremental Ingestion

Requirements:

- Bulk import product masters, stock snapshots, sales history, supplier tables, price lists, batch records.
- Incremental delta import using source high-water marks.
- Idempotent upserts by source system, source object id, and payload hash.
- Quarantine invalid records.
- Promote hot fields out of JSONB into typed columns.
- Preserve raw payloads for audit.
- Stream large CSV imports without materializing everything.
- Use database bulk loading for large imports.

### 4. Production Event/State Architecture

Implement:

- Partitioned event tables by time and tenant/event type.
- Partitioned inbound record tables.
- Typed event columns for tenant, event type, timestamp, SKU, location, batch, source system.
- Current-state tables for product, inventory, batch, supplier, price, demand features.
- Append-only raw event history.
- Materialized feature snapshots for daily scoring.

Do not rely on JSONB scans for core operational queries.

### 5. Candidate Generation Before Agent Reasoning

Candidate types:

- expiry risk
- stockout risk
- overstock risk
- supplier delay risk
- cold-chain risk
- delivery mismatch
- price/promotion anomaly
- sales spike
- slow mover
- missing batch/expiry data
- conflicting product identity
- shrink/loss anomaly
- recall/compliance hold
- shelf-gap/backroom mismatch

Requirements:

- Score all candidate rows with deterministic math first.
- Rank by financial exposure, urgency, confidence, and actionability.
- Keep only top N per store/category/day for expensive review.
- Suppress repeated recommendations already pending HITL.

### 6. Scale-Ready Worker Runtime

Requirements:

- Batch consume events.
- Configurable worker count and consumer name.
- Retry policy with exponential backoff.
- Dead-letter stream/table.
- Pending-message recovery.
- Idempotent processing keys.
- Per-tenant and per-store backpressure.
- Queue lag metrics.
- Worker leases/locks for same SKU/location/batch.
- No ack-on-failure unless failure is durably stored and replayable.

### 7. Partitioning, Indexes, Retention

Requirements:

- Partition `shelfwise_events` by event date.
- Partition `shelfwise_inbound_records` by ingested date.
- Consider partitioning `shelfwise_model_runs`, `cascade_runs`, and `cascade_steps`.
- Add typed indexes for hot access patterns.
- Add BRIN indexes for append-only time-series where appropriate.
- Add GIN indexes only where JSONB querying is unavoidable.
- Add retention/archive policies.
- Add migration tests for partitioned tables and RLS.

### 8. Context Assembler

Implement `ContextAssembler`.

Inputs:

- product identity
- variant identity
- store/location
- batch/lot
- recent sales window
- current stock state
- expiry observations
- supplier status
- open HITL tasks
- previous decisions
- tenant facts
- source quality

Outputs:

- compact context bundle
- cited source refs
- token estimate
- missing-data list
- confidence/evidence score
- decision-specific context manifest

## P1 Implementation Work

### 9. Product-Policy Registry

Policies:

- dairy chilled expiry policy
- bakery same-day markdown policy
- produce quality/spoilage policy
- frozen cold-chain breach policy
- ambient long-life reorder policy
- regulated hold/quarantine policy
- promotional stock policy
- staple availability policy

Fields:

- expiry method
- markdown ladder
- minimum margin
- FEFO required
- cold-chain sensitivity
- reorder service level
- supplier lead-time model
- HITL threshold
- auto-monitor threshold

### 10. Fleet-Wide Scoring Jobs

Jobs:

- expiry risk
- stockout risk
- overstock/slow-mover
- supplier delay exposure
- cold-chain exposure
- missing-data scoring
- contradiction scoring
- shrink/loss anomaly
- recall/compliance affected-stock search
- value-at-risk aggregation

Requirements:

- Run over product-location-batch state.
- Process in chunks.
- Persist scores and score deltas.
- Trigger candidates only when thresholds are crossed.
- Emit run summary metrics.

### 11. Demand And Forecast Feature Store

Implement:

- `daily_product_sales`
- `product_store_demand_features`
- `forecast_runs`
- `forecast_outputs`
- `promotion_calendar`
- `holiday_calendar`
- `stockout_adjusted_sales`

Requirements:

- Rolling averages and demand variance.
- Intermittent demand detection.
- Stockout adjustment.
- Promotions/holiday covariates.
- Transparent baseline as fallback.
- TSFM shadow tests before live use.

### 12. Identity Resolution Workflow

Requirements:

- Exact match by GTIN/barcode/SKU/source id.
- Alias match by source-system mapping.
- Fuzzy name match.
- Unit/pack-size-aware matching.
- Brand-aware matching.
- Confidence score.
- Human review queue.
- Merge/split audit trail.

### 13. Evidence Quality Scoring

Fields:

- source coverage
- freshness
- agreement
- missing critical fields
- data quality
- model confidence
- math confidence
- financial exposure
- action reversibility
- HITL requirement

Rules:

- Thin evidence downgrades to monitor.
- Contradictory evidence triggers Critic.
- Missing expiry/batch data creates a data-completion task.

### 14. Exception Queue And HITL Workload

Requirements:

- Top exceptions by store, category, value, and urgency.
- SLA clock.
- Deduplication.
- Bulk approve/reject only for low-risk reversible actions.
- Escalation path for high-risk actions.
- Workload cap per role.
- Reason for suppression when an item is not shown.

### 15. Scale Observability

Metrics:

- product records by tenant
- active inventory positions
- active batches/lots
- events ingested per minute
- invalid record rate
- queue lag
- oldest pending event age
- worker throughput/failure rate
- dead-letter count
- scoring job duration and rows processed
- candidate count by type
- recommendation count by type
- HITL backlog
- average evidence quality
- model calls per 1,000 products
- cost per 1,000 products
- value at risk discovered
- value recovered
- stale data by source system

### 16. Scale Evaluation Harness

Scenarios:

- 500,000 products.
- Multiple stores and categories.
- Multiple suppliers.
- Multiple batches per perishable product.
- Duplicate product names.
- Ambiguous aliases.
- Missing/conflicting expiry dates.
- Late POS events.
- Stock snapshots arriving after sales.
- Supplier delay shock.
- Cold-chain incident.
- Promotion spike.
- Store transfer.
- Product recall.
- High-risk writeback attempted without approval.

Metrics:

- ingest throughput
- memory usage
- database size
- queue lag
- scoring duration
- p50/p95/p99 endpoint latency
- candidate reduction ratio
- model call count
- cost estimate
- false positive rate
- false negative rate for planted risks
- evidence completeness

### 17. Frontend Information Architecture

Views:

- exception inbox
- store health view
- category risk view
- product search with server-side pagination
- variant-aware product detail
- batch/lot expiry table with virtualization
- source confidence panel
- evidence trace panel
- suppressed recommendation panel
- data-quality work queue
- HITL review queue
- operations dashboard

Rules:

- No unbounded client-side arrays.
- Server-side search/filtering.
- Virtualized large tables.
- Clear loading/empty/error/stale/partial states.

## P2 Implementation Work

### 18. Model Routing And Cost Controls

Requirements:

- No model call per product.
- No model call for routine low-risk scoring.
- AMD-hosted model for proof and batch summarization where useful.
- Strong model only for Critic, Executive, ambiguous evidence, or high-risk recommendations.
- Token budget per decision.
- Cost budget per tenant/day.
- Model-call audit by action type.
- Provider fallback trace.

### 19. Data Governance And Tenant Controls

Requirements:

- Tenant-scoped RLS on all new tables.
- Per-tenant credentials isolated outside the repo.
- Connector allow-list per tenant.
- Data retention policies.
- Audit logs for corrections, product merges, and policy changes.
- PII minimization.
- Fail-closed connector ingestion.
- No raw stack traces to end users.

### 20. Reconciliation And Correction Loops

Requirements:

- POS sales versus inventory decrement.
- Delivery received versus supplier invoice.
- WMS stock versus POS availability.
- Batch expiry observations versus product master shelf-life.
- Missing feed detection.
- Duplicate feed detection.
- Correction events with provenance.

### 21. Multi-Tenant And Multi-Store Scheduling

Requirements:

- Per-tenant scoring schedule.
- Per-store scoring windows.
- Job priority.
- Maximum concurrent jobs per tenant.
- Backoff when source systems lag.
- Manual replay/pause.
- Job history and receipts.

### 22. API Pagination And Query Contracts

Requirements:

- Cursor pagination.
- Limit caps.
- Sort keys.
- Filter contracts.
- Stable query indexes.
- Avoid total counts for huge tables unless cached.
- Separate summary endpoints from detail endpoints.

### 23. Load Testing And Capacity Reports

Artifacts:

- `scale_profile_500k.json`
- synthetic 500k catalog generator config
- load-test script
- ingestion benchmark
- scoring benchmark
- worker benchmark
- database size estimate
- cost estimate
- generated capacity report

### 24. Usefulness And Noise Evaluation

Metrics:

- recommendation precision
- duplicate recommendation rate
- alert suppression rate
- high-risk HITL capture rate
- unsupported-action rejection rate
- false urgency rate
- useful-actions-per-day per role
- manager review workload
- time-to-first-useful-action
- data-completion task rate
- recommendation value versus operational effort

Scenarios:

- 10,000 low-risk products with no action needed.
- 500 products with missing expiry data.
- 100 products with duplicate names but different variants.
- 50 products with stale POS data.
- 25 real expiry risks.
- 10 supplier delay risks.
- 5 cold-chain risks.
- 3 high-value but unsupported recommendations.

Expected result:

A small ranked work queue, not 10,000 alerts.

### 25. Hackathon Proof Package

Artifacts:

- README section: problem, useful outcome, architecture, how to run, AMD compute usage, limitations.
- Five-slide deck:
  1. Problem: waste, stockouts, dirty data, manager overload.
  2. Solution: evidence-first operations brain that prioritizes exceptions.
  3. Technical proof: harness primitives, agent skills, math tools, Critic, HITL, source provenance.
  4. AMD proof: ROCm/vLLM notebook benchmark or recorded provider trace plus Fireworks fallback.
  5. Usefulness and roadmap: scale benchmark, 500k catalog, connector path, what is real now.
- Demo video script:
  - useful action, not generic chat
  - numeric proof rail
  - Critic rejection
  - HITL approval
  - source/evidence trace
  - AMD compute proof
  - honest limitation: current demo seed is small, scale substrate is being built
- Repository artifacts:
  - `harness-evidence.md`
  - scale/readiness matrix
  - 500k benchmark target
  - source-system connector capability matrix
  - AMD notebook/vLLM proof receipt

### 26. Existing-Doc Contradiction Cleanup

Contradictions:

- Some older docs frame scaling/connectors as post-hackathon, while current instructions say full MVP capability is the target.
- Some older planning docs say Track 3 needs a container/live URL, while the participant guide says no Docker image is required and hosted URL is optional.
- Some docs say the catalog generator targets 50k-500k SKUs, while the current running generator falls short.
- Some docs call domain files code-complete, but not every domain is fully represented in running `src/` code.
- Some docs say MI300X when the attached pod evidence shows 48GB gfx1100 hardware. Use "AMD Developer Cloud ROCm/vLLM" unless MI300X is confirmed.

Required cleanup:

- Add a "current truth" section to README or `IMPLEMENTATION_STATUS.md`.
- Mark stale post-hackathon language where it conflicts.
- Distinguish "blueprint exists" from "running code exists".
- Distinguish "demo proof exists" from "scale proof exists".
- Map participant-guide requirements to repo artifacts.

## P3 Implementation Work

### 27. Harness Receipt Artifact

Receipt fields:

- trigger event
- product identity
- store/location
- batch/lot
- context manifest
- source refs
- candidate score
- evidence quality score
- tools called
- model route
- token estimate
- trace spans
- Critic verdict
- Executive action
- HITL state
- writeback task
- eval result

### 28. 10-Primitives Matrix

Track:

1. Instructions
2. Context Delivery
3. Durable State
4. Tool Interface
5. Execution Environment
6. Model
7. Orchestration
8. Bounded role workers, not new subagents
9. Skills & Procedures
10. Verification & Observability

For each:

- current implementation
- missing production work
- evidence artifact
- test coverage
- demo/deck proof

### 29. Public Proof Package

Required artifacts:

- harness primitive matrix
- scale benchmark report
- AMD ROCm/vLLM proof
- golden cascade receipt
- critic rejection receipt
- 500k synthetic run summary
- product identity diagram
- batch/expiry data model diagram
- cost-per-decision explanation
- model-routing explanation

## Proposed Implementation Order

1. Freeze the scale target: 500,000 products, variants, stores, batches, daily events.
2. Convert the retailer problem coverage matrix into eval scenarios and candidate types.
3. Add normalized product, variant, identifier, alias, batch, and inventory-position models.
4. Add large synthetic catalog and operational state generator that can reach 500,000 products.
5. Add bulk ingestion and idempotent upserts.
6. Add typed hot columns and indexes.
7. Add partitioned event and inbound tables.
8. Add candidate generation and fleet-wide scoring jobs.
9. Add exception queue and deduplication.
10. Add ContextAssembler and context receipts.
11. Add worker batching, retries, dead-letter handling, and queue lag metrics.
12. Add scale eval harness and capacity report.
13. Add usefulness/noise evaluation scenarios.
14. Add frontend exception views with server-side pagination.
15. Add hackathon proof package artifacts.
16. Clean up stale doc contradictions.
17. Add AMD notebook/vLLM proof receipt.
18. Add harness receipts and primitive matrix.

## Definition Of Done For Scale Readiness

ShelfWise should not be called scale-ready until it can demonstrate:

- Ingest 500,000 product/variant records without manual cleanup.
- Represent product variants, aliases, pack sizes, and GTIN/barcode mappings.
- Represent batch/lot expiry per store/location.
- Process large daily sales and stock feeds incrementally.
- Score expiry and stockout risks without running an LLM per product.
- Reduce all product-state rows into a manageable exception queue.
- Produce evidence-rich recommendations only for top candidates.
- Keep high-risk actions HITL-gated.
- Recover failed worker jobs without losing events.
- Show queue lag, scoring duration, stale sources, and model cost.
- Prove the system with a repeatable synthetic 500k benchmark.
- Explain exactly which records led to a recommendation.
- Keep manager-facing recommendations small, ranked, and useful.
- Prove noisy/low-value events are suppressed, not surfaced.
- Show the Critic rejects unsupported, stale, contradictory, or low-evidence actions.
- Map Track 3 requirements to concrete repo/deck/video artifacts.
- Show AMD compute usage with a receipt, benchmark, or recorded trace.
- Map every major retailer problem to either an implemented workflow or an explicit backlog item.
- Avoid breaking the current scan -> inventory -> expiry -> demand -> opportunity -> simulation -> critic -> executive -> HITL workflow.

## Final Critique

The current ShelfWise demo is strong because it is focused, evidence-first, and safe. But it is not yet a production-scale retail decision engine.

The missing system is not more agents. The missing system is the operational substrate:

- product identity
- variant and batch modeling
- large-scale ingestion
- state materialization
- partitioned storage
- cheap candidate generation
- fleet-wide scoring
- exception prioritization
- context assembly
- worker recovery
- scale observability
- benchmark receipts
- AMD ROCm/vLLM proof receipts
- useful alert suppression

Once those are implemented, the existing agent harness becomes credible because it reviews the right few decisions out of a very large daily workload instead of pretending to reason over every product one by one.
