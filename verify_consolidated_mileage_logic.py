#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke tests for hybrid authoritative/GPS mileage selection."""
from __future__ import annotations

from datetime import datetime, timedelta

import consolidated_report as core
from arvento_io import Point
from consolidated_mileage_logic import (
    apply_consolidated_mileage_logic,
    coordinate_segment_distances,
    iter_database_tracks_prefer_arvento,
    normalize_vehicle_day_distances,
    segment_distance_prefer_arvento,
    should_use_coordinate_fallback,
)

START = datetime(2026, 8, 4, 8, 0, 0)


def point(
    seconds: int,
    *,
    lat: float = 36.0,
    lon: float = 33.0,
    speed: float | None = 0.0,
    distance: float | None = None,
) -> Point:
    return Point(
        plate="TEST",
        time=START + timedelta(seconds=seconds),
        lat=lat,
        lon=lon,
        speed=speed,
        source_distance=distance,
    )


def check_general_report_distance_is_used_unchanged() -> None:
    points = [
        point(0, distance=None),
        point(60, distance=0.125),
        point(120, distance=None),
    ]
    mode = normalize_vehicle_day_distances(points)
    assert mode == "general_report_distance"
    assert points[0].source_distance == 0.0
    assert points[1].source_distance == 0.125
    assert points[2].source_distance == 0.0
    assert all(item.prepared_distance for item in points)


def check_all_missing_day_keeps_coordinate_fallback() -> None:
    points = [
        point(0, speed=20.0, distance=None),
        point(60, lon=33.0010, speed=20.0, distance=None),
    ]
    mode = normalize_vehicle_day_distances(points)
    assert mode == "coordinate_only"
    assert points[0].source_distance is None
    assert points[1].source_distance is None
    assert segment_distance_prefer_arvento(points[0], points[1]) > 0.0


def check_authoritative_total_scales_coordinate_segments() -> None:
    points = [
        point(0, speed=30.0),
        point(60, lon=33.0010, speed=30.0),
        point(120, lon=33.0020, speed=30.0),
    ]
    coordinate_total = sum(coordinate_segment_distances(points))
    authoritative = coordinate_total * 1.10
    mode = normalize_vehicle_day_distances(points, authoritative)
    assert mode == "authoritative_scaled_coordinates"
    actual = sum(item.source_distance or 0.0 for item in points)
    assert abs(actual - authoritative) < 1e-9
    assert all(item.prepared_distance for item in points)


def check_large_odometer_overstatement_uses_coordinates() -> None:
    points = [
        point(0, speed=30.0),
        point(60, lon=33.0010, speed=30.0),
        point(120, lon=33.0020, speed=30.0),
    ]
    coordinate_total = sum(coordinate_segment_distances(points))
    authoritative = coordinate_total + 30.0
    mode = normalize_vehicle_day_distances(points, authoritative)
    assert mode == "coordinate_fallback"
    actual = sum(item.source_distance or 0.0 for item in points)
    assert abs(actual - coordinate_total) < 1e-9


def check_threshold_requires_both_absolute_and_relative_excess() -> None:
    assert not should_use_coordinate_fallback(109.0, 100.0)
    assert not should_use_coordinate_fallback(120.0, 105.0)
    assert should_use_coordinate_fallback(130.0, 100.0)
    assert should_use_coordinate_fallback(4300.2, 105.9)


def check_prepared_segment_accepts_scaled_value() -> None:
    p1 = point(0)
    p2 = point(60)
    p2.source_distance = 25.0
    p2.prepared_distance = True
    assert segment_distance_prefer_arvento(p1, p2) == 25.0


def check_stationary_coordinate_jitter_is_zero() -> None:
    p1 = point(0, speed=0.0, distance=None)
    p2 = point(60, lon=33.00012, speed=0.0, distance=None)
    assert segment_distance_prefer_arvento(p1, p2) == 0.0


def check_short_real_coordinate_movement_is_kept_by_speed() -> None:
    p1 = point(0, speed=8.0, distance=None)
    p2 = point(30, lon=33.00012, speed=8.0, distance=None)
    assert segment_distance_prefer_arvento(p1, p2) > 0.0


def check_impossible_coordinate_jump_is_zero() -> None:
    p1 = point(0, speed=None, distance=None)
    p2 = point(10, lon=34.0, speed=None, distance=None)
    assert segment_distance_prefer_arvento(p1, p2) == 0.0


def check_patch_is_idempotent() -> None:
    apply_consolidated_mileage_logic()
    apply_consolidated_mileage_logic()
    assert core.segment_distance is segment_distance_prefer_arvento
    assert core.iter_database_tracks is iter_database_tracks_prefer_arvento


if __name__ == "__main__":
    check_general_report_distance_is_used_unchanged()
    check_all_missing_day_keeps_coordinate_fallback()
    check_authoritative_total_scales_coordinate_segments()
    check_large_odometer_overstatement_uses_coordinates()
    check_threshold_requires_both_absolute_and_relative_excess()
    check_prepared_segment_accepts_scaled_value()
    check_stationary_coordinate_jitter_is_zero()
    check_short_real_coordinate_movement_is_kept_by_speed()
    check_impossible_coordinate_jump_is_zero()
    check_patch_is_idempotent()
    print("OK: authoritative daily mileage uses GPS distribution with anomaly fallback")
