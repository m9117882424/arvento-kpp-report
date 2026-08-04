#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Incremental refresh of consolidated daily cache rows.

Intraday GPS synchronization writes affected ``(normalized_plate, day)`` pairs
into ``recalculation_queue``. This module recalculates only those vehicles, but
always uses their complete track from 00:00 to the current point of the day.
That preserves daily mileage, entry/exit and work-time semantics without a full
all-vehicle refresh every 30 minutes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterator, Sequence
from zoneinfo import ZoneInfo

import psycopg

from arvento_io import Point
from consolidated_cache import ensure_schema
from fuel_enriched_consolidated_report import load_fuel_totals
import consolidated_report as core
from consolidated_multi_report import has_night_site_mileage, load_rosters, select_roster
from roster_registry import normalize_plate

TZ = ZoneInfo("Europe/Istanbul")


@dataclass(frozen=True, slots=True)
class IncrementalCacheRow:
    report: core.ReportRow
    roster_day: date
    roster_filename: str
    fuel_liters: float


def _normalized_plate_list(values: Sequence[str]) -> list[str]:
    return sorted({normalize_plate(value) for value in values if normalize_plate(value)})


def pending_recalculation_plates(database_url: str, report_day: date) -> list[str]:
    """Return unprocessed queue entries for one calendar day."""
    with psycopg.connect(database_url) as connection:
        ensure_schema(connection)
        connection.commit()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT normalized_plate
                FROM recalculation_queue
                WHERE day=%s
                  AND completed_at IS NULL
                ORDER BY normalized_plate
                """,
                (report_day,),
            )
            return _normalized_plate_list([row[0] for row in cursor.fetchall()])


def complete_recalculation_queue(
    database_url: str,
    report_day: date,
    normalized_plates: Sequence[str] | None = None,
) -> int:
    """Mark queue entries complete after an authoritative cache refresh."""
    plates = _normalized_plate_list(normalized_plates or [])
    with psycopg.connect(database_url) as connection:
        ensure_schema(connection)
        connection.commit()
        with connection.cursor() as cursor:
            if normalized_plates is None:
                cursor.execute(
                    """
                    UPDATE recalculation_queue
                    SET completed_at=now(), locked_at=NULL
                    WHERE day=%s
                      AND completed_at IS NULL
                    """,
                    (report_day,),
                )
            elif plates:
                cursor.execute(
                    """
                    UPDATE recalculation_queue
                    SET completed_at=now(), locked_at=NULL
                    WHERE day=%s
                      AND normalized_plate = ANY(%s::text[])
                      AND completed_at IS NULL
                    """,
                    (report_day, plates),
                )
            else:
                return 0
            updated = cursor.rowcount
        connection.commit()
    return int(updated)


def iter_database_tracks(
    database_url: str,
    report_day: date,
    normalized_plates: Sequence[str],
) -> Iterator[tuple[str, list[Point]]]:
    """Yield full-day tracks only for requested normalized plates."""
    plates = _normalized_plate_list(normalized_plates)
    if not plates:
        return

    start_at = datetime.combine(report_day, time.min, tzinfo=TZ)
    finish_at = start_at + timedelta(days=1)
    query = """
        SELECT
            normalized_plate,
            COALESCE(NULLIF(plate, ''), normalized_plate) AS display_plate,
            event_time AT TIME ZONE 'Europe/Istanbul' AS local_time,
            latitude,
            longitude,
            speed_kmh,
            distance_km,
            COALESCE(address, '')
        FROM gps_points
        WHERE event_time >= %s
          AND event_time < %s
          AND normalized_plate = ANY(%s::text[])
        ORDER BY normalized_plate, event_time
    """

    with psycopg.connect(database_url) as connection:
        with connection.cursor(name="incremental_consolidated_points") as cursor:
            cursor.itersize = 20_000
            cursor.execute(query, (start_at, finish_at, plates))
            current_plate: str | None = None
            display_plate = ""
            points: list[Point] = []
            for row in cursor:
                normalized, row_plate, local_time, lat, lon, speed, distance, address = row
                if current_plate is not None and normalized != current_plate:
                    yield display_plate, points
                    points = []
                current_plate = normalized
                display_plate = str(row_plate or normalized)
                points.append(
                    Point(
                        plate=display_plate,
                        time=local_time,
                        lat=float(lat),
                        lon=float(lon),
                        speed=float(speed) if speed is not None else None,
                        source_distance=float(distance) if distance is not None else None,
                        address=str(address or ""),
                    )
                )
            if current_plate is not None and points:
                yield display_plate, points


def calculate_incremental_rows(
    database_url: str,
    report_day: date,
    normalized_plates: Sequence[str],
    roster_paths: Sequence[Path],
) -> list[IncrementalCacheRow]:
    """Calculate authoritative daily rows for a subset of vehicles."""
    plates = _normalized_plate_list(normalized_plates)
    if not plates:
        return []

    rosters = load_rosters(roster_paths)
    registry = core.load_registry(core.DEFAULT_GEOZONES)
    site_zone = core.find_site_boundary(registry)
    site_polygon = list(site_zone.points or [])
    route_polygon = core.load_kml_polygon(core.DEFAULT_ROUTE_KML)

    fuel_database_url = os.environ.get("FUEL_DATABASE_URL", "").strip()
    fuel_totals = load_fuel_totals(fuel_database_url, report_day, report_day)

    rows: list[IncrementalCacheRow] = []
    for display_plate, points in iter_database_tracks(database_url, report_day, plates):
        roster = select_roster(rosters, report_day)
        item = core.analyze_track(
            report_day,
            display_plate,
            points,
            roster.vehicles,
            site_polygon,
            route_polygon,
        )
        if item is None:
            continue
        item = replace(
            item,
            night_work=int(has_night_site_mileage(report_day, points, site_polygon)),
        )
        normalized = normalize_plate(display_plate)
        rows.append(
            IncrementalCacheRow(
                report=item,
                roster_day=roster.day,
                roster_filename=roster.path.name,
                fuel_liters=round(float(fuel_totals.get((report_day, normalized), 0.0)), 1),
            )
        )
    return rows


_INSERT_SQL = """
    INSERT INTO consolidated_report_cache(
        report_day, normalized_plate, company, plate, user_name, grade,
        max_speed, route_max_speed, site_max_speed,
        total_km, inside_km, outside_km, distance_difference_km,
        inside_percent, outside_percent, percent_difference,
        departure, arrival, weekday, entry_time, exit_time, worked_hours,
        boundary_violation, personal_use, weekend_work, night_work,
        fuel_liters, in_roster, raw_points, retained_points,
        valid_speed_points, max_distance_from_site_km,
        roster_day, roster_filename, refresh_run_id, computed_at
    ) VALUES (
        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now()
    )
    ON CONFLICT (report_day, normalized_plate) DO UPDATE SET
        company=EXCLUDED.company,
        plate=EXCLUDED.plate,
        user_name=EXCLUDED.user_name,
        grade=EXCLUDED.grade,
        max_speed=EXCLUDED.max_speed,
        route_max_speed=EXCLUDED.route_max_speed,
        site_max_speed=EXCLUDED.site_max_speed,
        total_km=EXCLUDED.total_km,
        inside_km=EXCLUDED.inside_km,
        outside_km=EXCLUDED.outside_km,
        distance_difference_km=EXCLUDED.distance_difference_km,
        inside_percent=EXCLUDED.inside_percent,
        outside_percent=EXCLUDED.outside_percent,
        percent_difference=EXCLUDED.percent_difference,
        departure=EXCLUDED.departure,
        arrival=EXCLUDED.arrival,
        weekday=EXCLUDED.weekday,
        entry_time=EXCLUDED.entry_time,
        exit_time=EXCLUDED.exit_time,
        worked_hours=EXCLUDED.worked_hours,
        boundary_violation=EXCLUDED.boundary_violation,
        personal_use=EXCLUDED.personal_use,
        weekend_work=EXCLUDED.weekend_work,
        night_work=EXCLUDED.night_work,
        fuel_liters=EXCLUDED.fuel_liters,
        in_roster=EXCLUDED.in_roster,
        raw_points=EXCLUDED.raw_points,
        retained_points=EXCLUDED.retained_points,
        valid_speed_points=EXCLUDED.valid_speed_points,
        max_distance_from_site_km=EXCLUDED.max_distance_from_site_km,
        roster_day=EXCLUDED.roster_day,
        roster_filename=EXCLUDED.roster_filename,
        refresh_run_id=EXCLUDED.refresh_run_id,
        computed_at=now()
