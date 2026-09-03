#!/usr/bin/env bash
set -Eeuo pipefail

DEFAULTS_FILE="${ARVENTO_DEFAULTS_FILE:-/etc/default/arvento-report}"
[[ -r "$DEFAULTS_FILE" ]] && . "$DEFAULTS_FILE"

ROOT="${ARVENTO_ROOT:-/opt/arvento_report}"
COMPOSE_FILE="$ROOT/docker-compose.server.yml"
REPORT_PORTAL_PORT="${REPORT_PORTAL_PORT:-18084}"
GEOFENCE_EDITOR_PORT="${GEOFENCE_EDITOR_PORT:-18083}"
MIN_FREE_DISK_PERCENT="${MIN_FREE_DISK_PERCENT:-10}"
BACKUP_DIR="${BACKUP_DIR:-/opt/arvento_backups}"
HEALTHCHECK_MAX_SYNC_AGE_MINUTES="${HEALTHCHECK_MAX_SYNC_AGE_MINUTES:-180}"
HEALTHCHECK_MAX_BACKUP_AGE_HOURS="${HEALTHCHECK_MAX_BACKUP_AGE_HOURS:-30}"
HEALTHCHECK_STALE_RUNNING_MINUTES="${HEALTHCHECK_STALE_RUNNING_MINUTES:-240}"

failures=0

require_uint() {
    local name="$1"
    local value="${!name}"
    if [[ ! "$value" =~ ^[0-9]+$ ]]; then
        echo "ERROR: $name должен быть целым неотрицательным числом"
        exit 2
    fi
}

for value_name in \
    MIN_FREE_DISK_PERCENT \
    HEALTHCHECK_MAX_SYNC_AGE_MINUTES \
    HEALTHCHECK_MAX_BACKUP_AGE_HOURS \
    HEALTHCHECK_STALE_RUNNING_MINUTES; do
    require_uint "$value_name"
done

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

section "LAST JOB RESULTS"
for service in \
    arvento-intraday-pipeline.service \
    arvento-nightly-correction.service \
    arvento-backup.service; do
    active="$(systemctl is-active "$service" 2>/dev/null || true)"
    result="$(systemctl show "$service" --property=Result --value 2>/dev/null || true)"
    exit_status="$(systemctl show "$service" --property=ExecMainStatus --value 2>/dev/null || true)"
    printf '%-40s active=%-12s result=%-10s exit=%s\n' \
        "$service" "$active" "${result:-unknown}" "${exit_status:-unknown}"
    if [[ "$active" == "active" || "$active" == "activating" ]]; then
        continue
    fi
    if [[ "$result" != "success" || "$exit_status" != "0" ]]; then
        failures=$((failures + 1))
    fi
done

section "BACKUP FRESHNESS"
latest_backup="$({
    find "$BACKUP_DIR" \
        -maxdepth 1 \
        -type f \
        -name 'arvento_report_*.dump' \
        -printf '%T@|%p\n' 2>/dev/null || true
} | sort -rn | head -n 1)"
if [[ -z "$latest_backup" ]]; then
    echo "FAILED: проверенный backup не найден в $BACKUP_DIR"
    failures=$((failures + 1))
else
    latest_backup_path="${latest_backup#*|}"
    backup_epoch="$(stat -c '%Y' "$latest_backup_path")"
    backup_age_seconds=$(( $(date +%s) - backup_epoch ))
    backup_max_age_seconds=$(( HEALTHCHECK_MAX_BACKUP_AGE_HOURS * 3600 ))
    backup_size="$(stat -c '%s' "$latest_backup_path")"
    printf 'latest=%s age_hours=%d size_bytes=%s\n' \
        "$latest_backup_path" "$(( backup_age_seconds / 3600 ))" "$backup_size"
    if (( backup_size <= 0 )); then
        echo "FAILED: последний backup пуст"
        failures=$((failures + 1))
    fi
    if (( backup_age_seconds < 0 || backup_age_seconds > backup_max_age_seconds )); then
        echo "FAILED: backup старше ${HEALTHCHECK_MAX_BACKUP_AGE_HOURS} ч"
        failures=$((failures + 1))
    fi
fi

section "DATABASE"
POSTGRES_CONTAINER="$("${COMPOSE[@]}" ps -q postgres)"
if [[ -n "$POSTGRES_CONTAINER" ]]; then
    db_scalar() {
        local sql="$1"
        printf '%s\n' "$sql" |
            docker exec -i "$POSTGRES_CONTAINER" \
                sh -lc 'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At'
    }

    docker exec "$POSTGRES_CONTAINER" \
        sh -lc 'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off -c "
            SELECT id, status, period_start, period_end, finished_at,
                   chunks_success, chunks_total, rows_received, rows_inserted
            FROM sync_runs
            ORDER BY id DESC
            LIMIT 10;
        "'

    echo
    echo "RUNNING-записи:"
    docker exec "$POSTGRES_CONTAINER" \
        sh -lc 'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off -c "
            SELECT id, period_start, period_end, finished_at, error_message
            FROM sync_runs
            WHERE status = '\''RUNNING'\''
            ORDER BY id DESC
            LIMIT 20;
        "'

    if latest_terminal_status="$(db_scalar "
        SELECT status
        FROM sync_runs
        WHERE status <> 'RUNNING'
        ORDER BY id DESC
        LIMIT 1;
    ")"; then
        printf 'Последний завершённый sync: %s\n' "${latest_terminal_status:-NOT_FOUND}"
        if [[ "$latest_terminal_status" != "SUCCESS" ]]; then
            echo "FAILED: последний завершённый sync имеет статус ${latest_terminal_status:-NOT_FOUND}"
            failures=$((failures + 1))
        fi
    else
        echo "FAILED: не удалось проверить статус последнего sync"
        failures=$((failures + 1))
    fi

    if sync_age_seconds="$(db_scalar "
        SELECT COALESCE(
            EXTRACT(EPOCH FROM (now() - max(finished_at)))::bigint,
            -1
        )
        FROM sync_runs
        WHERE status = 'SUCCESS';
    ")" && [[ "$sync_age_seconds" =~ ^-?[0-9]+$ ]]; then
        printf 'Возраст последнего успешного sync: %d мин\n' "$(( sync_age_seconds / 60 ))"
        if (( sync_age_seconds < 0 || sync_age_seconds > HEALTHCHECK_MAX_SYNC_AGE_MINUTES * 60 )); then
            echo "FAILED: успешный sync старше ${HEALTHCHECK_MAX_SYNC_AGE_MINUTES} мин"
            failures=$((failures + 1))
        fi
    else
        echo "FAILED: не удалось определить возраст последнего успешного sync"
        failures=$((failures + 1))
    fi

    if stale_running_count="$(db_scalar "
        SELECT count(*)
        FROM sync_runs
        WHERE status = 'RUNNING'
          AND started_at < now() - make_interval(mins => $HEALTHCHECK_STALE_RUNNING_MINUTES);
    ")" && [[ "$stale_running_count" =~ ^[0-9]+$ ]]; then
        printf 'Зависших RUNNING старше %s мин: %s\n' \
            "$HEALTHCHECK_STALE_RUNNING_MINUTES" "$stale_running_count"
        if (( stale_running_count > 0 )); then
            failures=$((failures + 1))
        fi
    else
        echo "FAILED: не удалось проверить зависшие RUNNING"
        failures=$((failures + 1))
    fi
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
