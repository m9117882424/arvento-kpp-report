#!/usr/bin/env bash
# Optional post-nightly warmup. Do not overlap the main sync/cache pipeline.
set -Eeuo pipefail

[[ -r /etc/default/arvento-report ]] && . /etc/default/arvento-report

ROOT="${ARVENTO_ROOT:-/opt/arvento_report}"
COMPOSE_FILE="$ROOT/docker-compose.server.yml"
TIME_LIMIT="${EFFICIENCY_CACHE_WARMUP_TIMEOUT_SECONDS:-900}"
[[ "$TIME_LIMIT" =~ ^[0-9]+$ ]] && (( TIME_LIMIT >= 60 && TIME_LIMIT <= 3600 )) || {
    printf '%s\n' 'Invalid efficiency warmup timeout' >&2
    exit 2
}

exec 9>/run/arvento-sync-and-cache.lock
if ! flock -n 9; then
    printf '%s\n' 'SKIP: Arvento sync/cache pipeline owns lock'
    exit 0
fi

[[ -r "$COMPOSE_FILE" ]] || exit 2
timeout --signal=TERM --kill-after=30 "$TIME_LIMIT" \
    docker compose --project-directory "$ROOT" -f "$COMPOSE_FILE" \
    run --rm --no-deps report-portal \
    python efficiency_cache_worker.py --days 2
