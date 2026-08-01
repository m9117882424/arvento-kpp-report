#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Low-risk runtime optimizations for the consolidated report engine.

The optimized functions preserve the existing report rules while removing
millions of repeated Python calls observed in cProfile. The patch is installed
explicitly by ``fuel_enriched_consolidated_report`` after the time-logic patch.
"""
from __future__ import annotations

import math
import os
import time as monotonic_time
from datetime import date, datetime, time, timedelta
from typing import Any, Iterator, Sequence

import consolidated_multi_report as multi
import consolidated_report as core
from arvento_io import Point
from speed_violation_report import (
    MAX_ACCELERATION_MPS2,
    MAX_LOCAL_SPEED_DEVIATION_KMH,
    MAX_SPEED_EVENT_GAP_SECONDS,
    MIN_SPEED_EVENT_DURATION_SECONDS,
    MIN_SPEED_EVENT_POINTS,
)

_PATCHED = False
_NIGHT_RESULT_BY_POINTS_ID: dict[int, bool] = {}


def optimized_sanitize_position_outliers(points: Sequence[Point]) -> list[Point]:
    """Return the same filtered track with adjacent speeds calculated once."""
    source = [
        point
        for point in sorted(points, key=lambda item: item.time)
        if math.isfinite(point.lat)
        and math.isfinite(point.lon)
        and -90 <= point.lat <= 90
        and -180 <= point.lon <= 180
    ]
    if len(source) < 3:
        return source

    adjacent_speeds = [
        core.implied_speed_kmh(source[index], source[index + 1])
        for index in range(len(source) - 1)
    ]
    rejected: set[int] = set()
    for index in range(1, len(source) - 1):
        before = adjacent_speeds[index - 1]
        after = adjacent_speeds[index]
        if before <= core.POSITION_OUTLIER_SPEED_KMH or after <= core.POSITION_OUTLIER_SPEED_KMH:
            continue
        direct = core.implied_speed_kmh(source[index - 1], source[index + 1])
        if direct <= core.MAX_VALID_GPS_SPEED_KMH:
            rejected.add(index)

    if not rejected:
        return source
    return [point for index, point in enumerate(source) if index not in rejected]


def optimized_validated_speed_indices(points: Sequence[Point]) -> set[int]:
    """Match the legacy 3–7 point window rules using precomputed predicates.

    The previous implementation allocated ``SimpleNamespace`` objects and called
    ``_event_is_smooth`` for every overlapping window. This version calculates
    gaps, acceleration checks and local-spike checks once per chunk, then tests a
    window with prefix sums.
    """
    valid: set[int] = set()
    chunk: list[tuple[int, datetime, float]] = []

    def close_chunk() -> None:
        nonlocal chunk
        count = len(chunk)
        if count < 3:
            chunk = []
            return

        original_indices = [item[0] for item in chunk]
        timestamps = [item[1] for item in chunk]
        speeds = [item[2] for item in chunk]
        gaps = [
            (timestamps[index + 1] - timestamps[index]).total_seconds()
            for index in range(count - 1)
        ]

        bad_edge_prefix = [0]
        for index, seconds in enumerate(gaps):
            bad = (
                seconds <= 0
                or seconds > MAX_SPEED_EVENT_GAP_SECONDS
                or abs(speeds[index + 1] - speeds[index]) / 3.6 / seconds
                > MAX_ACCELERATION_MPS2
            )
            bad_edge_prefix.append(bad_edge_prefix[-1] + int(bad))

        bad_local = [0] * count
        for index in range(1, count - 1):
            dt_before = gaps[index - 1]
            dt_after = gaps[index]
            total = dt_before + dt_after
            if total <= 0:
                bad_local[index] = 1
                continue
            before = speeds[index - 1]
            current = speeds[index]
            after = speeds[index + 1]
            expected = before + (after - before) * (dt_before / total)
            is_local_extreme = current > max(before, after) or current < min(before, after)
            bad_local[index] = int(
                is_local_extreme
                and abs(current - expected) > MAX_LOCAL_SPEED_DEVIATION_KMH
            )

        bad_local_prefix = [0]
        for value in bad_local:
            bad_local_prefix.append(bad_local_prefix[-1] + value)

        for start in range(count):
            # Preserve the legacy maximum window length of seven points.
            for finish in range(start + 2, min(count, start + 7)):
                if finish - start + 1 < MIN_SPEED_EVENT_POINTS:
                    continue
                duration = (timestamps[finish] - timestamps[start]).total_seconds()
                if duration < MIN_SPEED_EVENT_DURATION_SECONDS:
                    continue
                if bad_edge_prefix[finish] - bad_edge_prefix[start]:
                    continue
                if bad_local_prefix[finish] - bad_local_prefix[start + 1]:
                    continue
                valid.update(original_indices[start : finish + 1])

        chunk = []

    previous_time: datetime | None = None
    for index, point in enumerate(points):
        speed = core.valid_speed(point.speed)
        gap = (
            (point.time - previous_time).total_seconds()
            if previous_time is not None
            else None
        )
        if speed is None or (
            gap is not None and (gap <= 0 or gap > MAX_SPEED_EVENT_GAP_SECONDS)
        ):
            close_chunk()
        if speed is not None:
            chunk.append((index, point.time, speed))
        previous_time = point.time
    close_chunk()
    return valid


def optimized_iter_database_tracks(
    database_url: str,
    start_day: date,
    end_day: date,
) -> Iterator[tuple[date, str, list[Point]]]:
    """Stream tracks using the stored normalized plate instead of normalizing every row."""
    try:
        import psycopg
    except ModuleNotFoundError as exc:
        raise RuntimeError("Для чтения PostgreSQL требуется пакет psycopg") from exc

    start = datetime.combine(start_day, time.min, tzinfo=core.TZ)
    finish = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=core.TZ)
    query = """
        SELECT
            (event_time AT TIME ZONE 'Europe/Istanbul')::date AS local_day,
            normalized_plate,
            COALESCE(NULLIF(plate, ''), normalized_plate) AS display_plate,
            event_time AT TIME ZONE 'Europe/Istanbul' AS local_time,
            latitude,
            longitude,
            speed_kmh,
            distance_km,
            COALESCE(address, '')
        FROM gps_points
        WHERE event_time >= %s AND event_time < %s
        ORDER BY local_day, normalized_plate, event_time
    """
    fetch_size = max(1_000, int(os.environ.get("CONSOLIDATED_DB_FETCH_SIZE", "50000")))
    progress_every = max(1, int(os.environ.get("CONSOLIDATED_PROGRESS_EVERY", "25")))
    started = monotonic_time.monotonic()
    processed = 0

    def log_progress(day_value: date, *, final: bool = False) -> None:
        elapsed = monotonic_time.monotonic() - started
        marker = "DONE" if final else "PROGRESS"
        print(
            f"CONSOLIDATED_{marker}: day={day_value} vehicle_days={processed} "
            f"elapsed_seconds={elapsed:.1f}",
            flush=True,
        )

    with psycopg.connect(database_url) as connection:
        with connection.cursor(name="consolidated_report_points") as cursor:
            cursor.itersize = fetch_size
            cursor.execute(query, (start, finish))
            key: tuple[date, str] | None = None
            display_plate = ""
            last_raw_plate: Any = object()
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
                        speed=float(speed) if speed is not None else None,
                        source_distance=float(distance) if distance is not None else None,
                        address=core.clean(address),
                    )
                )

            if key is not None and points:
                processed += 1
                yield key[0], display_plate, points
                log_progress(key[0], final=True)


def _install_night_result_reuse() -> None:
    """Reuse ``ReportRow.night_work`` instead of analysing the same track again."""
    original_analyze = core.analyze_track
    if not getattr(original_analyze, "_performance_night_cache_installed", False):
        def analyze_track(*args: Any, **kwargs: Any):
            points = args[2] if len(args) > 2 else kwargs.get("points")
            row = original_analyze(*args, **kwargs)
            if points is not None:
                _NIGHT_RESULT_BY_POINTS_ID[id(points)] = bool(row and row.night_work)
            return row

        analyze_track._performance_night_cache_installed = True
        core.analyze_track = analyze_track

    original_night = multi.has_night_site_mileage
    if not getattr(original_night, "_performance_night_cache_installed", False):
        def has_night_site_mileage(
            report_day: date,
            points: Sequence[Point],
            site_polygon: list[tuple[float, float]],
        ) -> bool:
            key = id(points)
            if key in _NIGHT_RESULT_BY_POINTS_ID:
                return _NIGHT_RESULT_BY_POINTS_ID.pop(key)
            return original_night(report_day, points, site_polygon)

        has_night_site_mileage._performance_night_cache_installed = True
        multi.has_night_site_mileage = has_night_site_mileage


def apply_consolidated_performance() -> None:
    """Install idempotent calculation optimizations for the current process."""
    global _PATCHED
    if _PATCHED:
        return
    core.sanitize_position_outliers = optimized_sanitize_position_outliers
    core.validated_speed_indices = optimized_validated_speed_indices
    core.iter_database_tracks = optimized_iter_database_tracks
    _install_night_result_reuse()
    _PATCHED = True


__all__ = [
    "apply_consolidated_performance",
    "optimized_iter_database_tracks",
    "optimized_sanitize_position_outliers",
    "optimized_validated_speed_indices",
]
