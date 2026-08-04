#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${1:-/opt/arvento_report}"
COMPOSE_FILE="$ROOT/docker-compose.server.yml"
ENV_FILE="$ROOT/.env"
ENABLE_TIMERS="${INSTALL_ENABLE_TIMERS:-1}"

log() {
    printf '%s | %s\n' "$(date '+%F %T %z')" "$*"
}

fail() {
    log "ERROR: $*"
    exit 1
}

[[ "${EUID:-$(id -u)}" -eq 0 ]] || fail "запустите installer от root"

for command in docker git curl flock timeout python3 systemctl install; do
    command -v "$command" >/dev/null || fail "не установлена команда: $command"
done

docker compose version >/dev/null || fail "Docker Compose plugin не установлен"

[[ -d "$ROOT/.git" ]] || fail "$ROOT не является Git checkout"
[[ -f "$COMPOSE_FILE" ]] || fail "$COMPOSE_FILE не найден"

if [[ ! -f "$ENV_FILE" ]]; then
    cp "$ROOT/.env.server.example" "$ENV_FILE"
    chmod 0600 "$ENV_FILE"
    fail "создан $ENV_FILE; заполните его и повторите установку"
fi
chmod 0600 "$ENV_FILE"

log "Проверка обязательных переменных"
python3 - "$ENV_FILE" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

path = Path(sys.argv[1])
values: dict[str, str] = {}
for raw_line in path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    values[key.strip()] = value.strip()

required = (
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "DATABASE_URL",
    "ARVENTO_USER",
    "ARVENTO_PIN1",
    "ARVENTO_PIN2",
    "ARVENTO_GROUP",
)
missing = [key for key in required if not values.get(key)]
if missing:
    raise SystemExit("Не заполнены переменные: " + ", ".join(missing))

if "CHANGE_ME" in values["POSTGRES_PASSWORD"] or "CHANGE_ME" in values["DATABASE_URL"]:
    raise SystemExit("Замените CHANGE_ME в POSTGRES_PASSWORD и DATABASE_URL")

if "@postgres:5432/" not in values["DATABASE_URL"]:
    raise SystemExit("DATABASE_URL внутри Docker должен использовать host postgres:5432")

if any(character in values["POSTGRES_PASSWORD"] for character in "@:/?#[]"):
    raise SystemExit(
        "POSTGRES_PASSWORD содержит символы, требующие URL-кодирования. "
        "Для чистой установки используйте URL-safe пароль: openssl rand -hex 32"
    )
PY

cd "$ROOT"

log "Статическая проверка репозитория"
python3 verify_repository.py
python3 verify_deployment.py

log "Проверка Docker Compose"
docker compose \
    --project-directory "$ROOT" \
    -f "$COMPOSE_FILE" \
    config --quiet

log "Подготовка runtime-каталогов для непривилегированного пользователя контейнера"
install -d -m 0750 -o 10001 -g 10001 \
    "$ROOT/logs" \
    "$ROOT/reports" \
    "$ROOT/input"

log "Сборка проверенного server image"
docker compose \
    --project-directory "$ROOT" \
    -f "$COMPOSE_FILE" \
    build --pull report-portal

log "Запуск core stack без legacy gps-sync daemon"
docker compose \
    --project-directory "$ROOT" \
    -f "$COMPOSE_FILE" \
    up -d postgres geofence-editor report-portal

wait_for_health() {
    local service="$1"
    local attempts="${2:-60}"
    local container state

    for ((attempt = 1; attempt <= attempts; attempt++)); do
        container="$(
            docker compose \
                --project-directory "$ROOT" \
                -f "$COMPOSE_FILE" \
                ps -q "$service"
        )"
        if [[ -n "$container" ]]; then
            state="$(
                docker inspect \
                    -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
                    "$container"
            )"
            if [[ "$state" == "healthy" || "$state" == "running" ]]; then
                log "$service: $state"
                return 0
            fi
        fi
        sleep 5
    done

    docker compose \
        --project-directory "$ROOT" \
        -f "$COMPOSE_FILE" \
        logs --tail=200 "$service" || true
    fail "$service не перешёл в healthy/running"
}

wait_for_health postgres 60
wait_for_health geofence-editor 60
wait_for_health report-portal 60

log "Установка production scripts"
install -m 0755 "$ROOT/deploy/arvento-sync-and-cache.sh" \
    /usr/local/sbin/arvento-sync-and-cache
install -m 0755 "$ROOT/deploy/arvento-backup.sh" \
    /usr/local/sbin/arvento-backup
install -m 0755 "$ROOT/deploy/arvento-healthcheck.sh" \
    /usr/local/sbin/arvento-healthcheck

