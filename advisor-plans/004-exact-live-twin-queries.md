# Plan 004: Make live-twin observations exact and queryable

> **Executor instructions**: Execute after plan 001. Complete the provider-facing
> tests after plan 002. Preserve reported and predicted lane separation.
>
> **Drift check (run first)**: compare `TwinService._event_specs`, `live_context`,
> live tools, twin models/stores, and scenario engine with the excerpts below.

## Status

- **Priority**: P1
- **Effort**: L
- **Risk**: MED
- **Depends on**: plan 001; final integration tests depend on plan 002
- **Category**: bug, persistence, tests
- **Planned at**: commit `5e306c2`, 2026-07-13, dirty `developers` worktree

## Why This Matters

Even when chat selects the operational twin, the current projection and query APIs
can omit the exact fact the user asked for. Cold-chain temperature is dropped during
event projection, SKU filters are ignored, and truncation occurs before relevance
filtering. A model grounded on incomplete or unrelated state can still give a wrong
answer while appearing correctly wired.

## Current State

- `service._event_specs():487-520` stores diagnosis, severity, predicted unsafe
  minutes, outage hours, and stock risk, but not `temp_c`.
- `live_context():217-235` lists all reported properties, slices to `limit`, then
  returns them without entity/property relevance filters.
- `get_live_stock():480-484` records `requested_sku` but never filters by SKU.
- `get_live_cold_chain_status():486-490` defaults to only
  `cold_chain.diagnosis`.
- `ScenarioEngine` persists predicted properties but keeps branch metadata only in
  `self._branches`; compare returns 404 after process restart.

## Commands You Will Need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused tests | `python -m pytest -q tests/test_twin_models.py tests/test_twin_projector.py tests/test_twin_api.py tests/test_twin_scenarios.py tests/test_live_tool_boundary.py` | all pass |
| Full suite | `python -m pytest -q` | all pass |
| Lint | `python -m ruff check src tests scripts` | exit 0 |

## Scope

**In scope**:
- twin models, service, store, scenario engine, and schema
- live tools in `mcp_surface.py`
- route query models in `routes_twin.py`
- exact live-tool and scenario restart tests

**Out of scope**:
- Raw image/video storage; only derived observations are allowed.
- New computer-vision models.
- Changing the reported/predicted lane invariant.

## Steps

### Step 1: Normalize cold-chain observations

Map accepted input aliases `temp_c`, `temperature_c`, and `average_temp_c` to the
canonical property `cold_chain.temperature_c` with unit `celsius`. Preserve source
object, observed time, confidence, and payload hash. Keep diagnosis and risk fields.
Reject non-finite temperatures and values outside a documented sensor sanity range.

**Verify**: an ingested 25 C event creates a reported temperature property with the
same timestamp and source reference.

### Step 2: Add query-before-limit filtering

Extend the twin store/service query to filter by tenant, store, lane, twin/entity
identifier, local SKU/asset ID, exact property, and property prefix before applying
the limit. Return deterministic newest-first ordering within the requested entity.
Do not fetch 120 arbitrary rows and filter in Python afterward for Postgres.

**Verify**: create more than 120 unrelated properties plus one requested SKU; the
requested result must still be returned.

### Step 3: Make live tools honor their arguments

- `get_live_stock(sku, store_id)` must resolve the SKU to the product twin and return
  only that product's inventory properties.
- `get_live_cold_chain_status(store_id, asset_id)` must return all relevant
  `cold_chain.*` properties, including temperature and freshness.
- `get_live_twin_state` should accept explicit entity/property-prefix filters.
- All responses must include `data_domain`, `source_refs`, `missing_data`, and a
  truthful `truncated` flag.

Model arguments may narrow within the authenticated tenant but may never supply or
override tenant identity.

**Verify**: two SKUs and two fridge assets return disjoint results; cross-tenant
lookups return no data.

### Step 4: Prove reported state wins over simulation

Create the exact conflict regression:

- reported fridge temperature: 25 C;
- predicted/scenario temperature: 11 C;
- generated-world temperature: another value.

Assert live context, live tool output, and operational provider all return 25 C;
scenario compare returns 11 C in the predicted lane; simulation provider remains
separate.

**Verify**: run focused tests.

### Step 5: Persist scenario branch metadata

Add a tenant/store/branch-scoped scenario branch store (memory and Postgres). Persist
base projection hash, parameters, deltas, timestamps, and status before applying
predicted observations. Rehydrate comparison metadata after service restart. Keep
predicted properties immutable to reported state and add a bounded expiry/cleanup
policy for old scenario branches.

**Verify**: create a branch with Postgres-backed stores, create a new engine instance,
and compare successfully. Confirm reported property hashes are unchanged.

## Test Plan

- Temperature aliases normalize to one canonical property.
- Invalid temperatures fail validation.
- Filter-before-limit returns the requested entity.
- SKU and asset filters are real, not echoed metadata.
- 25 reported versus 11 predicted conflict resolves correctly per domain.
- Scenario compare survives engine restart.
- Tenant and lane isolation remain intact.

## Done Criteria

- [ ] Cold-chain events preserve measured temperature.
- [ ] Live tool arguments change the selected records correctly.
- [ ] Operational queries never include predicted/scenario values.
- [ ] Relevant facts cannot be lost by pre-filter truncation.
- [ ] Scenario metadata survives restart.
- [ ] Full tests and Ruff pass.

## STOP Conditions

- A query requires raw media rather than a derived observation.
- Any change writes predicted values into the reported lane.
- Scenario persistence cannot be added without deleting existing predicted history.

## Maintenance Notes

Use canonical property names at ingestion and indexes at query time. Adding a new
sensor property requires projection mapping, sanity validation, query coverage, and
a conflict test across reported/predicted domains.
