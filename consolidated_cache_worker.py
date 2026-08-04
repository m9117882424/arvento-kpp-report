#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Scheduled and manual refreshes of the consolidated report cache."""
from __future__ import annotations

import argparse
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import psycopg.rows

from extended_roster_fields import apply_extended_roster_fields

apply_extended_roster_fields()

from consolidated_cache import (
    ensure_schema,
    export_stored_rosters,
    recent_status,
    upsert_cache_from_workbook,
)
from consolidated_incremental_cache import (
    complete_recalculation_queue,
    pending_recalculation_plates,
    refresh_pending_day,
)
from fuel_enriched_consolidated_report import generate_multi_roster_report

TZ = ZoneInfo("Europe/Istanbul")
ADVISORY_LOCK_KEY = 2026072901


def database_url() -> str:
    value = os.environ.get("DATABASE_URL", "").strip()
    if not value:
        raise ValueError("DATABASE_URL не задан")
    return value


def requested_days(start_day: date, end_day: date) -> list[date]:
    return [
        start_day + timedelta(days=offset)
        for offset in range((end_day - start_day).days + 1)
    ]


def refresh(start_day: date, end_day: date, trigger_name: str) -> dict:
    if end_day < start_day:
        raise ValueError("Дата окончания раньше даты начала")
    days = requested_days(start_day, end_day)
    if len(days) > 31:
        raise ValueError("За один запуск можно обновить не более 31 дня")

    url = database_url()
    with psycopg.connect(url) as lock_connection:
        ensure_schema(lock_connection)
        # Commit schema creation before the worker opens other DB connections.
        lock_connection.commit()
        with lock_connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
            locked = bool(cursor.fetchone()[0])
        if not locked:
            print("SKIPPED: другой процесс обновления сводной истории уже работает", flush=True)
            return {"status": "SKIPPED"}

        # The lock is session-scoped and survives COMMIT. Closing this transaction
        # avoids an hours-long ``idle in transaction`` backend during calculations.
        lock_connection.commit()

        try:
            with tempfile.TemporaryDirectory(prefix="arvento_cache_refresh_") as temp_name:
                temp_dir = Path(temp_name)
                roster_paths = export_stored_rosters(url, temp_dir)
                total_calculated = 0
                total_cached = 0
                total_fuel_liters = 0.0
                total_queue_completed = 0
                run_ids: list[int] = []

                for position, report_day in enumerate(days, start=1):
                    output_path = temp_dir / f"cache_{report_day.isoformat()}.xlsx"
                    print(
                        {
                            "status": "START_DAY",
                            "day": report_day.isoformat(),
                            "position": position,
                            "total_days": len(days),
                        },
                        flush=True,
                    )
                    stats = generate_multi_roster_report(
                        start_day=report_day,
                        end_day=report_day,
                        roster_paths=roster_paths,
                        output_path=output_path,
                        database_url=url,
                    )
                    cache_stats = upsert_cache_from_workbook(
                        url,
                        output_path,
                        report_day,
                        report_day,
                        trigger_name=trigger_name,
                    )
                    queue_completed = complete_recalculation_queue(url, report_day)
                    calculated_rows = int(stats.get("rows", 0))
                    cached_rows = int(cache_stats.get("rows", 0))
                    fuel_liters = float(stats.get("fuel_liters", 0) or 0)
                    run_id = cache_stats.get("run_id")
                    if run_id is not None:
                        run_ids.append(int(run_id))
                    total_calculated += calculated_rows
                    total_cached += cached_rows
                    total_fuel_liters += fuel_liters
                    total_queue_completed += queue_completed
                    print(
                        {
                            "status": "DONE_DAY",
                            "day": report_day.isoformat(),
                            "calculated_rows": calculated_rows,
                            "cached_rows": cached_rows,
                            "cache_run_id": run_id,
                            "queue_completed": queue_completed,
                            "fuel_liters": fuel_liters,
                        },
                        flush=True,
                    )
                    output_path.unlink(missing_ok=True)

                result = {
                    "status": "SUCCESS",
                    "period": f"{start_day.isoformat()}..{end_day.isoformat()}",
                    "days_completed": len(days),
                    "calculated_rows": total_calculated,
                    "cached_rows": total_cached,
                    "cache_run_id": run_ids[-1] if run_ids else None,
                    "cache_run_ids": run_ids,
                    "queue_completed": total_queue_completed,
                    "fuel_liters": round(total_fuel_liters, 1),
                }
                print(result, flush=True)
                return result
        finally:
            with lock_connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
            lock_connection.commit()


