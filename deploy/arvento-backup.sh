#!/usr/bin/env bash
set -Eeuo pipefail

[[ -r /etc/default/arvento-report ]] && . /etc/default/arvento-report

ROOT="${ARVENTO_ROOT:-/opt/arvento_report}"
BACKUP_DIR="${BACKUP_DIR:-/opt/arvento_backups}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
COMPOSE_FILE="$ROOT/docker-compose.server.yml"
LOCK_FILE="/run/arvento-backup.lock"

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
[[ "$BACKUP_RETENTION_DAYS" =~ ^[0-9]+$ ]] || fail "BACKUP_RETENTION_DAYS должен быть числом"

mkdir -p "$BACKUP_DIR"
chmod 0700 "$BACKUP_DIR"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    log "SKIP: резервное копирование уже выполняется"
    exit 0
fi

COMPOSE=(
    docker compose
    --project-directory "$ROOT"
    -f "$COMPOSE_FILE"
)

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

log "START: создание $FINAL_PATH"

docker exec "$POSTGRES_CONTAINER" \
    sh -lc 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
    > "$TEMP_PATH"

[[ -s "$TEMP_PATH" ]] || fail "создан пустой backup"

docker exec -i "$POSTGRES_CONTAINER" pg_restore --list \
    < "$TEMP_PATH" \
    > /dev/null

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
