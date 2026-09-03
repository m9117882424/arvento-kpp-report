#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${1:-${ARVENTO_ROOT:-/opt/arvento_report}}"
EXPECTED_TAG="${2:-${ARVENTO_IMAGE_TAG:-}}"
EXPECTED_SHA="${3:-${ARVENTO_COMMIT_SHA:-}}"
COMPOSE_FILE="$ROOT/docker-compose.server.yml"
DEFAULTS_FILE="${ARVENTO_DEFAULTS_FILE:-/etc/default/arvento-report}"

[[ -r "$DEFAULTS_FILE" ]] && . "$DEFAULTS_FILE"

REPORT_PORTAL_PORT="${REPORT_PORTAL_PORT:-18084}"
GEOFENCE_EDITOR_PORT="${GEOFENCE_EDITOR_PORT:-18083}"

fail() {
    printf 'POST_DEPLOY_SMOKE FAILED: %s\n' "$*" >&2
    exit 1
}

[[ -f "$COMPOSE_FILE" ]] || fail "$COMPOSE_FILE не найден"
[[ -n "$EXPECTED_TAG" ]] || fail "не передан ожидаемый image tag"

COMPOSE=(
    docker compose
    --project-directory "$ROOT"
    -f "$COMPOSE_FILE"
)

"${COMPOSE[@]}" config --quiet

for service in postgres geofence-editor report-portal; do
    container="$("${COMPOSE[@]}" ps -q "$service")"
    [[ -n "$container" ]] || fail "$service не запущен"
    state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container")"
    [[ "$state" == "healthy" || "$state" == "running" ]] || \
        fail "$service имеет состояние $state"

    if [[ "$service" != "postgres" ]]; then
        image_name="$(docker inspect -f '{{.Config.Image}}' "$container")"
        [[ "$image_name" == "arvento-report:$EXPECTED_TAG" ]] || \
            fail "$service использует $image_name вместо arvento-report:$EXPECTED_TAG"
        if [[ -n "$EXPECTED_SHA" && "$EXPECTED_SHA" != "unknown" ]]; then
            revision="$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image_name")"
            [[ "$revision" == "$EXPECTED_SHA" ]] || \
                fail "$image_name имеет revision=$revision вместо $EXPECTED_SHA"
        fi
    fi
done

curl --fail --silent --show-error --max-time 10 \
    "http://127.0.0.1:${GEOFENCE_EDITOR_PORT}/health" >/dev/null
curl --fail --silent --show-error --max-time 10 \
    "http://127.0.0.1:${REPORT_PORTAL_PORT}/health" >/dev/null

/usr/local/sbin/arvento-healthcheck

printf 'POST_DEPLOY_SMOKE OK: image=arvento-report:%s revision=%s\n' \
    "$EXPECTED_TAG" "${EXPECTED_SHA:-not-checked}"
