create extension if not exists vector;

create table if not exists shelfwise_decisions (
    id text primary key,
    tenant_id text not null default 'default',
    data_domain text not null default 'world_simulation',
    status text not null,
    payload jsonb not null,
    created_at timestamptz not null,
    updated_at timestamptz not null
);
alter table shelfwise_decisions
add column if not exists data_domain text not null default 'world_simulation';

create index if not exists idx_shelfwise_decisions_status_updated
on shelfwise_decisions (status, updated_at desc);

create index if not exists idx_shelfwise_decisions_tenant_updated
on shelfwise_decisions (tenant_id, updated_at desc);
create index if not exists idx_shelfwise_decisions_tenant_domain_updated
on shelfwise_decisions (tenant_id, data_domain, updated_at desc);

create table if not exists shelfwise_candidates (
    candidate_key text primary key,
    tenant_id text not null,
    data_domain text not null default 'world_simulation',
    candidate_type text not null,
    sku text not null,
    lot_id text,
    status text not null,
    score numeric not null,
    urgency numeric not null,
    exposure_units integer not null,
    monitoring_only boolean not null,
    evidence jsonb not null,
    first_seen_at timestamptz not null,
    last_seen_at timestamptz not null,
    updated_at timestamptz not null,
    suppression_reason text,
    suppressed_until timestamptz,
    decision_id text
);
alter table shelfwise_candidates
add column if not exists data_domain text not null default 'world_simulation';

drop index if exists idx_shelfwise_candidates_tenant_status_updated;
create index if not exists idx_shelfwise_candidates_tenant_status_updated
on shelfwise_candidates (tenant_id, data_domain, status, updated_at desc);

drop index if exists idx_shelfwise_candidates_tenant_suppression;
create index if not exists idx_shelfwise_candidates_tenant_suppression
on shelfwise_candidates (tenant_id, data_domain, suppressed_until);

create table if not exists shelfwise_candidate_history (
    tenant_id text not null,
    data_domain text not null default 'world_simulation',
    candidate_key text not null,
    sequence integer not null,
    reason text not null,
    status text not null,
    score numeric not null,
    urgency numeric not null,
    exposure_units integer not null,
    decision_id text,
    recorded_at timestamptz not null,
    primary key (tenant_id, candidate_key, sequence)
);
create index if not exists idx_shelfwise_candidate_history_tenant_key
on shelfwise_candidate_history (tenant_id, candidate_key, sequence desc);

create table if not exists shelfwise_connector_cursors (
    tenant_id text not null,
    system text not null,
    cursor text not null,
    primary key (tenant_id, system)
);

create table if not exists shelfwise_open_orders (
    tenant_id text not null,
    data_domain text not null default 'operational_twin',
    order_id text not null,
    sku text not null,
    supplier_id text not null,
    ordered_units integer not null,
    received_units integer not null,
    remaining_units integer not null,
    eta timestamptz,
    status text not null,
    source_event_id text not null,
    updated_at timestamptz not null,
    payload jsonb not null,
    primary key (tenant_id, data_domain, order_id)
);
alter table shelfwise_open_orders
add column if not exists data_domain text not null default 'operational_twin';
alter table shelfwise_open_orders drop constraint if exists shelfwise_open_orders_pkey;
alter table shelfwise_open_orders add primary key (tenant_id, data_domain, order_id);

drop index if exists idx_shelfwise_open_orders_tenant_sku_status;
create index if not exists idx_shelfwise_open_orders_tenant_sku_status
on shelfwise_open_orders (tenant_id, data_domain, sku, status, eta);

create table if not exists shelfwise_events (
    id text not null,
    tenant_id text not null,
    data_domain text not null default 'operational_twin',
    event_type text not null,
    event_ts timestamptz not null,
    payload jsonb not null,
    received_at timestamptz not null,
    published boolean not null default false,
    primary key (tenant_id, data_domain, id)
);

