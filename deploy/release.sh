#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${1:-/opt/arvento_report}"
COMPOSE_FILE="$ROOT/docker-compose.server.yml"
ENV_FILE="$ROOT/.env"
STATE_ROOT="${ARVENTO_RELEASE_STATE_DIR:-/var/lib/arvento-report/releases}"
SBIN_DIR="${ARVENTO_SBIN_DIR:-/usr/local/sbin}"
SYSTEMD_DIR="${ARVENTO_SYSTEMD_DIR:-/etc/systemd/system}"
DEFAULTS_PATH="${ARVENTO_DEFAULTS_PATH:-/etc/default/arvento-report}"
RELEASE_LOCK_FILE="${ARVENTO_RELEASE_LOCK_FILE:-/run/arvento-release.lock}"
SMOKE_SCRIPT="${ARVENTO_SMOKE_SCRIPT:-$ROOT/deploy/post-deploy-smoke.sh}"
PYTHON_BIN="${ARVENTO_PYTHON_BIN:-python3}"
TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
TIMERS=(
    arvento-intraday-pipeline.timer
    arvento-nightly-correction.timer
    arvento-backup.timer
)
ROLLBACK_REQUIRED=0
PREVIOUS_IMAGE_ID=""
ROLLBACK_TAG=""
STATE_DIR=""

log() {
    printf '%s | %s\n' "$(date '+%F %T %z')" "$*"
}

fail() {
    log "RELEASE FAILED: $*"
    exit 1
}

set_env_value() {
    local key="$1"
    local value="$2"
    "$PYTHON_BIN" - "$ENV_FILE" "$key" "$value" <<'PY'
from __future__ import annotations

import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
lines = path.read_text(encoding="utf-8").splitlines()
result: list[str] = []
replaced = False
for line in lines:
    if line.startswith(f"{key}="):
        if not replaced:
            result.append(f"{key}={value}")
            replaced = True
    else:
        result.append(line)
if not replaced:
    result.append(f"{key}={value}")
temporary = path.with_name(f".{path.name}.release-{os.getpid()}")
temporary.write_text("\n".join(result) + "\n", encoding="utf-8")
os.chmod(temporary, 0o600)
os.replace(temporary, path)
PY
}

snapshot_host_files() {
    install -d -m 0700 "$STATE_DIR/usr-local-sbin" "$STATE_DIR/systemd"
    for path in \
        "$SBIN_DIR/arvento-sync-and-cache" \
        "$SBIN_DIR/arvento-backup" \
        "$SBIN_DIR/arvento-healthcheck" \
        "$SBIN_DIR/arvento-post-deploy-smoke" \
        "$SBIN_DIR/arvento-release" \
        "$SBIN_DIR/arvento-restore-drill" \
        "$DEFAULTS_PATH"; do
        [[ -f "$path" ]] && cp -a "$path" "$STATE_DIR/usr-local-sbin/"
    done
    for path in "$SYSTEMD_DIR"/arvento-*.service "$SYSTEMD_DIR"/arvento-*.timer; do
        [[ -f "$path" ]] && cp -a "$path" "$STATE_DIR/systemd/"
    done
    return 0
}

