#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke tests for stationary GPS-jitter filtering in consolidated mileage."""
from __future__ import annotations

from datetime import datetime, timedelta

import consolidated_report as core
from arvento_io import Point
from consolidated_mileage_logic import (
    apply_consolidated_mileage_logic,
    segment_distance_without_stationary_jitter,
)

START = datetime(2026, 8, 4, 8, 0, 0)


def point(
    seconds: int,
    *,
    lat: float = 36.0,
    lon: float = 33.0,
    speed: float | None = 0.0,
    distance: float | None = None,
    odometer: float | None = None,
) -> Point:
    return Point(
        plate="TEST",
        time=START + timedelta(seconds=seconds),
        lat=lat,
        lon=lon,
        speed=speed,
        source_distance=distance,
        odometer=odometer,
    )


def check_stationary_jitter_is_zero() -> None:
    p1 = point(0, speed=0.0)
    p2 = point(60, lon=33.00012, speed=0.0, distance=0.018)
    assert segment_distance_without_stationary_jitter(p1, p2) == 0.0


def check_short_real_movement_is_kept_by_speed() -> None:
    p1 = point(0, speed=8.0)
    p2 = point(30, lon=33.00012, speed=8.0, distance=0.018)
    assert segment_distance_without_stationary_jitter(p1, p2) > 0.0


def check_larger_movement_is_kept() -> None:
    p1 = point(0, speed=0.0)
    p2 = point(120, lon=33.0012, speed=0.0, distance=0.12)
    assert segment_distance_without_stationary_jitter(p1, p2) >= 0.1


def check_odometer_distance_is_never_filtered() -> None:
    p1 = point(0, speed=0.0, odometer=1000.000)
    p2 = point(60, lon=33.00005, speed=0.0, odometer=1000.012)
    value = segment_distance_without_stationary_jitter(p1, p2)
    assert abs(value - 0.012) < 1e-9


def check_missing_speed_uses_implied_speed() -> None:
    slow_p1 = point(0, speed=None)
    slow_p2 = point(120, lon=33.00010, speed=None, distance=0.015)
    assert segment_distance_without_stationary_jitter(slow_p1, slow_p2) == 0.0

    fast_p1 = point(0, speed=None)
    fast_p2 = point(10, lon=33.00010, speed=None, distance=0.015)
    assert segment_distance_without_stationary_jitter(fast_p1, fast_p2) > 0.0


def check_patch_is_idempotent() -> None:
    apply_consolidated_mileage_logic()
    apply_consolidated_mileage_logic()
    assert core.segment_distance is segment_distance_without_stationary_jitter


if __name__ == "__main__":
    check_stationary_jitter_is_zero()
    check_short_real_movement_is_kept_by_speed()
    check_larger_movement_is_kept()
    check_odometer_distance_is_never_filtered()
    check_missing_speed_uses_implied_speed()
    check_patch_is_idempotent()
    print("OK: stationary GPS jitter is excluded from consolidated mileage")