-- Legacy installs predate explicit data-domain provenance. Prefix checks are used
-- only for this one-time backfill; request-time policy uses Event.data_domain.
alter table shelfwise_events add column if not exists data_domain text;
alter table shelfwise_events add column if not exists published boolean not null default false;

update shelfwise_events
set data_domain = case
    when payload->>'data_domain' in
         ('operational_twin', 'world_simulation', 'training_fixture', 'twin_scenario')
        then payload->>'data_domain'
    when id like 'evt_demo_%'
         or coalesce(payload->>'correlation_id', '') like 'world_%'
        then 'world_simulation'
    else 'operational_twin'
end
where data_domain is null;
alter table shelfwise_events
    alter column data_domain set default 'operational_twin',
    alter column data_domain set not null;
alter table shelfwise_events drop constraint if exists shelfwise_events_pkey;
alter table shelfwise_events add primary key (tenant_id, data_domain, id);

drop index if exists idx_shelfwise_events_tenant_received;
create index if not exists idx_shelfwise_events_tenant_received
on shelfwise_events (tenant_id, data_domain, received_at desc);

create table if not exists shelfwise_inbound_records (
    id text primary key,
    tenant_id text not null,
    source_system text not null,
    source_object_type text not null,
    source_object_id text not null,
    raw_payload_hash text not null,
    event_id text,
    payload jsonb not null,
    ingested_at timestamptz not null,
    event_time timestamptz not null
);

-- Dedupe key must match PostgresInboundRecordStore's ON CONFLICT column list exactly.
-- The pre-migration shape keyed only on (tenant_id, source_system, raw_payload_hash):
-- every line/count derived from one raw webhook payload shares that hash, so it silently
-- kept only the first. Widen it to include source_object_id so distinct lines/counts from
-- a single payload can all persist, while a resent payload (same object ids) still dedups.
-- Mirrors the identical migration in inbound_store._ensure_schema, which only runs when
-- SHELFWISE_AUTO_SCHEMA is enabled - production deployments migrate through this file.
do $$
declare
    old_constraint text;
begin
    select con.conname into old_constraint
    from pg_constraint con
    join pg_class rel on rel.oid = con.conrelid
    where rel.relname = 'shelfwise_inbound_records'
      and con.contype = 'u'
      and pg_get_constraintdef(con.oid)
          = 'UNIQUE (tenant_id, source_system, raw_payload_hash)';

    if old_constraint is not null then
        execute format(
            'alter table shelfwise_inbound_records drop constraint %I',
            old_constraint
        );
    end if;

    if not exists (
        select 1 from pg_constraint
        where conname = 'shelfwise_inbound_records_dedup_key'
    ) then
        alter table shelfwise_inbound_records
        add constraint shelfwise_inbound_records_dedup_key
        unique (tenant_id, source_system, raw_payload_hash, source_object_id);
    end if;
end $$;

create index if not exists idx_shelfwise_inbound_records_tenant_ingested
on shelfwise_inbound_records (tenant_id, ingested_at desc);

create index if not exists idx_shelfwise_inbound_records_tenant_source_object
on shelfwise_inbound_records (tenant_id, source_system, source_object_id);

create table if not exists shelfwise_learning_thresholds (
    tenant_id text not null default 'default',
    data_domain text not null default 'world_simulation',
    metric text not null,
    sku text not null,
    threshold_units integer not null,
    updated_at timestamptz not null,
    primary key (tenant_id, data_domain, metric)
);
alter table shelfwise_learning_thresholds
add column if not exists data_domain text not null default 'world_simulation';
alter table shelfwise_learning_thresholds
drop constraint if exists shelfwise_learning_thresholds_pkey;
alter table shelfwise_learning_thresholds
add primary key (tenant_id, data_domain, metric);

drop index if exists ux_shelfwise_learning_thresholds_tenant_metric;
create unique index if not exists ux_shelfwise_learning_thresholds_domain_metric
on shelfwise_learning_thresholds (tenant_id, data_domain, metric);