restore_host_files() {
    local path base
    for path in "$STATE_DIR/usr-local-sbin"/*; do
        [[ -f "$path" ]] || continue
        base="$(basename "$path")"
        if [[ "$base" == "arvento-report" ]]; then
            install -m 0600 "$path" "$DEFAULTS_PATH"
        else
            install -m 0755 "$path" "$SBIN_DIR/$base"
        fi
    done
    for path in "$STATE_DIR/systemd"/*; do
        [[ -f "$path" ]] || continue
        install -m 0644 "$path" "$SYSTEMD_DIR/$(basename "$path")"
    done
    systemctl daemon-reload
}

rollback_release() {
    local original_rc="$1"
    set +e
    log "ROLLBACK: возврат на arvento-report:$ROLLBACK_TAG"
    systemctl stop "${TIMERS[@]}"
    set_env_value ARVENTO_IMAGE_TAG "$ROLLBACK_TAG"
    export ARVENTO_IMAGE_TAG="$ROLLBACK_TAG"
    restore_host_files
    docker compose --project-directory "$ROOT" -f "$COMPOSE_FILE" \
        up -d --no-build --force-recreate geofence-editor report-portal
    systemctl start "${TIMERS[@]}"
    bash "$SMOKE_SCRIPT" "$ROOT" "$ROLLBACK_TAG" ""
    rollback_rc=$?
    if (( rollback_rc == 0 )); then
        log "ROLLBACK OK: production возвращён на сохранённый image"
    else
        log "ROLLBACK FAILED: требуется ручное вмешательство; rc=$rollback_rc"
    fi
    exit "$original_rc"
}

on_exit() {
    local rc=$?
    trap - EXIT
    if (( rc != 0 && ROLLBACK_REQUIRED == 1 )); then
        rollback_release "$rc"
    fi
    systemctl start "${TIMERS[@]}" >/dev/null 2>&1 || true
    exit "$rc"
}
trap on_exit EXIT

if [[ "${ARVENTO_RELEASE_SKIP_ROOT_CHECK:-0}" != "1" ]]; then
    [[ "${EUID:-$(id -u)}" -eq 0 ]] || fail "запустите release от root"
fi
for command in docker git python3 systemctl install flock; do
    command -v "$command" >/dev/null || fail "не установлена команда: $command"
done
exec 8>"$RELEASE_LOCK_FILE"
flock -n 8 || fail "другой release уже выполняется"
[[ -d "$ROOT/.git" ]] || fail "$ROOT не является Git checkout"
[[ -f "$ENV_FILE" ]] || fail "$ENV_FILE не найден"
[[ -f "$COMPOSE_FILE" ]] || fail "$COMPOSE_FILE не найден"
[[ -z "$(git -C "$ROOT" status --porcelain)" ]] || fail "Git checkout содержит изменения"

TARGET_SHA="$(git -C "$ROOT" rev-parse --verify HEAD)"
[[ "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "не удалось определить commit SHA"
TARGET_TAG="git-$TARGET_SHA"

COMPOSE=(docker compose --project-directory "$ROOT" -f "$COMPOSE_FILE")
CURRENT_CONTAINER="$("${COMPOSE[@]}" ps -q report-portal)"
[[ -n "$CURRENT_CONTAINER" ]] || fail "report-portal не запущен; используйте install.sh для первой установки"
PREVIOUS_IMAGE_ID="$(docker inspect -f '{{.Image}}' "$CURRENT_CONTAINER")"
[[ -n "$PREVIOUS_IMAGE_ID" ]] || fail "не удалось определить текущий image"

log "BACKUP: создание контрольной копии перед release"
systemctl start arvento-backup.service

log "PREFLIGHT: production healthcheck после контрольного backup"
"$SBIN_DIR/arvento-healthcheck"

install -d -m 0700 "$STATE_ROOT"
STATE_DIR="$STATE_ROOT/$TIMESTAMP-$TARGET_SHA"
install -d -m 0700 "$STATE_DIR"
snapshot_host_files

ROLLBACK_TAG="rollback-$TIMESTAMP"
docker image tag "$PREVIOUS_IMAGE_ID" "arvento-report:$ROLLBACK_TAG"
ROLLBACK_REQUIRED=1

log "TIMERS: остановка на время release"
systemctl stop "${TIMERS[@]}"

set_env_value ARVENTO_IMAGE_TAG "$TARGET_TAG"
export ARVENTO_IMAGE_TAG="$TARGET_TAG"
export ARVENTO_COMMIT_SHA="$TARGET_SHA"

SKIP_BUILD=0
if docker image inspect "arvento-report:$TARGET_TAG" >/dev/null 2>&1; then
    revision="$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "arvento-report:$TARGET_TAG")"
    [[ "$revision" == "$TARGET_SHA" ]] || fail "существующий $TARGET_TAG имеет revision=$revision"
    SKIP_BUILD=1
fi

log "DEPLOY: commit=$TARGET_SHA image=arvento-report:$TARGET_TAG"
INSTALL_ENABLE_TIMERS=0 INSTALL_SKIP_BUILD="$SKIP_BUILD" \
    ARVENTO_IMAGE_TAG="$TARGET_TAG" ARVENTO_COMMIT_SHA="$TARGET_SHA" \
    bash "$ROOT/deploy/install.sh" "$ROOT"

systemctl start "${TIMERS[@]}"

log "SMOKE: проверка новой версии"
bash "$SMOKE_SCRIPT" "$ROOT" "$TARGET_TAG" "$TARGET_SHA"

cat > "$STATE_ROOT/current.env" <<EOF
CURRENT_COMMIT_SHA=$TARGET_SHA
CURRENT_IMAGE_TAG=$TARGET_TAG
PREVIOUS_IMAGE_TAG=$ROLLBACK_TAG
PREVIOUS_STATE_DIR=$STATE_DIR
DEPLOYED_AT=$TIMESTAMP
EOF
chmod 0600 "$STATE_ROOT/current.env"

ROLLBACK_REQUIRED=0
trap - EXIT
log "RELEASE OK: commit=$TARGET_SHA image=arvento-report:$TARGET_TAG"
