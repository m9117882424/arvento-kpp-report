#!/usr/bin/env bash
set -Eeuo pipefail

[[ -r /etc/default/arvento-report ]] && . /etc/default/arvento-report

ROOT="${ARVENTO_ROOT:-/opt/arvento_report}"
COMPOSE_FILE="$ROOT/docker-compose.server.yml"
REPORT_PORTAL_PORT="${REPORT_PORTAL_PORT:-18084}"
GEOFENCE_EDITOR_PORT="${GEOFENCE_EDITOR_PORT:-18083}"
MIN_FREE_DISK_PERCENT="${MIN_FREE_DISK_PERCENT:-10}"

failures=0

section() {
    printf '\n========== %s ==========\n' "$1"
}

check_url() {
    local name="$1"
    local url="$2"
    if curl --fail --silent --show-error --max-time 10 "$url" >/dev/null; then
        printf '%-24s OK\n' "$name"
    else
        printf '%-24s FAILED (%s)\n' "$name" "$url"
        failures=$((failures + 1))
    fi
}

[[ -d "$ROOT" ]] || {
    echo "ERROR: $ROOT не найден"
    exit 1
}
[[ -f "$COMPOSE_FILE" ]] || {
    echo "ERROR: $COMPOSE_FILE не найден"
    exit 1
}

COMPOSE=(
    docker compose
    --project-directory "$ROOT"
    -f "$COMPOSE_FILE"
)

section "DOCKER COMPOSE"
"${COMPOSE[@]}" ps

for service in postgres geofence-editor report-portal; do
    container="$("${COMPOSE[@]}" ps -q "$service")"
    if [[ -z "$container" ]]; then
        printf '%-24s NOT RUNNING\n' "$service"
        failures=$((failures + 1))
        continue
    fi
    state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container")"
    printf '%-24s %s\n' "$service" "$state"
    if [[ "$state" != "healthy" && "$state" != "running" ]]; then
        failures=$((failures + 1))
    fi
done

section "HTTP"
check_url "geofence-editor" "http://127.0.0.1:${GEOFENCE_EDITOR_PORT}/health"
check_url "report-portal" "http://127.0.0.1:${REPORT_PORTAL_PORT}/health"

section "SYSTEMD TIMERS"
for timer in \
    arvento-intraday-pipeline.timer \
    arvento-nightly-correction.timer \
    arvento-backup.timer; do
    state="$(systemctl is-active "$timer" 2>/dev/null || true)"
    enabled="$(systemctl is-enabled "$timer" 2>/dev/null || true)"
    printf '%-40s active=%-10s enabled=%s\n' "$timer" "$state" "$enabled"
    if [[ "$state" != "active" || "$enabled" != "enabled" ]]; then
        failures=$((failures + 1))
    fi
done

systemctl list-timers --all --no-pager | grep -E 'arvento-(intraday|nightly|backup)' || true

section "DATABASE"
POSTGRES_CONTAINER="$("${COMPOSE[@]}" ps -q postgres)"
if [[ -n "$POSTGRES_CONTAINER" ]]; then
    docker exec "$POSTGRES_CONTAINER" \
        sh -lc 'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off -c "
            SELECT id, status, period_start, period_end, finished_at,
                   chunks_success, chunks_total, rows_received, rows_inserted
            FROM sync_runs
            ORDER BY id DESC
            LIMIT 10;
        "'

    echo
    echo "RUNNING-записи (проверить, если нет активного процесса):"
    docker exec "$POSTGRES_CONTAINER" \
        sh -lc 'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off -c "
            SELECT id, period_start, period_end, finished_at, error_message
            FROM sync_runs
            WHERE status = '\''RUNNING'\''
            ORDER BY id DESC
            LIMIT 20;
        "'
fi

section "RESOURCE SUMMARY"
free -h || true
df -h "$ROOT"

disk_used="$(df -P "$ROOT" | awk 'NR==2 {gsub("%", "", $5); print $5}')"
if [[ "$disk_used" =~ ^[0-9]+$ ]]; then
    free_percent=$((100 - disk_used))
    if (( free_percent < MIN_FREE_DISK_PERCENT )); then
        echo "FAILED: свободно менее ${MIN_FREE_DISK_PERCENT}% диска"
        failures=$((failures + 1))
    fi
fi

section "RESULT"
if (( failures > 0 )); then
    echo "FAILED: critical_checks=$failures"
    exit 1
fi

echo "OK: core services, HTTP endpoints, timers and disk are healthy"
