#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mileage corrections for the consolidated report.

The source report may contain small coordinate movements while a vehicle is
stationary. Summing every such segment produces artificial daily mileage. This
module suppresses only short, low-speed segments and leaves odometer-backed
movement untouched.
"""
from __future__ import annotations

import math
import os
from typing import Sequence

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

_ORIGINAL_SEGMENT_DISTANCE = core.segment_distance
_PATCHED = False


def _valid_odometer_delta(p1: Point, p2: Point) -> float | None:
    if p1.odometer is None or p2.odometer is None:
        return None
    try:
        delta = float(p2.odometer) - float(p1.odometer)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(delta) or delta < 0:
        return None
    max_delta = float(getattr(core, "MAX_ODOMETER_DELTA_KM", 20.0))
    return delta if delta <= max_delta else None


def _reported_speed(points: Sequence[Point]) -> float | None:
    values: list[float] = []
    for point in points:
        try:
            value = float(point.speed) if point.speed is not None else None
        except (TypeError, ValueError):
            value = None
        if value is not None and math.isfinite(value) and value >= 0:
            values.append(value)
    return max(values) if values else None


def segment_distance_without_stationary_jitter(p1: Point, p2: Point) -> float:
    """Return segment mileage with stationary GPS jitter removed.

    Rules:
    * valid odometer deltas are trusted and never filtered;
    * a segment is suppressed only when both the calculated distance and the
      coordinate displacement are small;
    * reported speed is preferred as motion evidence; when it is absent, the
      implied coordinate speed is used.
    """
    gap_seconds = (p2.time - p1.time).total_seconds()
    if gap_seconds <= 0:
        return 0.0

    odometer_delta = _valid_odometer_delta(p1, p2)
    if odometer_delta is not None:
        return odometer_delta

    distance_km = _ORIGINAL_SEGMENT_DISTANCE(p1, p2)
    if not math.isfinite(distance_km) or distance_km <= 0:
        return 0.0

    coordinate_km = core.haversine_km_coords(p1.lat, p1.lon, p2.lat, p2.lon)
    reported_speed = _reported_speed((p1, p2))
    implied_speed = coordinate_km / (gap_seconds / 3600.0)
    motion_speed = reported_speed if reported_speed is not None else implied_speed

    is_short_reported_segment = distance_km * 1000.0 <= GPS_JITTER_MAX_DISTANCE_M
    is_small_coordinate_shift = coordinate_km * 1000.0 <= GPS_JITTER_RADIUS_M
    is_stationary = motion_speed <= GPS_JITTER_MAX_SPEED_KMH

    if is_short_reported_segment and is_small_coordinate_shift and is_stationary:
        return 0.0
    return distance_km


def apply_consolidated_mileage_logic() -> None:
    """Install the mileage correction once for the consolidated-report process."""
    global _PATCHED
    if _PATCHED:
        return
    core.segment_distance = segment_distance_without_stationary_jitter
    _PATCHED = True


__all__ = [
    "GPS_JITTER_MAX_DISTANCE_M",
    "GPS_JITTER_MAX_SPEED_KMH",
    "GPS_JITTER_RADIUS_M",
    "apply_consolidated_mileage_logic",
    "segment_distance_without_stationary_jitter",
]
