#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Sequence

import psycopg
import requests

from arvento_api_sync import HEADERS, build_params, fetch_chunk
from arvento_api_parser import parse_rows

TZ = timezone(timedelta(hours=3))
STAGE_TABLE = "arvento_gps_stage"


def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def normalize_plate(value: str) -> str:
    return "".join(ch for ch in value.upper() if ch.isalnum())


def source_hash(row) -> str:
    payload = "|".join([
        row.device_no or "",
        normalize_plate(row.plate),
        row.timestamp.isoformat(),
        f"{row.latitude:.7f}",
        f"{row.longitude:.7f}",
        row.event_type or "",
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ensure_schema(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE gps_points ADD COLUMN IF NOT EXISTS region_name TEXT")
    conn.commit()


def ensure_partition(conn: psycopg.Connection, day: date) -> None:
    start = datetime.combine(day, datetime.min.time(), TZ)
    end = start + timedelta(days=1)
    name = f"gps_points_{day:%Y_%m_%d}"
    with conn.cursor() as cur:
        cur.execute(
            psycopg.sql.SQL(
                "CREATE TABLE IF NOT EXISTS {} PARTITION OF gps_points "
                "FOR VALUES FROM ({}) TO ({})"
            ).format(
                psycopg.sql.Identifier(name),
                psycopg.sql.Literal(start),
                psycopg.sql.Literal(end),
            )
        )
        cur.execute(
            psycopg.sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (normalized_plate, event_time)").format(
                psycopg.sql.Identifier(f"ix_{name}_plate_time"),
                psycopg.sql.Identifier(name),
            )
        )
        cur.execute(
            psycopg.sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} USING GIST (position)").format(
                psycopg.sql.Identifier(f"ix_{name}_position"),
                psycopg.sql.Identifier(name),
            )
        )
        cur.execute(
            psycopg.sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (region_name)").format(
                psycopg.sql.Identifier(f"ix_{name}_region"),
                psycopg.sql.Identifier(name),
            )
        )


def partition_days(start: datetime, end: datetime) -> list[date]:
    """Return local calendar days touched by the half-open interval [start, end)."""
    if end <= start:
        return []
    first_day = start.astimezone(TZ).date()
    last_day = (end.astimezone(TZ) - timedelta(microseconds=1)).date()
    result: list[date] = []
    current = first_day
    while current <= last_day:
        result.append(current)
        current += timedelta(days=1)
    return result


def ensure_range_partitions(
    conn: psycopg.Connection,
    start: datetime,
    end: datetime,
) -> list[date]:
    """Create/check every required daily partition exactly once per sync run."""
    days = partition_days(start, end)
    for day in days:
        ensure_partition(conn, day)
    conn.commit()
    return days


def ensure_stage_table(conn: psycopg.Connection) -> None:
    """Create one session-local COPY staging table reused by all chunks."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TEMP TABLE IF NOT EXISTS {STAGE_TABLE} (
                device_no TEXT NOT NULL,
                plate TEXT NOT NULL,
                normalized_plate TEXT NOT NULL,
                event_time TIMESTAMPTZ NOT NULL,
                latitude DOUBLE PRECISION NOT NULL,
                longitude DOUBLE PRECISION NOT NULL,
                speed_kmh DOUBLE PRECISION,
                distance_km DOUBLE PRECISION,
                address TEXT,
                event_type TEXT,
                driver TEXT,
                pause_duration TEXT,
                idling_duration TEXT,
                ignition_duration TEXT,
                region_name TEXT,
                source_hash TEXT NOT NULL
            ) ON COMMIT PRESERVE ROWS
            """
        )
    conn.commit()


def _event_time(row) -> datetime:
    return row.timestamp.replace(tzinfo=TZ) if row.timestamp.tzinfo is None else row.timestamp


