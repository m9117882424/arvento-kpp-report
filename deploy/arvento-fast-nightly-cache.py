#!/usr/bin/env python3
"""Fast nightly cache finalization: full roster including cars without GPS.

Uses the application's proven incremental row computation and atomic upsert.
Run only as isolated Arvento report-portal Compose job under pipeline flock.
"""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from datetime import date, datetime, time as clocktime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import consolidated_cache_worker as worker
from consolidated_cache import export_stored_rosters
from consolidated_incremental_cache import (
    IncrementalCacheRow,
    calculate_incremental_rows,
    upsert_incremental_rows,
)
from consolidated_multi_report import (
    empty_vehicle_row,
    load_rosters,
    select_roster,
)
from fuel_enriched_consolidated_report import load_fuel_totals
from mileage_review_policy import refresh_mileage_review_candidates
from roster_registry import normalize_plate

ZONE = ZoneInfo("Europe/Istanbul")


def desired(database_url: str, day: date):
    start = datetime.combine(day, clocktime.min, tzinfo=ZONE)
    end = start + timedelta(days=1)
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as c:
            c.execute(
                """SELECT DISTINCT normalized_plate FROM gps_points
                   WHERE event_time >= %s AND event_time < %s
                     AND normalized_plate IS NOT NULL
                     AND normalized_plate <> ''""",
                (start, end),
            )
            gps = {normalize_plate(p) for (p,) in c.fetchall()}
            c.execute(
                """SELECT roster_day
                   FROM consolidated_roster_snapshots
                   WHERE roster_day <= %s
                   ORDER BY roster_day DESC LIMIT 1""",
                (day,),
            )
            chosen = c.fetchone()
            if chosen is None:
                c.execute(
                    "SELECT MIN(roster_day) FROM consolidated_roster_snapshots"
                )
                chosen = c.fetchone()
            if not chosen or not chosen[0]:
                raise RuntimeError("no stored roster to finalize the cache")
            roster_day = chosen[0]
            c.execute(
                """SELECT DISTINCT normalized_plate FROM consolidated_roster_entries
                   WHERE roster_day = %s""",
                (roster_day,),
            )
            roster = {normalize_plate(p) for (p,) in c.fetchall()}
            c.execute(
                """SELECT normalized_plate FROM consolidated_report_cache
                   WHERE report_day = %s""",
                (day,),
            )
            cache = {normalize_plate(p) for (p,) in c.fetchall()}
            c.execute(
                """SELECT COUNT(*) FROM recalculation_queue
                   WHERE day=%s AND completed_at IS NULL""",
                (day,),
            )
            pending = int(c.fetchone()[0])
            c.execute(
                """SELECT COUNT(*) FROM gps_points
                   WHERE event_time >= %s AND event_time < %s""",
                (start, end),
            )
            points = int(c.fetchone()[0])
    return dict(
        gps=gps,
        roster=roster,
        cache=cache,
        roster_day=roster_day,
        pending=pending,
        gps_points=points,
    )


def summarized(day: date, status: str, coverage: dict, **extra):
    print(json.dumps(
        {
            "status": status,
            "day": day.isoformat(),
            "gps_points": coverage["gps_points"],
            "gps_vehicles": len(coverage["gps"]),
            "roster_vehicles": len(coverage["roster"]),
            "cached_before": len(coverage["cache"]),
            "cached_missing_roster": len(
                coverage["roster"] - coverage["cache"]
            ),
            "pending": coverage["pending"],
            "effective_roster_date": coverage["roster_day"].isoformat(),
            **extra,
        },
        ensure_ascii=False,
    ), flush=True)


