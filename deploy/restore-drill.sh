#!/usr/bin/env bash
set -Eeuo pipefail

DEFAULTS_FILE="${ARVENTO_DEFAULTS_FILE:-/etc/default/arvento-report}"
[[ -r "$DEFAULTS_FILE" ]] && . "$DEFAULTS_FILE"

ROOT="${ARVENTO_ROOT:-/opt/arvento_report}"
BACKUP_DIR="${BACKUP_DIR:-/opt/arvento_backups}"
LOCK_WAIT="${RESTORE_DRILL_LOCK_WAIT_SECONDS:-600}"
RESTORE_TIMEOUT="${RESTORE_DRILL_TIMEOUT_SECONDS:-5400}"
COMPOSE_FILE="$ROOT/docker-compose.server.yml"
PIPELINE_LOCK_FILE="/run/arvento-sync-and-cache.lock"
DRILL_LOCK_FILE="/run/arvento-restore-drill.lock"
BACKUP_PATH="${1:-}"

log() {
    printf '%s | %s\n' "$(date '+%F %T %z')" "$*"
}

fail() {
    log "RESTORE_DRILL FAILED: $*"
    exit 1
}

for value_name in LOCK_WAIT RESTORE_TIMEOUT; do
    value="${!value_name}"
    [[ "$value" =~ ^[0-9]+$ ]] || fail "$value_name должен быть целым числом"
done

[[ -f "$COMPOSE_FILE" ]] || fail "$COMPOSE_FILE не найден"

if [[ -z "$BACKUP_PATH" ]]; then
    BACKUP_PATH="$({
        find "$BACKUP_DIR" -maxdepth 1 -type f \
            -name 'arvento_report_*.dump' -printf '%T@|%p\n' 2>/dev/null || true
    } | sort -rn | head -n 1)"
    BACKUP_PATH="${BACKUP_PATH#*|}"
fi

[[ -n "$BACKUP_PATH" && -f "$BACKUP_PATH" && -s "$BACKUP_PATH" ]] || \
    fail "не найден непустой backup: ${BACKUP_PATH:-не указан}"

COMPOSE=(
    docker compose
    --project-directory "$ROOT"
    -f "$COMPOSE_FILE"
)
"${COMPOSE[@]}" config --quiet

POSTGRES_CONTAINER="$("${COMPOSE[@]}" ps -q postgres)"
[[ -n "$POSTGRES_CONTAINER" ]] || fail "контейнер PostgreSQL не запущен"

POSTGRES_HEALTH="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$POSTGRES_CONTAINER")"
[[ "$POSTGRES_HEALTH" == "healthy" ]] || fail "PostgreSQL не healthy: $POSTGRES_HEALTH"

exec 8>"$DRILL_LOCK_FILE"
flock -n 8 || fail "другой restore drill уже выполняется"

exec 9>"$PIPELINE_LOCK_FILE"
log "LOCK: ожидание общей цепочки до ${LOCK_WAIT}s"
flock -w "$LOCK_WAIT" 9 || fail "общая цепочка не освободила lock"

log "VERIFY: $BACKUP_PATH"
timeout --signal=TERM --kill-after=30 300 \
    docker exec -i "$POSTGRES_CONTAINER" pg_restore --list \
    < "$BACKUP_PATH" >/dev/null || fail "архив не прошёл pg_restore --list"

DRILL_DB="arvento_restore_drill_$(date '+%Y%m%d_%H%M%S')_$$"
[[ "$DRILL_DB" =~ ^arvento_restore_drill_[0-9_]+$ ]] || fail "некорректное имя тестовой БД"

cleanup() {
    docker exec "$POSTGRES_CONTAINER" sh -lc \
        'dropdb -U "$POSTGRES_USER" --if-exists --force "$1"' \
        sh "$DRILL_DB" >/dev/null 2>&1 || true
}
trap cleanup EXIT

log "CREATE: временная БД $DRILL_DB"
docker exec "$POSTGRES_CONTAINER" sh -lc \
    'createdb -U "$POSTGRES_USER" -T template0 "$1"' \
    sh "$DRILL_DB"

log "RESTORE: timeout=${RESTORE_TIMEOUT}s"
timeout --signal=TERM --kill-after=60 "$RESTORE_TIMEOUT" \
    docker exec -i "$POSTGRES_CONTAINER" sh -lc \
        'pg_restore -U "$POSTGRES_USER" -d "$1" --no-owner --no-privileges --exit-on-error' \
        sh "$DRILL_DB" < "$BACKUP_PATH"

read -r TABLE_COUNT SYNC_RUN_COUNT <<< "$(
    docker exec "$POSTGRES_CONTAINER" sh -lc \
        'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1" -At -F " " -c "
            SELECT
                (SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname = '\''public'\''),
                (SELECT count(*) FROM public.sync_runs);
        "' sh "$DRILL_DB"
)"

[[ "$TABLE_COUNT" =~ ^[0-9]+$ && "$TABLE_COUNT" -gt 0 ]] || \
    fail "в восстановленной БД нет таблиц public"
[[ "$SYNC_RUN_COUNT" =~ ^[0-9]+$ && "$SYNC_RUN_COUNT" -gt 0 ]] || \
    fail "в восстановленной БД нет истории sync_runs"

log "RESTORE_DRILL OK: backup=$BACKUP_PATH tables=$TABLE_COUNT sync_runs=$SYNC_RUN_COUNT"