def prepare_stage_rows(rows: Sequence[object]) -> list[tuple[object, ...]]:
    prepared: list[tuple[object, ...]] = []
    for row in rows:
        prepared.append(
            (
                row.device_no or "",
                row.plate,
                normalize_plate(row.plate),
                _event_time(row),
                row.latitude,
                row.longitude,
                row.speed,
                row.distance,
                row.address,
                row.event_type,
                row.driver,
                row.pause_duration,
                row.idling_duration,
                row.ignition_duration,
                row.region_name,
                source_hash(row),
            )
        )
    return prepared


def _copy_stage_rows(cur: psycopg.Cursor, prepared: Iterable[tuple[object, ...]]) -> None:
    cur.execute(f"TRUNCATE {STAGE_TABLE}")
    with cur.copy(
        f"""
        COPY {STAGE_TABLE} (
            device_no, plate, normalized_plate, event_time,
            latitude, longitude, speed_kmh, distance_km,
            address, event_type, driver, pause_duration,
            idling_duration, ignition_duration, region_name, source_hash
        ) FROM STDIN
        """
    ) as copy:
        for values in prepared:
            copy.write_row(values)


def _insert_gps_and_queue(cur: psycopg.Cursor) -> tuple[int, set[tuple[str, date]]]:
    cur.execute(
        f"""
        WITH inserted AS (
            INSERT INTO gps_points (
                device_no, plate, normalized_plate, event_time,
                latitude, longitude, position, speed_kmh, distance_km,
                address, event_type, driver, pause_duration,
                idling_duration, ignition_duration, region_name, source_hash
            )
            SELECT
                device_no,
                plate,
                normalized_plate,
                event_time,
                latitude,
                longitude,
                ST_SetSRID(ST_MakePoint(longitude, latitude),4326)::geography,
                speed_kmh,
                distance_km,
                address,
                event_type,
                driver,
                pause_duration,
                idling_duration,
                ignition_duration,
                region_name,
                source_hash
            FROM {STAGE_TABLE}
            ON CONFLICT (source_hash, event_time) DO NOTHING
            RETURNING normalized_plate, event_time
        ),
        queued AS (
            INSERT INTO recalculation_queue (
                normalized_plate, day, reason
            )
            SELECT DISTINCT
                normalized_plate,
                (event_time AT TIME ZONE 'Europe/Istanbul')::date,
                'new_gps_data'
            FROM inserted
            ON CONFLICT (normalized_plate, day) DO UPDATE SET
                reason=EXCLUDED.reason,
                created_at=now(),
                completed_at=NULL
            RETURNING normalized_plate, day
        )
        SELECT
            (SELECT COUNT(*) FROM inserted) AS inserted_count,
            COALESCE(
                array_agg(normalized_plate || '|' || day::text)
                    FILTER (WHERE normalized_plate IS NOT NULL),
                ARRAY[]::text[]
            ) AS affected
        FROM queued
        """
    )
    inserted_count, affected_values = cur.fetchone()
    affected: set[tuple[str, date]] = set()
    for value in affected_values or []:
        plate, day_text = value.rsplit("|", 1)
        affected.add((plate, date.fromisoformat(day_text)))
    return int(inserted_count or 0), affected


def _update_missing_regions(cur: psycopg.Cursor) -> int:
    cur.execute(
        f"""
        UPDATE gps_points AS gps
        SET region_name = stage.region_name
        FROM (
            SELECT DISTINCT ON (source_hash, event_time)
                source_hash,
                event_time,
                region_name
            FROM {STAGE_TABLE}
            WHERE COALESCE(region_name, '') <> ''
            ORDER BY source_hash, event_time, region_name DESC
        ) AS stage
        WHERE gps.source_hash = stage.source_hash
          AND gps.event_time = stage.event_time
          AND COALESCE(gps.region_name, '') = ''
        """
    )
    return int(cur.rowcount or 0)


