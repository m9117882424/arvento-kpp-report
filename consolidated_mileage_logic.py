#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mileage corrections for the consolidated report.

The Arvento general report contains both interval distance and a cumulative
odometer. The consolidated report uses both sources, suppresses stationary GPS
jitter, and prevents positive odometer jumps from exceeding calculated mileage.
"""
from __future__ import annotations

import math
import os
import time as monotonic_time
from datetime import date, datetime, time, timedelta
from typing import Iterator, Sequence

import consolidated_report as core
from arvento_io import Point

GPS_JITTER_RADIUS_M = max(
    0.0,
    float(os.environ.get("CONSOLIDATED_GPS_JITTER_RADIUS_M", "50")),
)
GPS_JITTER_MAX_DISTANCE_M = max(
    GPS_JITTER_RADIUS_M,
    float(os.environ.get("CONSOLIDATED_GPS_JITTER_MAX_DISTANCE_M", "100")),
)
GPS_JITTER_MAX_SPEED_KMH = max(
    0.0,
    float(os.environ.get("CONSOLIDATED_GPS_JITTER_MAX_SPEED_KMH", "3")),
)
MAX_SEGMENT_DISTANCE_KM = max(
    0.1,
    float(os.environ.get("CONSOLIDATED_MAX_SEGMENT_DISTANCE_KM", "20")),
)
MAX_REASONABLE_COORDINATE_SPEED_KMH = max(
    1.0,
    float(os.environ.get("CONSOLIDATED_MAX_COORDINATE_SPEED_KMH", "180")),
)

_PATCHED = False


def _valid_nonnegative(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _valid_odometer_delta(p1: Point, p2: Point) -> float | None:
    first = _valid_nonnegative(p1.odometer)
    second = _valid_nonnegative(p2.odometer)
    if first is None or second is None:
        return None
    delta = second - first
    if delta < 0 or delta > MAX_SEGMENT_DISTANCE_KM:
        return None
    return delta


def _valid_source_distance(point: Point) -> float | None:
    value = _valid_nonnegative(point.source_distance)
    if value is None or value > MAX_SEGMENT_DISTANCE_KM:
        return None
    return value


def _reported_speed(points: Sequence[Point]) -> float | None:
    values: list[float] = []
    for point in points:
        value = _valid_nonnegative(point.speed)
        if value is not None:
            values.append(value)
    return max(values) if values else None


def _coordinate_distance(p1: Point, p2: Point, gap_seconds: float) -> tuple[float, float]:
    distance = core.haversine_km_coords(p1.lat, p1.lon, p2.lat, p2.lon)
    if not math.isfinite(distance) or distance < 0 or gap_seconds <= 0:
        return 0.0, math.inf
    implied_speed = distance / (gap_seconds / 3600.0)
    if implied_speed > MAX_REASONABLE_COORDINATE_SPEED_KMH:
        return 0.0, implied_speed
    return distance, implied_speed


def segment_distance_without_stationary_jitter(p1: Point, p2: Point) -> float:
    """Return one segment distance using bounded odometer and jitter filtering.

    Selection order:
    * suppress a short stationary segment as coordinate jitter;
    * use Arvento interval distance as the calculated value when present;
    * otherwise use reasonable coordinate distance;
    * if a positive odometer delta is available, it may reduce but never
      increase the calculated value;
    * a zero/stale odometer delta does not erase confirmed movement.
    """
    gap_seconds = (p2.time - p1.time).total_seconds()
    if gap_seconds <= 0:
        return 0.0

    coordinate_km, implied_speed = _coordinate_distance(p1, p2, gap_seconds)
    source_km = _valid_source_distance(p2)
    calculated_km = (
        source_km
        if source_km is not None and source_km > 0
        else coordinate_km
    )

    reported_speed = _reported_speed((p1, p2))
    motion_speed = reported_speed if reported_speed is not None else implied_speed
    is_stationary_jitter = (
        coordinate_km * 1000.0 <= GPS_JITTER_RADIUS_M
        and calculated_km * 1000.0 <= GPS_JITTER_MAX_DISTANCE_M
        and motion_speed <= GPS_JITTER_MAX_SPEED_KMH
    )
    if is_stationary_jitter:
        return 0.0

    odometer_delta = _valid_odometer_delta(p1, p2)
    if odometer_delta is not None and odometer_delta > 0:
        if calculated_km > 0:
            return min(odometer_delta, calculated_km)
        return odometer_delta

    return calculated_km if math.isfinite(calculated_km) and calculated_km > 0 else 0.0


def iter_database_tracks_with_odometer(
    database_url: str,
    start_day: date,
    end_day: date,
) -> Iterator[tuple[date, str, list[Point]]]:
    """Stream PostgreSQL tracks including cumulative Arvento odometer values."""
    try:
        import psycopg
    except ModuleNotFoundError as exc:
        raise RuntimeError("Для чтения PostgreSQL требуется пакет psycopg") from exc

    start = datetime.combine(start_day, time.min, tzinfo=core.TZ)
    finish = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=core.TZ)
    fetch_size = max(1_000, int(os.environ.get("CONSOLIDATED_DB_FETCH_SIZE", "50000")))
    progress_every = max(1, int(os.environ.get("CONSOLIDATED_PROGRESS_EVERY", "25")))
    started = monotonic_time.monotonic()
    processed = 0

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as schema_cursor:
            schema_cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'gps_points'
                      AND column_name = 'odometer_km'
                )
                """
            )
            has_odometer = bool(schema_cursor.fetchone()[0])

        odometer_expression = "odometer_km" if has_odometer else "NULL::double precision"
        query = f"""
            SELECT
                (event_time AT TIME ZONE 'Europe/Istanbul')::date AS local_day,
                normalized_plate,
                COALESCE(NULLIF(plate, ''), normalized_plate) AS display_plate,
                event_time AT TIME ZONE 'Europe/Istanbul' AS local_time,
                latitude,
                longitude,
                speed_kmh,
                distance_km,
                {odometer_expression} AS odometer_km,
                COALESCE(address, '')
            FROM gps_points
            WHERE event_time >= %s AND event_time < %s
            ORDER BY local_day, normalized_plate, event_time
        """

        def log_progress(day_value: date, *, final: bool = False) -> None:
            elapsed = monotonic_time.monotonic() - started
            marker = "DONE" if final else "PROGRESS"
            print(
                f"CONSOLIDATED_{marker}: day={day_value} vehicle_days={processed} "
                f"elapsed_seconds={elapsed:.1f}",
                flush=True,
            )

        with connection.cursor(name="consolidated_report_points") as cursor:
            cursor.itersize = fetch_size
            cursor.execute(query, (start, finish))
            key: tuple[date, str] | None = None
            display_plate = ""
            last_raw_plate: object = object()
            points: list[Point] = []

            for row in cursor:
                (
                    day_value,
                    stored_normalized_plate,
                    row_plate,
                    local_time,
                    lat,
                    lon,
                    speed,
                    distance,
                    odometer,
                    address,
                ) = row
                normalized = str(stored_normalized_plate or "").strip()
                if not normalized:
                    normalized = core.normalize_plate(row_plate)
                row_key = (day_value, normalized)

                if key is not None and row_key != key:
                    processed += 1
                    if processed % progress_every == 0:
                        log_progress(key[0])
                    yield key[0], display_plate, points
                    points = []
                    last_raw_plate = object()

                key = row_key
                if row_plate != last_raw_plate:
                    display_plate = core.clean(row_plate)
                    last_raw_plate = row_plate
                points.append(
                    Point(
                        plate=display_plate,
                        time=local_time,
                        lat=float(lat),
                        lon=float(lon),
                        odometer=float(odometer) if odometer is not None else None,
                        source_distance=float(distance) if distance is not None else None,
                        speed=float(speed) if speed is not None else None,
                        address=core.clean(address),
                    )
                )

            if key is not None and points:
                processed += 1
                yield key[0], display_plate, points
                log_progress(key[0], final=True)


def apply_consolidated_mileage_logic() -> None:
    """Install mileage corrections once for the consolidated-report process."""
    global _PATCHED
    if _PATCHED:
        return
    core.segment_distance = segment_distance_without_stationary_jitter
    core.iter_database_tracks = iter_database_tracks_with_odometer
    _PATCHED = True


__all__ = [
    "GPS_JITTER_MAX_DISTANCE_M",
    "GPS_JITTER_MAX_SPEED_KMH",
    "GPS_JITTER_RADIUS_M",
    "apply_consolidated_mileage_logic",
    "iter_database_tracks_with_odometer",
    "segment_distance_without_stationary_jitter",
]