create table if not exists shelfwise_learning_events (
    tenant_id text not null default 'default',
    data_domain text not null default 'world_simulation',
    decision_id text not null,
    payload jsonb not null,
    created_at timestamptz not null,
    primary key (tenant_id, data_domain, decision_id)
);
alter table shelfwise_learning_events
add column if not exists data_domain text not null default 'world_simulation';
alter table shelfwise_learning_events
drop constraint if exists shelfwise_learning_events_pkey;
alter table shelfwise_learning_events
add primary key (tenant_id, data_domain, decision_id);

drop index if exists idx_shelfwise_learning_events_tenant_created;
create index if not exists idx_shelfwise_learning_events_tenant_created
on shelfwise_learning_events (tenant_id, data_domain, created_at desc);

create table if not exists cascade_runs (
    run_id text primary key,
    tenant_id text not null,
    data_domain text not null default 'operational_twin',
    status text not null default 'running',
    started_at timestamptz not null,
    finished_at timestamptz
);

alter table cascade_runs
add column if not exists data_domain text not null default 'operational_twin';

create index if not exists idx_cascade_runs_tenant_domain_started
on cascade_runs (tenant_id, data_domain, started_at desc);

create table if not exists cascade_steps (
    run_id text not null references cascade_runs(run_id) on delete cascade,
    tenant_id text not null default 'default',
    step_key text not null,
    output jsonb not null,
    compensation jsonb,
    recorded_at timestamptz not null,
    primary key (run_id, step_key)
);

create index if not exists idx_cascade_steps_tenant_run
on cascade_steps (tenant_id, run_id);

create table if not exists shelfwise_model_runs (
    id text primary key,
    tenant_id text not null,
    correlation_id text not null,
    agent text not null,
    model text not null,
    provider text not null,
    prompt_version text not null,
    schema_version text not null,
    input_tokens integer not null,
    output_tokens integer not null,
    latency_ms integer not null,
    data_domain text not null default 'world_simulation',
    status text not null default 'ok',
    created_at timestamptz not null,
    user_message text not null default '',
    response_text text not null default '',
    error_detail text not null default ''
);

alter table shelfwise_model_runs
add column if not exists data_domain text not null default 'world_simulation';
alter table shelfwise_model_runs
add column if not exists user_message text not null default '';
alter table shelfwise_model_runs
add column if not exists response_text text not null default '';
alter table shelfwise_model_runs
add column if not exists error_detail text not null default '';

create index if not exists idx_shelfwise_model_runs_tenant_created
on shelfwise_model_runs (tenant_id, created_at desc);

create index if not exists idx_shelfwise_model_runs_tenant_domain_created
on shelfwise_model_runs (tenant_id, data_domain, created_at desc);

create table if not exists shelfwise_prompt_versions (
    tenant_id text not null,
    id text not null,
    agent text not null,
    version text not null,
    sha text not null,
    system_prompt text not null,
    schema_version text not null default 'v1',
    created_at timestamptz not null,
    primary key (tenant_id, id)
);

create index if not exists idx_shelfwise_prompt_versions_tenant_agent
on shelfwise_prompt_versions (tenant_id, agent, version);

create table if not exists shelfwise_writeback_tasks (
    tenant_id text not null,
    data_domain text not null default 'operational_twin',
    idempotency_key text not null,
    task_id text not null unique,
    title text not null,
    assignee_role text not null,
    action jsonb not null,
    status text not null,
    rollback_instructions jsonb not null default '{}',
    payload jsonb not null,
    created_at timestamptz not null,
    updated_at timestamptz not null,
    primary key (tenant_id, data_domain, idempotency_key)
);