log "Установка актуальных systemd units"
install -m 0644 "$ROOT"/deploy/systemd/*.service /etc/systemd/system/
install -m 0644 "$ROOT"/deploy/systemd/*.timer /etc/systemd/system/

log "Формирование /etc/default/arvento-report"
python3 - "$ENV_FILE" "$ROOT" > /etc/default/arvento-report <<'PY'
from __future__ import annotations

import shlex
import sys
from pathlib import Path

path = Path(sys.argv[1])
root = sys.argv[2]
values: dict[str, str] = {}
for raw_line in path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    values[key.strip()] = value.strip()

result = {
    "ARVENTO_ROOT": root,
    "PIPELINE_INTRADAY_TIMEOUT_SECONDS": values.get(
        "PIPELINE_INTRADAY_TIMEOUT_SECONDS", "2700"
    ),
    "PIPELINE_NIGHTLY_TIMEOUT_SECONDS": values.get(
        "PIPELINE_NIGHTLY_TIMEOUT_SECONDS", "7200"
    ),
    "PIPELINE_CACHE_TIMEOUT_SECONDS": values.get(
        "PIPELINE_CACHE_TIMEOUT_SECONDS", "3600"
    ),
    "BACKUP_DIR": values.get("BACKUP_DIR", "/opt/arvento_backups"),
    "BACKUP_RETENTION_DAYS": values.get("BACKUP_RETENTION_DAYS", "14"),
    "BACKUP_PIPELINE_LOCK_WAIT_SECONDS": values.get(
        "BACKUP_PIPELINE_LOCK_WAIT_SECONDS", "600"
    ),
    "BACKUP_DUMP_TIMEOUT_SECONDS": values.get(
        "BACKUP_DUMP_TIMEOUT_SECONDS", "2700"
    ),
    "BACKUP_VERIFY_TIMEOUT_SECONDS": values.get(
        "BACKUP_VERIFY_TIMEOUT_SECONDS", "300"
    ),
    "REPORT_PORTAL_PORT": values.get("REPORT_PORTAL_PORT", "18084"),
    "GEOFENCE_EDITOR_PORT": values.get("GEOFENCE_EDITOR_PORT", "18083"),
    "MIN_FREE_DISK_PERCENT": values.get("MIN_FREE_DISK_PERCENT", "10"),
}
for key, value in result.items():
    print(f"{key}={shlex.quote(value)}")
PY
chmod 0600 /etc/default/arvento-report

BACKUP_DIR="$(
    awk -F= '$1 == "BACKUP_DIR" {gsub(/^\047|\047$/, "", $2); print $2}' \
        /etc/default/arvento-report
)"
BACKUP_DIR="${BACKUP_DIR:-/opt/arvento_backups}"
install -d -m 0700 "$BACKUP_DIR"

# Disable and remove superseded scheduling schemes. Running any of them together
# with the unified pipeline would duplicate API traffic and database work.
for obsolete_unit in \
    arvento-consolidated-cache.timer \
    arvento-consolidated-cache.service \
    arvento-yesterday-backfill.timer \
    arvento-yesterday-backfill.service; do
    systemctl disable --now "$obsolete_unit" 2>/dev/null || true
    rm -f "/etc/systemd/system/$obsolete_unit"
done

# Stop a container created by the previous default gps-sync daemon setup.
docker compose \
    --project-directory "$ROOT" \
    -f "$COMPOSE_FILE" \
    stop gps-sync 2>/dev/null || true

systemctl daemon-reload
systemctl reset-failed \
    arvento-intraday-pipeline.service \
    arvento-nightly-correction.service \
    arvento-backup.service \
    2>/dev/null || true

if [[ "$ENABLE_TIMERS" == "1" ]]; then
    log "Включение systemd timers"
    systemctl enable --now \
        arvento-intraday-pipeline.timer \
        arvento-nightly-correction.timer \
        arvento-backup.timer
else
    log "Timers установлены, но не включены: INSTALL_ENABLE_TIMERS=$ENABLE_TIMERS"
fi

log "Проверка HTTP endpoints"
source /etc/default/arvento-report
curl --fail --silent --show-error --max-time 10 \
    "http://127.0.0.1:${GEOFENCE_EDITOR_PORT}/health" >/dev/null
curl --fail --silent --show-error --max-time 10 \
    "http://127.0.0.1:${REPORT_PORTAL_PORT}/health" >/dev/null

log "Установка завершена"
docker compose \
    --project-directory "$ROOT" \
    -f "$COMPOSE_FILE" \
    ps
systemctl list-timers --all --no-pager | grep -E 'arvento-(intraday|nightly|backup)' || true

echo
echo "Следующая проверка: sudo /usr/local/sbin/arvento-healthcheck"
echo "Логи pipeline: journalctl -u arvento-intraday-pipeline.service -n 200 --no-pager"
