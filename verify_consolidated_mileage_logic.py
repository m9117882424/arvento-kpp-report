#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke tests for Arvento-distance preference and coordinate fallback."""
from __future__ import annotations

from datetime import datetime, timedelta

import consolidated_report as core
from arvento_io import Point
from consolidated_mileage_logic import (
    apply_consolidated_mileage_logic,
    iter_database_tracks_prefer_arvento,
    normalize_vehicle_day_distances,
    segment_distance_prefer_arvento,
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


def check_arvento_distance_is_used_unchanged() -> None:
    p1 = point(0, speed=0.0, distance=0.0)
    p2 = point(60, lon=33.00012, speed=0.0, distance=0.018)
    assert abs(segment_distance_prefer_arvento(p1, p2) - 0.018) < 1e-9


def check_missing_intervals_are_zero_when_day_has_arvento() -> None:
    points = [
        point(0, distance=None),
        point(60, distance=0.125),
        point(120, distance=None),
    ]
    assert normalize_vehicle_day_distances(points) is True
    assert points[0].source_distance == 0.0
    assert points[1].source_distance == 0.125
    assert points[2].source_distance == 0.0


def check_all_missing_day_keeps_coordinate_fallback() -> None:
    points = [
        point(0, speed=20.0, distance=None),
        point(60, lon=33.0010, speed=20.0, distance=None),
    ]
    assert normalize_vehicle_day_distances(points) is False
    assert points[0].source_distance is None
    assert points[1].source_distance is None
    assert segment_distance_prefer_arvento(points[0], points[1]) > 0.0


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


def check_invalid_arvento_value_is_zero_in_mixed_day() -> None:
    points = [
        point(0, distance=0.0),
        point(60, distance=0.1),
        point(120, distance=50.0),
    ]
    assert normalize_vehicle_day_distances(points) is True
    assert points[2].source_distance == 0.0


def check_patch_is_idempotent() -> None:
    apply_consolidated_mileage_logic()
    apply_consolidated_mileage_logic()
    assert core.segment_distance is segment_distance_prefer_arvento
    assert core.iter_database_tracks is iter_database_tracks_prefer_arvento


if __name__ == "__main__":
    check_arvento_distance_is_used_unchanged()
    check_missing_intervals_are_zero_when_day_has_arvento()
    check_all_missing_day_keeps_coordinate_fallback()
    check_stationary_coordinate_jitter_is_zero()
    check_short_real_coordinate_movement_is_kept_by_speed()
    check_impossible_coordinate_jump_is_zero()
    check_invalid_arvento_value_is_zero_in_mixed_day()
    check_patch_is_idempotent()
    print("OK: Arvento distance is authoritative per vehicle-day; coordinate fallback is isolated")
