#!/usr/bin/env bash
# Nightly client-database backup for a dedicated ShelfWise stack.
# Custom-format pg_dump with daily/weekly rotation; secrets (.env) are NEVER included -
# they live in the password manager. Pair with client_restore_verify.sh: a backup that
# has not passed a restore drill does not count (CLIENT_INTAKE_RUNBOOK.md section 6).
#
# Required: DATABASE_URL (the restricted shelfwise_app role is sufficient).
# Optional: BACKUP_DIR (default /var/backups/shelfwise), DAILY_KEEP=14, WEEKLY_KEEP=8.
set -euo pipefail

readonly BACKUP_DIR="${BACKUP_DIR:-/var/backups/shelfwise}"
readonly DAILY_KEEP="${DAILY_KEEP:-14}"
readonly WEEKLY_KEEP="${WEEKLY_KEEP:-8}"

if [[ -z "${DATABASE_URL:-}" ]]; then
    echo "ERROR: DATABASE_URL is required" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR/daily" "$BACKUP_DIR/weekly"

readonly STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
readonly DUMP="$BACKUP_DIR/daily/shelfwise_${STAMP}.dump"

pg_dump --format=custom --no-owner --no-privileges \
    --dbname="$DATABASE_URL" --file="$DUMP"

# An empty or unreadable dump must fail the job loudly, not rotate into retention.
pg_restore --list "$DUMP" > /dev/null
echo "backup written and list-verified: $DUMP ($(du -h "$DUMP" | cut -f1))"

# Sundays additionally copy into the weekly ring.
if [[ "$(date -u +%u)" == "7" ]]; then
    cp "$DUMP" "$BACKUP_DIR/weekly/"
fi

# Rotate: keep the newest N in each ring.
prune() {
    local dir="$1" keep="$2"
    ls -1t "$dir"/shelfwise_*.dump 2>/dev/null | tail -n +$((keep + 1)) | while read -r old; do
        rm -f "$old"
        echo "pruned: $old"
    done
}
prune "$BACKUP_DIR/daily" "$DAILY_KEEP"
prune "$BACKUP_DIR/weekly" "$WEEKLY_KEEP"