alter table shelfwise_writeback_tasks
add column if not exists data_domain text not null default 'operational_twin';
alter table shelfwise_writeback_tasks
drop constraint if exists shelfwise_writeback_tasks_pkey;
alter table shelfwise_writeback_tasks
add primary key (tenant_id, data_domain, idempotency_key);
drop index if exists idx_shelfwise_writeback_tasks_tenant_created;
create index if not exists idx_shelfwise_writeback_tasks_tenant_created
on shelfwise_writeback_tasks (tenant_id, data_domain, created_at desc);

create table if not exists shelfwise_worldgen_runs (
    run_id text primary key,
    tenant_id text not null,
    scenario_id text not null,
    seed integer not null,
    status text not null,
    payload jsonb not null,
    created_at timestamptz not null,
    updated_at timestamptz not null
);

create index if not exists idx_shelfwise_worldgen_runs_tenant_created
on shelfwise_worldgen_runs (tenant_id, created_at desc);

create table if not exists shelfwise_product_state (
    tenant_id text not null,
    sku text not null,
    location_id text not null,
    payload jsonb not null,
    embedding vector(768),
    updated_at timestamptz not null,
    primary key (tenant_id, sku, location_id)
);

create index if not exists idx_shelfwise_product_state_tenant_sku
on shelfwise_product_state (tenant_id, sku);

create table if not exists shelfwise_learned_patterns (
    id text primary key,
    tenant_id text not null,
    data_domain text not null default 'world_simulation',
    pattern_type text not null,
    sku text,
    conclusion text not null,
    evidence_refs text[] not null default '{}',
    payload jsonb not null,
    embedding vector(768),
    created_at timestamptz not null
);

alter table shelfwise_learned_patterns
add column if not exists data_domain text not null default 'world_simulation';
drop index if exists idx_shelfwise_learned_patterns_tenant_type;
create index if not exists idx_shelfwise_learned_patterns_tenant_type
on shelfwise_learned_patterns (tenant_id, data_domain, pattern_type);

create table if not exists shelfwise_business_profile (
    tenant_id text primary key,
    payload jsonb not null,
    embedding vector(768),
    updated_at timestamptz not null
);

create table if not exists shelfwise_products (
    tenant_id text not null,
    product_id text not null,
    payload jsonb not null,
    primary key (tenant_id, product_id)
);

create table if not exists shelfwise_product_variants (
    tenant_id text not null,
    variant_id text not null,
    product_id text not null,
    payload jsonb not null,
    primary key (tenant_id, variant_id)
);

create index if not exists idx_shelfwise_product_variants_product
on shelfwise_product_variants (tenant_id, product_id);

create table if not exists shelfwise_product_identifiers (
    tenant_id text not null,
    kind text not null,
    value text not null,
    variant_id text not null,
    source_system text,
    primary key (tenant_id, kind, value)
);

create index if not exists idx_shelfwise_product_identifiers_variant
on shelfwise_product_identifiers (tenant_id, variant_id);

create table if not exists shelfwise_chat_conversations (
    tenant_id text not null,
    user_id text not null,
    conversation_id text not null,
    payload jsonb not null,
    created_at timestamptz not null,
    updated_at timestamptz not null,
    primary key (tenant_id, user_id, conversation_id)
);

create index if not exists idx_shelfwise_chat_conversations_user_updated
on shelfwise_chat_conversations (tenant_id, user_id, updated_at desc);

-- Hierarchical conversation memory (plan Section 41.5, additive): rolling episode
-- summaries and provenance-tracked memory items layered over the JSON conversation
-- store, so long conversations keep their earlier context instead of silently losing
-- everything past the recent-turns window.
create table if not exists shelfwise_chat_memory_items (
    tenant_id text not null,
    user_id text not null,
    conversation_id text not null,
    memory_id text not null,
    kind text not null,
    status text not null,
    summary_version text not null,
    payload jsonb not null,
    created_at timestamptz not null,
    primary key (tenant_id, user_id, conversation_id, memory_id)
);

