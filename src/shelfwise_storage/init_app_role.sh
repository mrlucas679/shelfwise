#!/bin/sh
set -eu

# Provisions the least-privilege role the application connects as at runtime.
#
# Postgres does not enforce row-level security - not even FORCE ROW LEVEL SECURITY -
# for superusers or BYPASSRLS roles. The compose stack's POSTGRES_USER is a superuser
# (standard for the official postgres/pgvector images), so every tenant-isolation
# policy `apply_tenant_rls` installs (see schema.sql) would be silently inert if the
# app ever connected as it. This script creates a separate, restricted role and grants
# it only the table access it needs; the app's DATABASE_URL must point at this role,
# never at POSTGRES_USER. Runs after 01-schema.sql (docker-entrypoint-initdb.d executes
# *.sql/*.sh files in name order), so the tables already exist for the GRANTs below.

: "${POSTGRES_APP_PASSWORD:?POSTGRES_APP_PASSWORD must be set to provision the app role}"
: "${POSTGRES_DB:=shelfwise}"
: "${POSTGRES_USER:=shelfwise}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    do \$\$
    begin
        if not exists (select 1 from pg_roles where rolname = 'shelfwise_app') then
            create role shelfwise_app with login nosuperuser nocreatedb nocreaterole nobypassrls;
        end if;
    end
    \$\$;

    alter role shelfwise_app with password '$POSTGRES_APP_PASSWORD';

    grant connect on database $POSTGRES_DB to shelfwise_app;
    grant usage on schema public to shelfwise_app;
    grant select, insert, update, delete on all tables in schema public to shelfwise_app;
    grant usage, select on all sequences in schema public to shelfwise_app;
    alter default privileges in schema public
        grant select, insert, update, delete on tables to shelfwise_app;
    alter default privileges in schema public
        grant usage, select on sequences to shelfwise_app;
EOSQL
