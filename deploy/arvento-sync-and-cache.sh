#!/usr/bin/env bash
set -Eeuo pipefail

[[ -r /etc/default/arvento-report ]] && . /etc/default/arvento-report

MODE="${1:-}"
ROOT="${ARVENTO_ROOT:-/opt/arvento_report}"
COMPOSE_FILE="$ROOT/docker-compose.server.yml"
LOCK_FILE="/run/arvento-sync-and-cache.lock"
CACHE_LOCK_FILE="/run/arvento-consolidated-cache.lock"
INTRADAY_TIMEOUT="${PIPELINE_INTRADAY_TIMEOUT_SECONDS:-2700}"
NIGHTLY_TIMEOUT="${PIPELINE_NIGHTLY_TIMEOUT_SECONDS:-7200}"
CACHE_TIMEOUT="${PIPELINE_CACHE_TIMEOUT_SECONDS:-3600}"

log() {
    printf '%s | %s\n' "$(date '+%F %T %z')" "$*"
}

fail() {
    log "ERROR: $*"
    exit 1
}

on_error() {
    local rc=$?
    log "FAILED: mode=${MODE:-unknown} rc=$rc line=${BASH_LINENO[0]:-unknown}"
    exit "$rc"
}
trap on_error ERR

case "$MODE" in
    intraday)
        REPORT_DAY="$(TZ=Europe/Istanbul date '+%F')"
        SYNC_TIMEOUT="$INTRADAY_TIMEOUT"
        SYNC_COMMAND=(python sync_arvento_gps_to_postgres.py recent --hours 6)
        TRIGGER="intraday"
        ;;
    nightly)
        REPORT_DAY="$(TZ=Europe/Istanbul date -d yesterday '+%F')"
        SYNC_TIMEOUT="$NIGHTLY_TIMEOUT"
        SYNC_COMMAND=(python sync_arvento_gps_to_postgres.py day "$REPORT_DAY")
        TRIGGER="nightly-correction"
        ;;
    *)
        printf 'Usage: %s intraday|nightly\n' "$0" >&2
        exit 2
        ;;
esac

[[ -d "$ROOT" ]] || fail "каталог $ROOT не найден"
[[ -f "$ROOT/.env" ]] || fail "файл $ROOT/.env не найден"
[[ -f "$COMPOSE_FILE" ]] || fail "файл $COMPOSE_FILE не найден"
command -v docker >/dev/null || fail "docker не установлен"
command -v flock >/dev/null || fail "flock не установлен"
command -v timeout >/dev/null || fail "timeout не установлен"

docker compose version >/dev/null

COMPOSE=(
    docker compose
    --project-directory "$ROOT"
    -f "$COMPOSE_FILE"
)

"${COMPOSE[@]}" config --quiet

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    log "SKIP: другая цепочка Arvento уже выполняется"
    exit 0
fi

POSTGRES_CONTAINER="$("${COMPOSE[@]}" ps -q postgres)"
[[ -n "$POSTGRES_CONTAINER" ]] || fail "контейнер PostgreSQL не запущен"

POSTGRES_HEALTH="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$POSTGRES_CONTAINER")"
[[ "$POSTGRES_HEALTH" == "healthy" ]] || fail "PostgreSQL не healthy: $POSTGRES_HEALTH"

db_scalar() {
    local sql="$1"
    printf '%s\n' "$sql" |
        docker exec -i "$POSTGRES_CONTAINER" \
            sh -lc 'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At'
}

mark_interrupted_run_failed() {
    local before_id="$1"
    local reason="$2"
    db_scalar "
        UPDATE sync_runs
        SET status='FAILED',
            finished_at=COALESCE(finished_at, now()),
            error_message=COALESCE(NULLIF(error_message, ''), '$reason')
        WHERE id=(
            SELECT id
            FROM sync_runs
            WHERE id > $before_id
              AND status='RUNNING'
            ORDER BY id DESC
            LIMIT 1
        )
        RETURNING id;
    " || true
}

log "START: mode=$MODE report_day=$REPORT_DAY"

BEFORE_RUN_ID="$(db_scalar 'SELECT COALESCE(MAX(id), 0) FROM sync_runs;')"
[[ "$BEFORE_RUN_ID" =~ ^[0-9]+$ ]] || fail "не удалось определить previous_run_id"

log "SYNC: начало загрузки Arvento; previous_run_id=$BEFORE_RUN_ID timeout=${SYNC_TIMEOUT}s"

set +e
timeout --signal=TERM --kill-after=60 "$SYNC_TIMEOUT" \
    "${COMPOSE[@]}" run \
        --rm \
        --no-deps \
        report-portal \
        "${SYNC_COMMAND[@]}"
SYNC_RC=$?
set -e

if (( SYNC_RC != 0 )); then
    mark_interrupted_run_failed "$BEFORE_RUN_ID" "Pipeline process stopped with rc=$SYNC_RC"
    fail "загрузка Arvento завершилась с rc=$SYNC_RC"
fi

SYNC_RESULT="$(
    db_scalar "
        SELECT
            id::text || '|' ||
            status || '|' ||
            COALESCE(chunks_total, 0)::text || '|' ||
            COALESCE(chunks_success, 0)::text || '|' ||
            COALESCE(rows_received, 0)::text || '|' ||
            COALESCE(rows_inserted, 0)::text
        FROM sync_runs
        WHERE id > $BEFORE_RUN_ID
        ORDER BY id DESC
        LIMIT 1;
    "
)"

[[ -n "$SYNC_RESULT" ]] || fail "после загрузки не появилась запись в sync_runs"

IFS='|' read -r \
    SYNC_RUN_ID \
    SYNC_STATUS \
    CHUNKS_TOTAL \
    CHUNKS_SUCCESS \
    ROWS_RECEIVED \
    ROWS_INSERTED \
    <<< "$SYNC_RESULT"

log "SYNC_RESULT: run_id=$SYNC_RUN_ID status=$SYNC_STATUS chunks=$CHUNKS_SUCCESS/$CHUNKS_TOTAL received=$ROWS_RECEIVED inserted=$ROWS_INSERTED"

[[ "$SYNC_STATUS" == "SUCCESS" ]] || \
    fail "загрузка завершилась со статусом $SYNC_STATUS; кэш не рассчитывается"

exec 8>"$CACHE_LOCK_FILE"
if ! flock -n 8; then
    log "SKIP_CACHE: другой процесс расчёта сводного уже выполняется"
    exit 0
fi

log "CACHE: расчёт сводного за $REPORT_DAY timeout=${CACHE_TIMEOUT}s"

# consolidated_cache_worker.py refresh
set +e
timeout --signal=TERM --kill-after=60 "$CACHE_TIMEOUT" \
    "${COMPOSE[@]}" run \
        --rm \
        --no-deps \
        report-portal \
        python consolidated_cache_worker.py refresh \
            --date "$REPORT_DAY" \
            --trigger "${TRIGGER}-${REPORT_DAY}"
CACHE_RC=$?
set -e

(( CACHE_RC == 0 )) || fail "расчёт кэша завершился с rc=$CACHE_RC"

log "SUCCESS: загрузка и расчёт завершены; day=$REPORT_DAY run_id=$SYNC_RUN_ID"
