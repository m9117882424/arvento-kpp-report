#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import time
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row
import requests

from arvento_api_sync import HEADERS, build_params, fetch_chunk
from arvento_api_parser import parse_rows

TZ = timezone(timedelta(hours=3))


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


def ensure_partition(conn: psycopg.Connection, day) -> None:
    start = datetime.combine(day, datetime.min.time(), TZ)
    end = start + timedelta(days=1)
    name = f"gps_points_{day:%Y_%m_%d}"
    with conn.cursor() as cur:
        cur.execute(
            psycopg.sql.SQL(
                "CREATE TABLE IF NOT EXISTS {} PARTITION OF gps_points FOR VALUES FROM (%s) TO (%s)"
            ).format(psycopg.sql.Identifier(name)),
            (start, end),
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


def insert_rows(conn: psycopg.Connection, rows, group_name: str) -> tuple[int, set[tuple[str, object]]]:
    inserted = 0
    affected: set[tuple[str, object]] = set()
    with conn.cursor() as cur:
        for row in rows:
            event_time = row.timestamp.replace(tzinfo=TZ) if row.timestamp.tzinfo is None else row.timestamp
            plate = normalize_plate(row.plate)
            ensure_partition(conn, event_time.date())
            cur.execute(
                """
                INSERT INTO gps_points (
                    device_no, plate, normalized_plate, event_time,
                    latitude, longitude, position, speed_kmh, distance_km,
                    address, event_type, driver, pause_duration,
                    idling_duration, ignition_duration, region_name, source_hash
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,
                    ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s
                )
                ON CONFLICT (source_hash, event_time) DO UPDATE SET
                    region_name = COALESCE(NULLIF(EXCLUDED.region_name, ''), gps_points.region_name)
                RETURNING (xmax = 0)
                """,
                (
                    row.device_no or "", row.plate, plate, event_time,
                    row.latitude, row.longitude, row.longitude, row.latitude,
                    row.speed, row.distance, row.address, row.event_type,
                    row.driver, row.pause_duration, row.idling_duration,
                    row.ignition_duration, row.region_name, source_hash(row),
                ),
            )
            result = cur.fetchone()
            if result and result[0]:
                inserted += 1
                affected.add((plate, event_time.date()))

            cur.execute(
                """
                INSERT INTO vehicles (
                    device_no, plate, normalized_plate, driver, group_name,
                    first_seen_at, last_seen_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (device_no) DO UPDATE SET
                    plate=EXCLUDED.plate,
                    normalized_plate=EXCLUDED.normalized_plate,
                    driver=EXCLUDED.driver,
                    group_name=EXCLUDED.group_name,
                    last_seen_at=GREATEST(vehicles.last_seen_at, EXCLUDED.last_seen_at),
                    updated_at=now()
                """,
                (row.device_no or None, row.plate, plate, row.driver, group_name, event_time, event_time),
            )

        for plate, day in affected:
            cur.execute(
                """
                INSERT INTO recalculation_queue (normalized_plate, day, reason)
                VALUES (%s,%s,'new_gps_data')
                ON CONFLICT (normalized_plate, day) DO UPDATE SET
                    reason=EXCLUDED.reason,
                    created_at=now(),
                    completed_at=NULL
                """,
                (plate, day),
            )
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
    with psycopg.connect(database_url, row_factory=dict_row) as conn, requests.Session() as session:
        ensure_schema(conn)
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
                    print(f"{current:%Y-%m-%d %H:%M}–{chunk_end:%H:%M}: received={len(rows)} inserted={inserted}", flush=True)
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