create index if not exists idx_shelfwise_chat_memory_active_summary
on shelfwise_chat_memory_items (tenant_id, user_id, conversation_id, kind, status);

-- Assistant skill catalogue (plan Section 41.4/41.5): versioned, validated manifests of
-- what the assistant may discover per conversation turn, with a promotion lifecycle -
-- discovery only ever surfaces promoted manifests.
create table if not exists shelfwise_skill_manifests (
    tenant_id text not null,
    skill_id text not null,
    version text not null,
    domain_owner text not null,
    status text not null,
    manifest jsonb not null,
    created_at timestamptz not null,
    primary key (tenant_id, skill_id)
);

create table if not exists shelfwise_inventory_positions (
    tenant_id text not null,
    sku text not null,
    location_type text not null,
    location_id text not null,
    bin_id text not null default 'unassigned',
    quantity integer not null check (quantity >= 0),
    state text not null,
    source_reference text not null,
    payload jsonb not null,
    updated_at timestamptz not null,
    primary key (tenant_id, sku, location_type, location_id, bin_id)
);

create index if not exists idx_shelfwise_inventory_positions_tenant_sku
on shelfwise_inventory_positions (tenant_id, sku, location_type);

create table if not exists shelfwise_world_snapshot (
    tenant_id text primary key,
    seed integer not null,
    policy text not null,
    generated_at timestamptz not null,
    payload jsonb not null
);

-- Additive exact-store digital-twin state. Raw media is intentionally not represented here;
-- edge devices submit only derived, provenance-bearing observations.
create table if not exists shelfwise_twin_entities (
    tenant_id text not null,
    twin_id text not null,
    store_id text not null,
    entity_type text not null,
    model_version text not null,
    display_name text not null,
    attributes jsonb not null default '{}',
    created_at timestamptz not null,
    retired_at timestamptz,
    primary key (tenant_id, twin_id)
);

create table if not exists shelfwise_twin_relationships (
    tenant_id text not null,
    relationship_id text not null,
    source_twin_id text not null,
    relationship_type text not null,
    target_twin_id text not null,
    attributes jsonb not null default '{}',
    valid_from timestamptz not null,
    valid_to timestamptz,
    primary key (tenant_id, relationship_id),
    foreign key (tenant_id, source_twin_id)
        references shelfwise_twin_entities (tenant_id, twin_id),
    foreign key (tenant_id, target_twin_id)
        references shelfwise_twin_entities (tenant_id, twin_id)
);

create table if not exists shelfwise_twin_observations (
    tenant_id text not null,
    observation_id text not null,
    store_id text not null,
    twin_id text not null,
    property_name text not null,
    lane text not null check (lane in ('reported', 'estimated', 'desired', 'predicted')),
    value jsonb not null,
    unit text,
    observed_at timestamptz not null,
    ingested_at timestamptz not null,
    source_system text not null,
    source_object_id text not null,
    source_sequence text,
    source_quality double precision not null check (source_quality between 0 and 1),
    schema_version text not null,
    correlation_id text not null,
    causation_id text,
    scenario_branch_id text,
    payload_hash text not null,
    primary key (tenant_id, observation_id),
    foreign key (tenant_id, twin_id)
        references shelfwise_twin_entities (tenant_id, twin_id),
    check (lane <> 'predicted' or scenario_branch_id is not null)
);

create unique index if not exists ux_shelfwise_twin_observation_source
on shelfwise_twin_observations
    (tenant_id, source_system, source_object_id, property_name, lane, payload_hash);

create index if not exists idx_shelfwise_twin_observations_entity_time
on shelfwise_twin_observations (tenant_id, twin_id, observed_at desc);

