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

from consolidated_cache import (
    ensure_schema,
    export_stored_rosters,
    recent_status,
    upsert_cache_from_workbook,
)
from fuel_enriched_consolidated_report import generate_multi_roster_report

TZ = ZoneInfo("Europe/Istanbul")
ADVISORY_LOCK_KEY = 2026072901


def database_url() -> str:
    value = os.environ.get("DATABASE_URL", "").strip()
    if not value:
        raise ValueError("DATABASE_URL не задан")
    return value


def refresh(start_day: date, end_day: date, trigger_name: str) -> dict:
    if end_day < start_day:
        raise ValueError("Дата окончания раньше даты начала")
    if (end_day - start_day).days + 1 > 31:
        raise ValueError("За один запуск можно обновить не более 31 дня")

    url = database_url()
    with psycopg.connect(url) as lock_connection:
        ensure_schema(lock_connection)
        with lock_connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
            locked = bool(cursor.fetchone()[0])
        if not locked:
            print("SKIPPED: другой процесс обновления сводной истории уже работает", flush=True)
            return {"status": "SKIPPED"}

        try:
            with tempfile.TemporaryDirectory(prefix="arvento_cache_refresh_") as temp_name:
                temp_dir = Path(temp_name)
                roster_paths = export_stored_rosters(url, temp_dir)
                output_path = temp_dir / f"cache_{start_day.isoformat()}_{end_day.isoformat()}.xlsx"
                stats = generate_multi_roster_report(
                    start_day=start_day,
                    end_day=end_day,
                    roster_paths=roster_paths,
                    output_path=output_path,
                    database_url=url,
                )
                cache_stats = upsert_cache_from_workbook(
                    url,
                    output_path,
                    start_day,
                    end_day,
                    trigger_name=trigger_name,
                )
                result = {
                    "status": "SUCCESS",
                    "period": f"{start_day.isoformat()}..{end_day.isoformat()}",
                    "calculated_rows": stats.get("rows", 0),
                    "cached_rows": cache_stats.get("rows", 0),
                    "cache_run_id": cache_stats.get("run_id"),
                    "fuel_liters": stats.get("fuel_liters", 0),
                }
                print(result, flush=True)
                return result
        finally:
            with lock_connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))


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

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--limit", type=int, default=10)

    args = parser.parse_args()
    if args.command == "status":
        show_status(args.limit)
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
