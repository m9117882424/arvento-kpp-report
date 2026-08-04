#!/usr/bin/env bash
set -Eeuo pipefail

[[ -r /etc/default/arvento-report ]] && . /etc/default/arvento-report

ROOT="${ARVENTO_ROOT:-/opt/arvento_report}"
BACKUP_DIR="${BACKUP_DIR:-/opt/arvento_backups}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
BACKUP_PIPELINE_LOCK_WAIT_SECONDS="${BACKUP_PIPELINE_LOCK_WAIT_SECONDS:-600}"
BACKUP_DUMP_TIMEOUT_SECONDS="${BACKUP_DUMP_TIMEOUT_SECONDS:-2700}"
BACKUP_VERIFY_TIMEOUT_SECONDS="${BACKUP_VERIFY_TIMEOUT_SECONDS:-300}"
COMPOSE_FILE="$ROOT/docker-compose.server.yml"
BACKUP_LOCK_FILE="/run/arvento-backup.lock"
PIPELINE_LOCK_FILE="/run/arvento-sync-and-cache.lock"

log() {
    printf '%s | %s\n' "$(date '+%F %T %z')" "$*"
}

fail() {
    log "ERROR: $*"
    exit 1
}

[[ -d "$ROOT" ]] || fail "каталог $ROOT не найден"
[[ -f "$ROOT/.env" ]] || fail "файл $ROOT/.env не найден"
[[ -f "$COMPOSE_FILE" ]] || fail "файл $COMPOSE_FILE не найден"

for value_name in \
    BACKUP_RETENTION_DAYS \
    BACKUP_PIPELINE_LOCK_WAIT_SECONDS \
    BACKUP_DUMP_TIMEOUT_SECONDS \
    BACKUP_VERIFY_TIMEOUT_SECONDS; do
    value="${!value_name}"
    [[ "$value" =~ ^[0-9]+$ ]] || fail "$value_name должен быть целым числом"
done

mkdir -p "$BACKUP_DIR"
chmod 0700 "$BACKUP_DIR"

# Prevent duplicate backup processes first. Then join the shared pipeline lock so
# pg_dump does not add load while Arvento sync/cache work is active.
exec 9>"$BACKUP_LOCK_FILE"
if ! flock -n 9; then
    log "SKIP: резервное копирование уже выполняется"
    exit 0
fi

exec 8>"$PIPELINE_LOCK_FILE"
log "LOCK: ожидание общей цепочки до ${BACKUP_PIPELINE_LOCK_WAIT_SECONDS}s"
if ! flock -w "$BACKUP_PIPELINE_LOCK_WAIT_SECONDS" 8; then
    fail "общая цепочка Arvento не освободила lock за ${BACKUP_PIPELINE_LOCK_WAIT_SECONDS}s"
fi

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

STAMP="$(TZ=Europe/Istanbul date '+%Y%m%d_%H%M%S')"
FINAL_PATH="$BACKUP_DIR/arvento_report_${STAMP}.dump"
TEMP_PATH="${FINAL_PATH}.partial"

cleanup() {
    rm -f "$TEMP_PATH"
}
trap cleanup EXIT

log "START: создание $FINAL_PATH timeout=${BACKUP_DUMP_TIMEOUT_SECONDS}s"

if timeout --signal=TERM --kill-after=60 "$BACKUP_DUMP_TIMEOUT_SECONDS" \
    docker exec "$POSTGRES_CONTAINER" \
        sh -lc 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
        > "$TEMP_PATH"; then
    :
else
    rc=$?
    fail "pg_dump завершился с rc=$rc"
fi

[[ -s "$TEMP_PATH" ]] || fail "создан пустой backup"

log "VERIFY: проверка структуры backup timeout=${BACKUP_VERIFY_TIMEOUT_SECONDS}s"
if timeout --signal=TERM --kill-after=30 "$BACKUP_VERIFY_TIMEOUT_SECONDS" \
    docker exec -i "$POSTGRES_CONTAINER" pg_restore --list \
        < "$TEMP_PATH" \
        > /dev/null; then
    :
else
    rc=$?
    fail "pg_restore --list завершился с rc=$rc"
fi

chmod 0600 "$TEMP_PATH"
mv -f "$TEMP_PATH" "$FINAL_PATH"
trap - EXIT

find "$BACKUP_DIR" \
    -maxdepth 1 \
    -type f \
    -name 'arvento_report_*.dump' \
    -mtime "+$BACKUP_RETENTION_DAYS" \
    -print \
    -delete

SIZE="$(du -h "$FINAL_PATH" | awk '{print $1}')"
log "SUCCESS: backup=$FINAL_PATH size=$SIZE retention_days=$BACKUP_RETENTION_DAYS"
