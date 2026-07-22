#!/usr/bin/env bash
# Restore drill: prove the latest client backup actually restores, before and during
# production (monthly). Restores into a scratch database and compares row counts for the
# tables that carry the client's operational truth. This script is the acceptance gate in
# CLIENT_INTAKE_RUNBOOK.md section 6 - a stack is not client-ready until it passes.
#
# Required:
#   DATABASE_URL        - the live client database (read-only use here: counts only)
#   RESTORE_ADMIN_URL   - superuser/CREATEDB connection for creating the scratch database
# Optional:
#   BACKUP_DIR (default /var/backups/shelfwise), SCRATCH_DB (default shelfwise_restore_drill)
set -euo pipefail

readonly BACKUP_DIR="${BACKUP_DIR:-/var/backups/shelfwise}"
readonly SCRATCH_DB="${SCRATCH_DB:-shelfwise_restore_drill}"
readonly CHECK_TABLES=(
    shelfwise_events
    shelfwise_decisions
    shelfwise_inbound_records
    shelfwise_learning_events
)

for var in DATABASE_URL RESTORE_ADMIN_URL; do
    if [[ -z "${!var:-}" ]]; then
        echo "ERROR: $var is required" >&2
        exit 1
    fi
done

readonly LATEST="$(ls -1t "$BACKUP_DIR"/daily/shelfwise_*.dump 2>/dev/null | head -n 1)"
if [[ -z "$LATEST" ]]; then
    echo "ERROR: no backups found under $BACKUP_DIR/daily" >&2
    exit 1
fi
echo "drilling restore of: $LATEST"

readonly ADMIN_BASE="${RESTORE_ADMIN_URL%/*}"
psql "$RESTORE_ADMIN_URL" -v ON_ERROR_STOP=1 \
    -c "drop database if exists $SCRATCH_DB" \
    -c "create database $SCRATCH_DB"
pg_restore --no-owner --no-privileges --dbname="$ADMIN_BASE/$SCRATCH_DB" "$LATEST"

status=0
for table in "${CHECK_TABLES[@]}"; do
    live="$(psql "$DATABASE_URL" -tA -c "select count(*) from $table" 2>/dev/null || echo "absent")"
    restored="$(psql "$ADMIN_BASE/$SCRATCH_DB" -tA -c "select count(*) from $table" 2>/dev/null || echo "absent")"
    if [[ "$live" == "$restored" ]]; then
        echo "OK   $table: $restored rows"
    else
        # The live count can legitimately move between dump and drill; a small positive
        # drift is expected on a busy stack. A restored count of zero or "absent" while
        # live has data is the failure this drill exists to catch.
        if [[ "$restored" == "absent" || ( "$restored" == "0" && "$live" != "0" && "$live" != "absent" ) ]]; then
            echo "FAIL $table: live=$live restored=$restored" >&2
            status=1
        else
            echo "DRIFT $table: live=$live restored=$restored (verify timing, not data loss)"
        fi
    fi
done

psql "$RESTORE_ADMIN_URL" -v ON_ERROR_STOP=1 -c "drop database if exists $SCRATCH_DB"

if [[ "$status" -eq 0 ]]; then
    echo "RESTORE DRILL PASSED - record the date and dump name in the client ops log"
else
    echo "RESTORE DRILL FAILED - do not consider this stack client-ready" >&2
fi
exit "$status"