def refresh_pending(report_day: date, trigger_name: str) -> dict:
    """Recalculate only vehicles queued by the latest intraday GPS sync."""
    url = database_url()
    with psycopg.connect(url) as lock_connection:
        ensure_schema(lock_connection)
        lock_connection.commit()
        with lock_connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
            locked = bool(cursor.fetchone()[0])
        if not locked:
            print("SKIPPED: другой процесс обновления сводной истории уже работает", flush=True)
            return {"status": "SKIPPED"}
        lock_connection.commit()

        try:
            plates = pending_recalculation_plates(url, report_day)
            if not plates:
                result = {
                    "status": "SKIPPED",
                    "day": report_day.isoformat(),
                    "reason": "нет новых GPS-точек в очереди пересчёта",
                    "requested_plates": 0,
                }
                print(result, flush=True)
                return result

            print(
                {
                    "status": "START_PENDING",
                    "day": report_day.isoformat(),
                    "requested_plates": len(plates),
                },
                flush=True,
            )
            with tempfile.TemporaryDirectory(prefix="arvento_cache_pending_") as temp_name:
                roster_paths = export_stored_rosters(url, Path(temp_name))
                result = refresh_pending_day(
                    url,
                    report_day,
                    plates,
                    roster_paths,
                    trigger_name=trigger_name,
                )
            print(result, flush=True)
            return result
        finally:
            with lock_connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
            lock_connection.commit()


def parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Дата должна быть в формате YYYY-MM-DD") from exc


def show_status(limit: int) -> None:
    days, runs = recent_status(database_url(), limit=limit)
    print("=== CACHE DAYS ===")
    for item in days:
        print(
            item["report_day"],
            item["status"],
            f"rows={item['row_count']}",
            f"gps_max={item['gps_max_event_time']}",
            f"refreshed={item['refreshed_at']}",
        )
    print("=== CACHE RUNS ===")
    for item in runs:
        print(
            f"id={item['id']}",
            f"period={item['period_start']}..{item['period_end']}",
            f"trigger={item['trigger_name']}",
            f"status={item['status']}",
            f"rows={item['rows_written']}",
            f"started={item['started_at']}",
            f"finished={item['finished_at']}",
            f"error={item['error_message'] or ''}",
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Обновление базы готовых строк сводного отчёта")
    subparsers = parser.add_subparsers(dest="command", required=True)

    refresh_parser = subparsers.add_parser("refresh")
    refresh_parser.add_argument("--date", type=parse_day)
    refresh_parser.add_argument("--date-from", type=parse_day)
    refresh_parser.add_argument("--date-to", type=parse_day)
    refresh_parser.add_argument("--days-back", type=int, default=1)
    refresh_parser.add_argument("--trigger", default="manual")

    pending_parser = subparsers.add_parser("refresh-pending")
    pending_parser.add_argument("--date", type=parse_day, required=True)
    pending_parser.add_argument("--trigger", default="intraday")

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--limit", type=int, default=10)

    args = parser.parse_args()
    if args.command == "status":
        show_status(args.limit)
        return
    if args.command == "refresh-pending":
        refresh_pending(args.date, args.trigger)
        return

    today = datetime.now(TZ).date()
    if args.date:
        start_day = end_day = args.date
    elif args.date_from:
        start_day = args.date_from
        end_day = args.date_to or start_day
    else:
        if args.days_back < 0 or args.days_back > 30:
            raise ValueError("--days-back должен быть от 0 до 30")
        start_day = today - timedelta(days=args.days_back)
        end_day = today

    refresh(start_day, end_day, args.trigger)


if __name__ == "__main__":
    main()
