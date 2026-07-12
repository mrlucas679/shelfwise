create extension if not exists vector;

create table if not exists shelfwise_decisions (
    id text primary key,
    tenant_id text not null default 'default',
    status text not null,
    payload jsonb not null,
    created_at timestamptz not null,
    updated_at timestamptz not null
);

create index if not exists idx_shelfwise_decisions_status_updated
on shelfwise_decisions (status, updated_at desc);

create index if not exists idx_shelfwise_decisions_tenant_updated
on shelfwise_decisions (tenant_id, updated_at desc);

create table if not exists shelfwise_events (
    id text primary key,
    tenant_id text not null,
    event_type text not null,
    event_ts timestamptz not null,
    payload jsonb not null,
    received_at timestamptz not null
);

create index if not exists idx_shelfwise_events_tenant_received
on shelfwise_events (tenant_id, received_at desc);

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
    event_time timestamptz not null,
    unique (tenant_id, source_system, raw_payload_hash)
);

create index if not exists idx_shelfwise_inbound_records_tenant_ingested
on shelfwise_inbound_records (tenant_id, ingested_at desc);

create index if not exists idx_shelfwise_inbound_records_tenant_source_object
on shelfwise_inbound_records (tenant_id, source_system, source_object_id);

create table if not exists shelfwise_learning_thresholds (
    tenant_id text not null default 'default',
    metric text not null,
    sku text not null,
    threshold_units integer not null,
    updated_at timestamptz not null,
    primary key (tenant_id, metric)
);

create unique index if not exists ux_shelfwise_learning_thresholds_tenant_metric
on shelfwise_learning_thresholds (tenant_id, metric);

create table if not exists shelfwise_learning_events (
    tenant_id text not null default 'default',
    decision_id text not null,
    payload jsonb not null,
    created_at timestamptz not null,
    primary key (tenant_id, decision_id)
);

create index if not exists idx_shelfwise_learning_events_tenant_created
on shelfwise_learning_events (tenant_id, created_at desc);

create table if not exists cascade_runs (
    run_id text primary key,
    tenant_id text not null,
    status text not null default 'running',
    started_at timestamptz not null,
    finished_at timestamptz
);

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
    status text not null default 'ok',
    created_at timestamptz not null
);

create index if not exists idx_shelfwise_model_runs_tenant_created
on shelfwise_model_runs (tenant_id, created_at desc);

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
    primary key (tenant_id, idempotency_key)
);

create index if not exists idx_shelfwise_writeback_tasks_tenant_created
on shelfwise_writeback_tasks (tenant_id, created_at desc);

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
    pattern_type text not null,
    sku text,
    conclusion text not null,
    evidence_refs text[] not null default '{}',
    payload jsonb not null,
    embedding vector(768),
    created_at timestamptz not null
);

create index if not exists idx_shelfwise_learned_patterns_tenant_type
on shelfwise_learned_patterns (tenant_id, pattern_type);

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

alter table shelfwise_decisions enable row level security;
alter table shelfwise_decisions force row level security;
drop policy if exists shelfwise_decisions_tenant_isolation on shelfwise_decisions;
create policy shelfwise_decisions_tenant_isolation on shelfwise_decisions
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

alter table shelfwise_inventory_positions enable row level security;
alter table shelfwise_inventory_positions force row level security;
drop policy if exists shelfwise_inventory_positions_tenant_isolation
on shelfwise_inventory_positions;
create policy shelfwise_inventory_positions_tenant_isolation on shelfwise_inventory_positions
using (tenant_id = current_setting('app.tenant_id', true))
with check (tenant_id = current_setting('app.tenant_id', true));