create table if not exists shelfwise_twin_property_state (
    tenant_id text not null,
    twin_id text not null,
    property_name text not null,
    lane text not null check (lane in ('reported', 'estimated', 'desired', 'predicted')),
    scenario_branch_key text not null default '',
    value jsonb not null,
    unit text,
    observation_id text not null,
    observed_at timestamptz not null,
    projected_at timestamptz not null,
    source_system text not null,
    source_quality double precision not null check (source_quality between 0 and 1),
    confidence double precision not null check (confidence between 0 and 1),
    freshness text not null,
    primary key (tenant_id, twin_id, property_name, lane, scenario_branch_key)
);

create table if not exists shelfwise_twin_calibrations (
    tenant_id text not null,
    store_id text not null,
    device_id text not null,
    property_name text not null,
    reference_value double precision not null,
    observed_value double precision not null,
    tolerance double precision not null check (tolerance > 0),
    calibrated_at timestamptz not null,
    calibration_id text not null,
    primary key (tenant_id, store_id, device_id, property_name)
);

create table if not exists shelfwise_twin_onboarding_manifests (
    tenant_id text not null,
    store_id text not null,
    manifest jsonb not null,
    primary key (tenant_id, store_id)
);

create table if not exists shelfwise_twin_scenario_branches (
    tenant_id text not null,
    store_id text not null,
    branch_id text not null,
    payload jsonb not null,
    created_at timestamptz not null,
    updated_at timestamptz not null,
    primary key (tenant_id, store_id, branch_id)
);

create index if not exists idx_shelfwise_twin_scenarios_tenant_store
on shelfwise_twin_scenario_branches (tenant_id, store_id, updated_at desc);