def _upsert_vehicles(cur: psycopg.Cursor, group_name: str) -> int:
    cur.execute(
        f"""
        WITH ranked AS (
            SELECT
                device_no,
                plate,
                normalized_plate,
                driver,
                MIN(event_time) OVER (PARTITION BY device_no) AS first_seen_at,
                MAX(event_time) OVER (PARTITION BY device_no) AS last_seen_at,
                ROW_NUMBER() OVER (
                    PARTITION BY device_no
                    ORDER BY event_time DESC, normalized_plate, plate
                ) AS row_number
            FROM {STAGE_TABLE}
            WHERE COALESCE(device_no, '') <> ''
        )
        INSERT INTO vehicles (
            device_no, plate, normalized_plate, driver, group_name,
            first_seen_at, last_seen_at
        )
        SELECT
            device_no,
            plate,
            normalized_plate,
            driver,
            %s,
            first_seen_at,
            last_seen_at
        FROM ranked
        WHERE row_number = 1
        ON CONFLICT (device_no) DO UPDATE SET
            plate=EXCLUDED.plate,
            normalized_plate=EXCLUDED.normalized_plate,
            driver=EXCLUDED.driver,
            group_name=EXCLUDED.group_name,
            first_seen_at=LEAST(
                COALESCE(vehicles.first_seen_at, EXCLUDED.first_seen_at),
                EXCLUDED.first_seen_at
            ),
            last_seen_at=GREATEST(
                COALESCE(vehicles.last_seen_at, EXCLUDED.last_seen_at),
                EXCLUDED.last_seen_at
            ),
            updated_at=now()
        """,
        (group_name,),
    )
    return int(cur.rowcount or 0)


def insert_rows(
    conn: psycopg.Connection,
    rows: Sequence[object],
    group_name: str,
) -> tuple[int, set[tuple[str, date]]]:
    """Bulk-load one API chunk without per-row SQL or partition DDL."""
    if not rows:
        return 0, set()

    prepared = prepare_stage_rows(rows)
    with conn.cursor() as cur:
        _copy_stage_rows(cur, prepared)
        inserted, affected = _insert_gps_and_queue(cur)
        _update_missing_regions(cur)
        _upsert_vehicles(cur, group_name)
    return inserted, affected


def create_run(conn, start: datetime, end: datetime, group: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sync_runs(period_start,period_end,group_name,status) VALUES(%s,%s,%s,'RUNNING') RETURNING id",
            (start, end, group),
        )
        return cur.fetchone()[0]