def finalize(day: date, trigger: str):
    url = worker.database_url()
    with psycopg.connect(url) as owner:
        worker.ensure_schema(owner)
        owner.commit()
        with owner.cursor() as c:
            c.execute(
                "SELECT pg_try_advisory_lock(%s)",
                (worker.ADVISORY_LOCK_KEY,),
            )
            locked = bool(c.fetchone()[0])
        owner.commit()
        if not locked:
            raise RuntimeError("cache advisory lock held by another worker")
        try:
            before = desired(url, day)
            summarized(day, "START_FAST_DAY", before)
            started = time.monotonic()
            with tempfile.TemporaryDirectory(prefix="arvento_fast_cache_") as td:
                roster_paths = export_stored_rosters(url, Path(td))
                rosters = load_rosters(roster_paths)
                selected = select_roster(rosters, day)
                roster_by_key = {
                    normalize_plate(info.plate): info
                    for info in selected.vehicles.values()
                }
                if set(roster_by_key) != before["roster"]:
                    raise RuntimeError("effective roster changed since preflight")
                requested = sorted(before["gps"] | before["roster"])
                rows = calculate_incremental_rows(
                    url, day, requested, roster_paths
                )
                computed = {
                    normalize_plate(r.report.plate) for r in rows
                }
                if len(rows) != len(computed):
                    raise RuntimeError("duplicate normalized plates in computation")
                # The base full export includes every listed car, including
                # cars without any usable GPS track.
                fuel_totals = load_fuel_totals(
                    __import__("os").environ.get("FUEL_DATABASE_URL", ""),
                    day, day,
                )
                placeholders = 0
                for key, info in roster_by_key.items():
                    if key in computed:
                        continue
                    rows.append(
                        IncrementalCacheRow(
                            report=empty_vehicle_row(day, info),
                            roster_day=selected.day,
                            roster_filename=selected.path.name,
                            fuel_liters=round(
                                float(fuel_totals.get((day, key), 0.0)), 1
                            ),
                        )
                    )
                    placeholders += 1
                expected = before["roster"] | computed
                actual = {normalize_plate(r.report.plate) for r in rows}
                if actual != expected or len(rows) != len(actual):
                    raise RuntimeError("computation omitted or duplicated vehicles")
                # Fail before any mutation if GPS/roster inputs have changed;
                # scheduled pipeline's outer flock also prevents overlapping sync.
                right_before_commit = desired(url, day)
                if (
                    right_before_commit["gps"] != before["gps"]
                    or right_before_commit["gps_points"] != before["gps_points"]
                    or right_before_commit["roster"] != before["roster"]
                    or right_before_commit["roster_day"] != before["roster_day"]
                ):
                    raise RuntimeError("source changed during nightly cache calculation")
                result = upsert_incremental_rows(
                    url, day, requested, rows, trigger_name=trigger
                )
                if result.get("status") != "SUCCESS":
                    raise RuntimeError("cache upsert did not succeed")
                if int(result.get("cached_rows", -1)) != len(rows):
                    raise RuntimeError("cached_rows mismatch after transaction")
                after = desired(url, day)
                expected_cache = expected
                if after["cache"] != expected_cache:
                    raise RuntimeError(
                        "cache contains stale or missing vehicle keys"
                    )
                candidates = refresh_mileage_review_candidates(
                    url, day, day
                )
                summarized(
                    day,
                    "SUCCESS",
                    after,
                    calculated_rows=len(rows),
                    cache_vehicles=len(after["cache"]),
                    no_gps_roster_rows=placeholders,
                    queue_completed=result.get("queue_completed"),
                    cache_run_id=result.get("cache_run_id"),
                    mileage_review_candidates=len(candidates),
                    duration_sec=round(time.monotonic()-started, 1),
                )
        finally:
            with owner.cursor() as c:
                c.execute(
                    "SELECT pg_advisory_unlock(%s)",
                    (worker.ADVISORY_LOCK_KEY,),
                )
            owner.commit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, required=True)
    parser.add_argument("--trigger", default="nightly-fast-roster")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    today = datetime.now(ZONE).date()
    if args.date > today or (today - args.date).days > 31:
        raise ValueError("date outside allowed window")
    if args.check_only:
        state = desired(worker.database_url(), args.date)
        summarized(args.date, "READ_ONLY", state)
    else:
        finalize(args.date, args.trigger)


if __name__ == "__main__":
    main()