"""


def _record_values(item: IncrementalCacheRow) -> tuple[object, ...]:
    row = item.report
    return (
        row.day,
        normalize_plate(row.plate),
        row.company,
        row.plate,
        row.user,
        row.grade,
        row.max_speed,
        row.route_max_speed,
        row.site_max_speed,
        row.total_km,
        row.inside_km,
        row.outside_km,
        row.distance_difference_km,
        row.inside_percent,
        row.outside_percent,
        row.percent_difference,
        row.departure,
        row.arrival,
        row.weekday,
        row.entry_time,
        row.exit_time,
        row.worked_hours,
        row.boundary_violation,
        row.personal_use,
        row.weekend_work,
        row.night_work,
        item.fuel_liters,
        row.in_roster,
        row.raw_points,
        row.retained_points,
        row.valid_speed_points,
        row.max_distance_from_site_km,
        item.roster_day,
        item.roster_filename,
    )


def upsert_incremental_rows(
    database_url: str,
    report_day: date,
    normalized_plates: Sequence[str],
    rows: Sequence[IncrementalCacheRow],
    *,
    trigger_name: str,
) -> dict[str, object]:
    """Atomically replace only affected vehicle rows for one calendar day."""
    plates = _normalized_plate_list(normalized_plates)
    if not plates:
        return {
            "status": "SKIPPED",
            "day": report_day.isoformat(),
            "requested_plates": 0,
            "calculated_rows": 0,
            "cached_rows": 0,
            "queue_completed": 0,
        }

    run_id: int | None = None
    try:
        with psycopg.connect(database_url) as connection:
            ensure_schema(connection)
            connection.commit()
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO consolidated_cache_runs(period_start, period_end, trigger_name)
                    VALUES (%s,%s,%s)
                    RETURNING id
                    """,
                    (report_day, report_day, trigger_name),
                )
                run_id = int(cursor.fetchone()[0])
            connection.commit()

            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM consolidated_report_cache
                    WHERE report_day=%s
                      AND normalized_plate = ANY(%s::text[])
                    """,
                    (report_day, plates),
                )
                for item in rows:
                    cursor.execute(_INSERT_SQL, (*_record_values(item), run_id))

                cursor.execute(
                    """
                    UPDATE recalculation_queue
                    SET completed_at=now(), locked_at=NULL
                    WHERE day=%s
                      AND normalized_plate = ANY(%s::text[])
                      AND completed_at IS NULL
                    """,
                    (report_day, plates),
                )
                queue_completed = int(cursor.rowcount)

                cursor.execute(
                    "SELECT COUNT(*) FROM consolidated_report_cache WHERE report_day=%s",
                    (report_day,),
                )
                day_row_count = int(cursor.fetchone()[0])

                start_at = datetime.combine(report_day, time.min, tzinfo=TZ)
                finish_at = start_at + timedelta(days=1)
                cursor.execute(
                    "SELECT MAX(event_time) FROM gps_points WHERE event_time >= %s AND event_time < %s",
                    (start_at, finish_at),
                )
                gps_max = cursor.fetchone()[0]

                cursor.execute(
                    """
                    INSERT INTO consolidated_cache_days(
                        report_day, status, row_count, gps_max_event_time,
                        refresh_run_id, refreshed_at
                    ) VALUES (%s,'SUCCESS',%s,%s,%s,now())
                    ON CONFLICT (report_day) DO UPDATE SET
                        status='SUCCESS',
                        row_count=EXCLUDED.row_count,
                        gps_max_event_time=EXCLUDED.gps_max_event_time,
                        refresh_run_id=EXCLUDED.refresh_run_id,
                        refreshed_at=now()
                    """,
                    (report_day, day_row_count, gps_max, run_id),
                )
                cursor.execute(
                    """
                    UPDATE consolidated_cache_runs
                    SET status='SUCCESS', rows_written=%s, finished_at=now()
                    WHERE id=%s
                    """,
                    (len(rows), run_id),
                )
            connection.commit()
    except Exception as exc:
        if run_id is not None:
            try:
                with psycopg.connect(database_url) as connection:
                    ensure_schema(connection)
                    connection.commit()
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """
                            UPDATE consolidated_cache_runs
                            SET status='FAILED', finished_at=now(), error_message=%s
                            WHERE id=%s
                            """,
                            (str(exc)[:4000], run_id),
                        )
                    connection.commit()
            except Exception:
                pass
        raise

    return {
        "status": "SUCCESS",
        "day": report_day.isoformat(),
        "requested_plates": len(plates),
        "calculated_rows": len(rows),
        "cached_rows": len(rows),
        "day_row_count": day_row_count,
        "queue_completed": queue_completed,
        "cache_run_id": run_id,
        "fuel_liters": round(sum(item.fuel_liters for item in rows), 1),
    }


def refresh_pending_day(
    database_url: str,
    report_day: date,
    normalized_plates: Sequence[str],
    roster_paths: Sequence[Path],
    *,
    trigger_name: str,
) -> dict[str, object]:
    rows = calculate_incremental_rows(
        database_url,
        report_day,
        normalized_plates,
        roster_paths,
    )
    return upsert_incremental_rows(
        database_url,
        report_day,
        normalized_plates,
        rows,
        trigger_name=trigger_name,
    )


__all__ = [
    "IncrementalCacheRow",
    "calculate_incremental_rows",
    "complete_recalculation_queue",
    "iter_database_tracks",
    "pending_recalculation_plates",
    "refresh_pending_day",
    "upsert_incremental_rows",
]