def finish_run(conn, run_id: int, status: str, totals: dict[str, int], error: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE sync_runs SET finished_at=now(), status=%s, chunks_total=%s,
                chunks_success=%s, rows_received=%s, rows_inserted=%s, error_message=%s
            WHERE id=%s
            """,
            (status, totals['chunks'], totals['success'], totals['received'], totals['inserted'], error, run_id),
        )


def sync_range(start: datetime, end: datetime) -> None:
    database_url = os.environ["DATABASE_URL"]
    username = os.environ["ARVENTO_USER"]
    pin1 = os.environ["ARVENTO_PIN1"]
    pin2 = os.environ["ARVENTO_PIN2"]
    group = os.environ.get("ARVENTO_GROUP", "TSM")
    chunk_minutes = env_int("ARVENTO_CHUNK_MINUTES", 120)
    minute_dif = env_int("ARVENTO_MINUTE_DIF", 180)
    timeout = env_int("ARVENTO_HTTP_TIMEOUT", 180)
    retries = env_int("ARVENTO_HTTP_RETRIES", 3)

    totals = {'chunks': 0, 'success': 0, 'received': 0, 'inserted': 0}
    with psycopg.connect(database_url) as conn, requests.Session() as session:
        ensure_schema(conn)
        ensured_days = ensure_range_partitions(conn, start, end)
        ensure_stage_table(conn)
        print(
            "Prepared GPS partitions: "
            + ", ".join(day.isoformat() for day in ensured_days),
            flush=True,
        )
        session.headers.update(HEADERS)
        run_id = create_run(conn, start, end, group)
        conn.commit()
        current = start
        try:
            while current < end:
                chunk_end = min(current + timedelta(minutes=chunk_minutes), end)
                totals['chunks'] += 1
                started = time.monotonic()
                try:
                    params = build_params(
                        username, pin1, pin2, group, "",
                        current.replace(tzinfo=None), chunk_end.replace(tzinfo=None), minute_dif,
                    )
                    params["chkRegion"] = "1"
                    response = fetch_chunk(session, params, timeout, retries)
                    rows = parse_rows(response.text)
                    inserted, _ = insert_rows(conn, rows, group)
                    totals['received'] += len(rows)
                    totals['inserted'] += inserted
                    totals['success'] += 1
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO sync_chunks(run_id,chunk_start,chunk_end,status,rows_received,rows_inserted,attempt_count,duration_seconds)
                            VALUES(%s,%s,%s,'SUCCESS',%s,%s,1,%s)
                            """,
                            (run_id, current, chunk_end, len(rows), inserted, time.monotonic() - started),
                        )
                    conn.commit()
                    print(
                        f"{current:%Y-%m-%d %H:%M}–{chunk_end:%H:%M}: "
                        f"received={len(rows)} inserted={inserted}",
                        flush=True,
                    )
                except Exception as exc:
                    conn.rollback()
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO sync_chunks(run_id,chunk_start,chunk_end,status,error_message,duration_seconds)
                            VALUES(%s,%s,%s,'FAILED',%s,%s)
                            """,
                            (run_id, current, chunk_end, str(exc)[:2000], time.monotonic() - started),
                        )
                    conn.commit()
                    print(f"FAILED {current}–{chunk_end}: {exc}", flush=True)
                current = chunk_end
            status = 'SUCCESS' if totals['success'] == totals['chunks'] else 'PARTIAL'
            finish_run(conn, run_id, status, totals)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            finish_run(conn, run_id, 'FAILED', totals, str(exc)[:4000])
            conn.commit()
            raise


def retention() -> None:
    days = env_int("ARVENTO_RETENTION_DAYS", 60)
    cutoff = (datetime.now(TZ) - timedelta(days=days)).date()
    database_url = os.environ["DATABASE_URL"]
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT child.relname
            FROM pg_inherits
            JOIN pg_class parent ON pg_inherits.inhparent = parent.oid
            JOIN pg_class child ON pg_inherits.inhrelid = child.oid
            WHERE parent.relname='gps_points' AND child.relname ~ '^gps_points_[0-9]{4}_[0-9]{2}_[0-9]{2}$'
            """
        )
        for (name,) in cur.fetchall():
            part_day = datetime.strptime(name.removeprefix("gps_points_"), "%Y_%m_%d").date()
            if part_day < cutoff:
                cur.execute(psycopg.sql.SQL("DROP TABLE IF EXISTS {}").format(psycopg.sql.Identifier(name)))
                print(f"Dropped old partition: {name}")
        cur.execute("DELETE FROM sync_chunks WHERE chunk_end < now() - interval '90 days'")
        cur.execute("DELETE FROM sync_runs WHERE period_end < now() - interval '90 days'")


def daemon() -> None:
    interval = env_int("ARVENTO_SYNC_INTERVAL_SECONDS", 1800)
    lookback = env_int("ARVENTO_SYNC_LOOKBACK_HOURS", 6)
    while True:
        try:
            end = datetime.now(TZ).replace(microsecond=0)
            sync_range(end - timedelta(hours=lookback), end)
            retention()
        except Exception as exc:
            print(f"Sync cycle failed: {exc}", flush=True)
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("daemon")
    recent = sub.add_parser("recent")
    recent.add_argument("--hours", type=int, default=6)
    day = sub.add_parser("day")
    day.add_argument("date")
    sub.add_parser("retention")
    args = parser.parse_args()

    if args.command == "daemon":
        daemon()
    elif args.command == "recent":
        end = datetime.now(TZ).replace(microsecond=0)
        sync_range(end - timedelta(hours=args.hours), end)
    elif args.command == "day":
        start = datetime.fromisoformat(args.date).replace(tzinfo=TZ)
        sync_range(start, start + timedelta(days=1))
    elif args.command == "retention":
        retention()


if __name__ == "__main__":
    main()