alter table shelfwise_decisions enable row level security;
alter table shelfwise_decisions force row level security;
drop policy if exists shelfwise_decisions_tenant_isolation on shelfwise_decisions;
create policy shelfwise_decisions_tenant_isolation on shelfwise_decisions
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table shelfwise_candidates enable row level security;
alter table shelfwise_candidates force row level security;
drop policy if exists shelfwise_candidates_tenant_isolation on shelfwise_candidates;
create policy shelfwise_candidates_tenant_isolation on shelfwise_candidates
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table shelfwise_candidate_history enable row level security;
alter table shelfwise_candidate_history force row level security;
drop policy if exists shelfwise_candidate_history_tenant_isolation on shelfwise_candidate_history;
create policy shelfwise_candidate_history_tenant_isolation on shelfwise_candidate_history
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table shelfwise_connector_cursors enable row level security;
alter table shelfwise_connector_cursors force row level security;
drop policy if exists shelfwise_connector_cursors_tenant_isolation on shelfwise_connector_cursors;
create policy shelfwise_connector_cursors_tenant_isolation on shelfwise_connector_cursors
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table shelfwise_open_orders enable row level security;
alter table shelfwise_open_orders force row level security;
drop policy if exists shelfwise_open_orders_tenant_isolation on shelfwise_open_orders;
create policy shelfwise_open_orders_tenant_isolation on shelfwise_open_orders
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table shelfwise_events enable row level security;
alter table shelfwise_events force row level security;
drop policy if exists shelfwise_events_tenant_isolation on shelfwise_events;
create policy shelfwise_events_tenant_isolation on shelfwise_events
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table shelfwise_inbound_records enable row level security;
alter table shelfwise_inbound_records force row level security;
drop policy if exists shelfwise_inbound_records_tenant_isolation
on shelfwise_inbound_records;
create policy shelfwise_inbound_records_tenant_isolation on shelfwise_inbound_records
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table shelfwise_learning_thresholds enable row level security;
alter table shelfwise_learning_thresholds force row level security;
drop policy if exists shelfwise_learning_thresholds_tenant_isolation
on shelfwise_learning_thresholds;
create policy shelfwise_learning_thresholds_tenant_isolation on shelfwise_learning_thresholds
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table shelfwise_learning_events enable row level security;
alter table shelfwise_learning_events force row level security;
drop policy if exists shelfwise_learning_events_tenant_isolation
on shelfwise_learning_events;
create policy shelfwise_learning_events_tenant_isolation on shelfwise_learning_events
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table cascade_runs enable row level security;
alter table cascade_runs force row level security;
drop policy if exists cascade_runs_tenant_isolation on cascade_runs;
create policy cascade_runs_tenant_isolation on cascade_runs
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table cascade_steps enable row level security;
alter table cascade_steps force row level security;
drop policy if exists cascade_steps_tenant_isolation on cascade_steps;
create policy cascade_steps_tenant_isolation on cascade_steps
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table shelfwise_model_runs enable row level security;
alter table shelfwise_model_runs force row level security;
drop policy if exists shelfwise_model_runs_tenant_isolation on shelfwise_model_runs;
create policy shelfwise_model_runs_tenant_isolation on shelfwise_model_runs
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table shelfwise_prompt_versions enable row level security;
alter table shelfwise_prompt_versions force row level security;
drop policy if exists shelfwise_prompt_versions_tenant_isolation
on shelfwise_prompt_versions;
create policy shelfwise_prompt_versions_tenant_isolation on shelfwise_prompt_versions
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table shelfwise_writeback_tasks enable row level security;
alter table shelfwise_writeback_tasks force row level security;
drop policy if exists shelfwise_writeback_tasks_tenant_isolation
on shelfwise_writeback_tasks;
create policy shelfwise_writeback_tasks_tenant_isolation on shelfwise_writeback_tasks
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table shelfwise_worldgen_runs enable row level security;
alter table shelfwise_worldgen_runs force row level security;
drop policy if exists shelfwise_worldgen_runs_tenant_isolation
on shelfwise_worldgen_runs;
create policy shelfwise_worldgen_runs_tenant_isolation on shelfwise_worldgen_runs
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table shelfwise_product_state enable row level security;
alter table shelfwise_product_state force row level security;
drop policy if exists shelfwise_product_state_tenant_isolation
on shelfwise_product_state;
create policy shelfwise_product_state_tenant_isolation on shelfwise_product_state
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table shelfwise_learned_patterns enable row level security;
alter table shelfwise_learned_patterns force row level security;
drop policy if exists shelfwise_learned_patterns_tenant_isolation
on shelfwise_learned_patterns;
create policy shelfwise_learned_patterns_tenant_isolation on shelfwise_learned_patterns
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table shelfwise_business_profile enable row level security;
alter table shelfwise_business_profile force row level security;
drop policy if exists shelfwise_business_profile_tenant_isolation
on shelfwise_business_profile;
create policy shelfwise_business_profile_tenant_isolation on shelfwise_business_profile
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table shelfwise_products enable row level security;
alter table shelfwise_products force row level security;
drop policy if exists shelfwise_products_tenant_isolation on shelfwise_products;
create policy shelfwise_products_tenant_isolation on shelfwise_products
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table shelfwise_product_variants enable row level security;
alter table shelfwise_product_variants force row level security;
drop policy if exists shelfwise_product_variants_tenant_isolation
on shelfwise_product_variants;
create policy shelfwise_product_variants_tenant_isolation on shelfwise_product_variants
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table shelfwise_product_identifiers enable row level security;
alter table shelfwise_product_identifiers force row level security;
drop policy if exists shelfwise_product_identifiers_tenant_isolation
on shelfwise_product_identifiers;
create policy shelfwise_product_identifiers_tenant_isolation on shelfwise_product_identifiers
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table shelfwise_chat_conversations enable row level security;
alter table shelfwise_chat_conversations force row level security;
drop policy if exists shelfwise_chat_conversations_tenant_isolation
on shelfwise_chat_conversations;
create policy shelfwise_chat_conversations_tenant_isolation on shelfwise_chat_conversations
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table shelfwise_chat_memory_items enable row level security;
alter table shelfwise_chat_memory_items force row level security;
drop policy if exists shelfwise_chat_memory_items_tenant_isolation
on shelfwise_chat_memory_items;
create policy shelfwise_chat_memory_items_tenant_isolation on shelfwise_chat_memory_items
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table shelfwise_skill_manifests enable row level security;
alter table shelfwise_skill_manifests force row level security;
drop policy if exists shelfwise_skill_manifests_tenant_isolation
on shelfwise_skill_manifests;
create policy shelfwise_skill_manifests_tenant_isolation on shelfwise_skill_manifests
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table shelfwise_inventory_positions enable row level security;
alter table shelfwise_inventory_positions force row level security;
drop policy if exists shelfwise_inventory_positions_tenant_isolation
on shelfwise_inventory_positions;
create policy shelfwise_inventory_positions_tenant_isolation on shelfwise_inventory_positions
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table shelfwise_world_snapshot enable row level security;
alter table shelfwise_world_snapshot force row level security;
drop policy if exists shelfwise_world_snapshot_tenant_isolation
on shelfwise_world_snapshot;
create policy shelfwise_world_snapshot_tenant_isolation on shelfwise_world_snapshot
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table shelfwise_twin_entities enable row level security;
alter table shelfwise_twin_entities force row level security;
drop policy if exists shelfwise_twin_entities_tenant_isolation on shelfwise_twin_entities;
create policy shelfwise_twin_entities_tenant_isolation on shelfwise_twin_entities
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table shelfwise_twin_relationships enable row level security;
alter table shelfwise_twin_relationships force row level security;
drop policy if exists shelfwise_twin_relationships_tenant_isolation on shelfwise_twin_relationships;
create policy shelfwise_twin_relationships_tenant_isolation on shelfwise_twin_relationships
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table shelfwise_twin_observations enable row level security;
alter table shelfwise_twin_observations force row level security;
drop policy if exists shelfwise_twin_observations_tenant_isolation on shelfwise_twin_observations;
create policy shelfwise_twin_observations_tenant_isolation on shelfwise_twin_observations
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table shelfwise_twin_property_state enable row level security;
alter table shelfwise_twin_property_state force row level security;
drop policy if exists shelfwise_twin_property_state_tenant_isolation on shelfwise_twin_property_state;
create policy shelfwise_twin_property_state_tenant_isolation on shelfwise_twin_property_state
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table shelfwise_twin_calibrations enable row level security;
alter table shelfwise_twin_calibrations force row level security;
drop policy if exists shelfwise_twin_calibrations_tenant_isolation on shelfwise_twin_calibrations;
create policy shelfwise_twin_calibrations_tenant_isolation on shelfwise_twin_calibrations
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table shelfwise_twin_onboarding_manifests enable row level security;
alter table shelfwise_twin_onboarding_manifests force row level security;
drop policy if exists shelfwise_twin_onboarding_manifests_tenant_isolation
    on shelfwise_twin_onboarding_manifests;
create policy shelfwise_twin_onboarding_manifests_tenant_isolation
    on shelfwise_twin_onboarding_manifests
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

alter table shelfwise_twin_scenario_branches enable row level security;
alter table shelfwise_twin_scenario_branches force row level security;
drop policy if exists shelfwise_twin_scenario_branches_tenant_isolation
on shelfwise_twin_scenario_branches;
create policy shelfwise_twin_scenario_branches_tenant_isolation
on shelfwise_twin_scenario_branches
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));

-- BRIN indexes for append-only time-series ("Things that needs to be implemented" item 7):
-- physically-correlated timestamps make BRIN the right shape - block-range summaries stay
-- tiny at millions of rows where a btree would not, and time-window scans (retention,
-- soak-report queries, replay bounds) prune whole ranges. Placed at end of file so every
-- indexed table already exists.
create index if not exists brin_shelfwise_events_received_at
on shelfwise_events using brin (received_at);
create index if not exists brin_shelfwise_inbound_records_ingested_at
on shelfwise_inbound_records using brin (ingested_at);
create index if not exists brin_cascade_runs_started_at
on cascade_runs using brin (started_at);
