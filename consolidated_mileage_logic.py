#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mileage-source selection for the consolidated report.

For PostgreSQL vehicle-day tracks, Arvento ``distance_km`` is authoritative
when it is available for at least one point of that vehicle-day. Missing or
invalid interval values in such a track contribute zero and are not replaced
with coordinate distance. Coordinate distance is used only when the whole
vehicle-day has no usable Arvento distance values.
"""
from __future__ import annotations

import math
import os
from datetime import date
from typing import Iterator, Sequence

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

_ORIGINAL_ITER_DATABASE_TRACKS = core.iter_database_tracks
_PATCHED = False


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


def normalize_vehicle_day_distances(points: Sequence[Point]) -> bool:
    """Prepare one vehicle-day track and return whether Arvento distance exists.

    When at least one usable ``source_distance`` value is present, every missing
    or invalid value is changed to ``0.0``. This prevents mixing Arvento interval
    distance with coordinate distance inside the same daily mileage total.
    """
    has_arvento_distance = any(
        _valid_source_distance(point.source_distance) is not None
        for point in points
    )
    if not has_arvento_distance:
        return False

    for point in points:
        value = _valid_source_distance(point.source_distance)
        point.source_distance = value if value is not None else 0.0
    return True


def segment_distance_prefer_arvento(p1: Point, p2: Point) -> float:
    """Return one segment using Arvento distance or coordinate-only fallback."""
    gap_seconds = (p2.time - p1.time).total_seconds()
    if gap_seconds <= 0:
        return 0.0

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
    """Stream tracks while enforcing one mileage source per vehicle-day."""
    for day_value, plate, points in _ORIGINAL_ITER_DATABASE_TRACKS(
        database_url,
        start_day,
        end_day,
    ):
        normalize_vehicle_day_distances(points)
        yield day_value, plate, points


def apply_consolidated_mileage_logic() -> None:
    """Install the mileage-source selection once for this process."""
    global _PATCHED
    if _PATCHED:
        return
    core.segment_distance = segment_distance_prefer_arvento
    core.iter_database_tracks = iter_database_tracks_prefer_arvento
    _PATCHED = True


__all__ = [
    "GPS_JITTER_MAX_SPEED_KMH",
    "GPS_JITTER_RADIUS_M",
    "apply_consolidated_mileage_logic",
    "iter_database_tracks_prefer_arvento",
    "normalize_vehicle_day_distances",
    "segment_distance_prefer_arvento",
]
