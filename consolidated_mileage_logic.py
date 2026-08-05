#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hybrid mileage-source selection for the consolidated report.

``VehicleDistanceReport`` provides the authoritative vehicle-day total. The
GPS track is used to distribute that total across route and site segments. If
the authoritative total is substantially larger than coordinate mileage, the
coordinate mileage is used without scaling.

The anomaly threshold is deliberately two-dimensional: the authoritative
value must exceed coordinate mileage by both an absolute number of kilometres
and a relative multiplier. This prevents ordinary GPS path simplification from
being treated as an odometer failure.

When no authoritative daily value is available, the previous behaviour is
preserved: Arvento ``distance_km`` is preferred for the whole vehicle-day and
coordinate mileage is used only when the day has no usable Arvento distance.
"""
from __future__ import annotations

import math
import os
from datetime import date
from typing import Iterator, Sequence

import psycopg

import consolidated_report as core
from arvento_io import Point


GPS_JITTER_RADIUS_M = max(
    0.0,
    float(os.environ.get("CONSOLIDATED_GPS_JITTER_RADIUS_M", "50")),
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
HYBRID_ABSOLUTE_GAP_KM = max(
    0.0,
    float(os.environ.get("CONSOLIDATED_HYBRID_ABSOLUTE_GAP_KM", "10")),
)
HYBRID_RATIO = max(
    1.0,
    float(os.environ.get("CONSOLIDATED_HYBRID_RATIO", "1.20")),
)

_ORIGINAL_ITER_DATABASE_TRACKS = core.iter_database_tracks
_PATCHED = False


def _normalize_plate(value: object) -> str:
    return "".join(character for character in str(value or "").upper() if character.isalnum())


def _valid_nonnegative(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _valid_source_distance(value: object) -> float | None:
    number = _valid_nonnegative(value)
    if number is None or number > MAX_SEGMENT_DISTANCE_KM:
        return None
    return number


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


def coordinate_segment_distances(points: Sequence[Point]) -> list[float]:
    """Return a prepared coordinate distance for each point.

    The first point receives zero. Every following value represents movement
    from the preceding point to that point, matching the project's segment
    convention.
    """
    if not points:
        return []

    values = [0.0]
    for p1, p2 in zip(points, points[1:]):
        gap_seconds = (p2.time - p1.time).total_seconds()
        if gap_seconds <= 0:
            values.append(0.0)
            continue

        distance, implied_speed = _coordinate_distance(p1, p2, gap_seconds)
        reported_speed = _reported_speed((p1, p2))
        motion_speed = reported_speed if reported_speed is not None else implied_speed

        if (
            distance * 1000.0 <= GPS_JITTER_RADIUS_M
            and motion_speed <= GPS_JITTER_MAX_SPEED_KMH
        ):
            distance = 0.0

        values.append(max(0.0, distance))

    return values


def should_use_coordinate_fallback(
    authoritative_km: float,
    coordinate_km: float,
) -> bool:
    """Return True only for a substantial odometer-over-GPS anomaly."""
    if coordinate_km <= 0:
        return False
    return (
        authoritative_km - coordinate_km > HYBRID_ABSOLUTE_GAP_KM
        and authoritative_km > coordinate_km * HYBRID_RATIO
    )


def _assign_prepared_distances(
    points: Sequence[Point],
    values: Sequence[float],
    *,
    scale: float = 1.0,
) -> None:
    if len(points) != len(values):
        raise ValueError("Количество точек и подготовленных расстояний различается")

    for point, value in zip(points, values):
        point.source_distance = max(0.0, float(value) * scale)
        point.prepared_distance = True


def load_authoritative_daily_distances(
    database_url: str,
    start_day: date,
    end_day: date,
) -> dict[tuple[date, str], float]:
    """Read VehicleDistanceReport totals; an absent table means no override."""
    query = """
        SELECT report_day, normalized_plate, distance_km
        FROM vehicle_distance_daily
        WHERE report_day >= %s
          AND report_day <= %s
    """
    try:
        with psycopg.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, (start_day, end_day))
                result: dict[tuple[date, str], float] = {}
                for report_day, plate, distance in cursor:
                    value = _valid_nonnegative(distance)
                    normalized = _normalize_plate(plate)
                    if value is not None and normalized:
                        result[(report_day, normalized)] = value
                return result
    except psycopg.errors.UndefinedTable:
        return {}


def normalize_vehicle_day_distances(
    points: Sequence[Point],
    authoritative_distance_km: float | None = None,
) -> str:
    """Prepare one vehicle-day track and return the selected mileage mode."""
    authoritative_km = _valid_nonnegative(authoritative_distance_km)

    if authoritative_km is not None:
        coordinate_values = coordinate_segment_distances(points)
        coordinate_km = sum(coordinate_values)

        if coordinate_km > 0:
            if should_use_coordinate_fallback(authoritative_km, coordinate_km):
                _assign_prepared_distances(points, coordinate_values)
                return "coordinate_fallback"

            _assign_prepared_distances(
                points,
                coordinate_values,
                scale=authoritative_km / coordinate_km,
            )
            return "authoritative_scaled_coordinates"

        source_values = [
            _valid_source_distance(point.source_distance) or 0.0
            for point in points
        ]
        source_km = sum(source_values)

        if source_km > 0:
            _assign_prepared_distances(
                points,
                source_values,
                scale=authoritative_km / source_km,
            )
            return "authoritative_scaled_source"

        _assign_prepared_distances(points, [0.0] * len(points))
        return "no_usable_track"

    has_arvento_distance = any(
        _valid_source_distance(point.source_distance) is not None
        for point in points
    )
    if not has_arvento_distance:
        return "coordinate_only"

    source_values = [
        _valid_source_distance(point.source_distance) or 0.0
        for point in points
    ]
    _assign_prepared_distances(points, source_values)
    return "general_report_distance"


def segment_distance_prefer_arvento(p1: Point, p2: Point) -> float:
    """Return one prepared, Arvento, or coordinate-only segment."""
    gap_seconds = (p2.time - p1.time).total_seconds()
    if gap_seconds <= 0:
        return 0.0

    if p2.prepared_distance:
        prepared = _valid_nonnegative(p2.source_distance)
        return prepared if prepared is not None else 0.0

    source_km = _valid_source_distance(p2.source_distance)
    if source_km is not None:
        return source_km

    coordinate_km, implied_speed = _coordinate_distance(p1, p2, gap_seconds)
    reported_speed = _reported_speed((p1, p2))
    motion_speed = reported_speed if reported_speed is not None else implied_speed

    if (
        coordinate_km * 1000.0 <= GPS_JITTER_RADIUS_M
        and motion_speed <= GPS_JITTER_MAX_SPEED_KMH
    ):
        return 0.0

    return coordinate_km if coordinate_km > 0 else 0.0


def iter_database_tracks_prefer_arvento(
    database_url: str,
    start_day: date,
    end_day: date,
) -> Iterator[tuple[date, str, list[Point]]]:
    """Stream tracks while selecting one mileage mode per vehicle-day."""
    authoritative = load_authoritative_daily_distances(
        database_url,
        start_day,
        end_day,
    )

    for day_value, plate, points in _ORIGINAL_ITER_DATABASE_TRACKS(
        database_url,
        start_day,
        end_day,
    ):
        track = core.sanitize_position_outliers(points)
        daily_total = authoritative.get((day_value, _normalize_plate(plate)))
        normalize_vehicle_day_distances(track, daily_total)
        yield day_value, plate, track


def apply_consolidated_mileage_logic() -> None:
    """Install the hybrid mileage selection once for this process."""
    global _PATCHED
    if _PATCHED:
        return
    core.segment_distance = segment_distance_prefer_arvento
    core.iter_database_tracks = iter_database_tracks_prefer_arvento
    _PATCHED = True


__all__ = [
    "GPS_JITTER_MAX_SPEED_KMH",
    "GPS_JITTER_RADIUS_M",
    "HYBRID_ABSOLUTE_GAP_KM",
    "HYBRID_RATIO",
    "apply_consolidated_mileage_logic",
    "coordinate_segment_distances",
    "iter_database_tracks_prefer_arvento",
    "load_authoritative_daily_distances",
    "normalize_vehicle_day_distances",
    "segment_distance_prefer_arvento",
    "should_use_coordinate_fallback",
]
