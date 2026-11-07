#!/usr/bin/env python3
"""Conservatively warm verified closed-day efficiency metrics in PostgreSQL.

Run after the established nightly Arvento sync/cache pipeline. The worker does
not refresh GPS or consolidated mileage itself and skips incomplete days.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg

from cache_freshness import load_cache_coverage
from efficiency_metric_cache import persistent_cache_enabled
from efficiency_report import EfficiencySettings, generate_efficiency_report


TZ = ZoneInfo("Europe/Istanbul")


def main() -> int:
    parser = argparse.ArgumentParser(description="Прогрев суточного GPS-кэша эффективности")
    parser.add_argument("--date", type=date.fromisoformat, default=None,
                        help="Конечные сутки YYYY-MM-DD, по умолчанию вчера")
    parser.add_argument("--days", type=int, default=1,
                        help="Число закрытых суток для прогрева, 1..7")
    args = parser.parse_args()
    if not 1 <= args.days <= 7:
        parser.error("--days должен быть от 1 до 7")
    today = datetime.now(TZ).date()
    last_day = args.date or (today - timedelta(days=1))
    if last_day >= today:
        parser.error("Прогрев разрешён только для завершённых суток")
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        parser.error("DATABASE_URL не задан")
    if not persistent_cache_enabled():
        parser.error("Постоянный GPS-кэш отключён настройкой")

    failures = 0
    for offset in reversed(range(args.days)):
        day = last_day - timedelta(days=offset)
        with psycopg.connect(url, connect_timeout=10, options="-c statement_timeout=120000") as connection:
            coverage = load_cache_coverage(connection, day, day)
        if not coverage.complete:
            failures += 1
            print(json.dumps({
                "day": day.isoformat(), "status": "SKIPPED_STALE_CONSOLIDATED_CACHE",
                "reasons": [list(reasons) for _, reasons in coverage.stale_reasons],
            }, ensure_ascii=False), flush=True)
            continue

        report = generate_efficiency_report(url, EfficiencySettings(date_from=day, date_to=day))
        freshness = report["coverage"]
        if (freshness["incomplete_days"] or freshness["provisional_days"]
                or not freshness["gps_metric_persisted_available"]):
            failures += 1
            status = "INCOMPLETE_WARMUP"
        else:
            status = "WARMED"
        print(json.dumps({
            "day": day.isoformat(), "status": status,
            "vehicles": report["summary"]["vehicles"],
            "memory_hits": freshness["gps_metric_cache_hits"] - freshness["gps_metric_persisted_hits"],
            "persisted_hits": freshness["gps_metric_persisted_hits"],
            "persisted_writes": freshness["gps_metric_persisted_writes"],
            "gps_misses": freshness["gps_metric_cache_misses"],
        }, ensure_ascii=False), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
