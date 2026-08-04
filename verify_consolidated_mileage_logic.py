#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke tests for odometer-backed consolidated mileage and jitter filtering."""
from __future__ import annotations

from datetime import datetime, timedelta

import consolidated_report as core
from arvento_io import Point
from consolidated_mileage_logic import (
    apply_consolidated_mileage_logic,
    iter_database_tracks_with_odometer,
    segment_distance_without_stationary_jitter,
)
from parse_arvento_general_report import parse_general_report_rows

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


def check_stationary_jitter_is_zero_even_with_odometer_change() -> None:
    p1 = point(0, speed=0.0, odometer=1000.000)
    p2 = point(
        60,
        lon=33.00012,
        speed=0.0,
        distance=0.018,
        odometer=1000.012,
    )
    assert segment_distance_without_stationary_jitter(p1, p2) == 0.0


def check_positive_odometer_can_reduce_calculated_distance() -> None:
    p1 = point(0, speed=20.0, odometer=1000.000)
    p2 = point(
        60,
        lon=33.0010,
        speed=20.0,
        distance=0.090,
        odometer=1000.060,
    )
    value = segment_distance_without_stationary_jitter(p1, p2)
    assert abs(value - 0.060) < 1e-9


def check_odometer_cannot_increase_calculated_distance() -> None:
    p1 = point(0, speed=20.0, odometer=1000.000)
    p2 = point(
        60,
        lon=33.0010,
        speed=20.0,
        distance=0.090,
        odometer=1000.140,
    )
    value = segment_distance_without_stationary_jitter(p1, p2)
    assert abs(value - 0.090) < 1e-9


def check_zero_odometer_does_not_erase_real_movement() -> None:
    p1 = point(0, speed=20.0, odometer=1000.000)
    p2 = point(
        60,
        lon=33.0010,
        speed=20.0,
        distance=0.090,
        odometer=1000.000,
    )
    value = segment_distance_without_stationary_jitter(p1, p2)
    assert abs(value - 0.090) < 1e-9


def check_short_real_movement_is_kept_by_speed() -> None:
    p1 = point(0, speed=8.0)
    p2 = point(30, lon=33.00012, speed=8.0, distance=0.018)
    assert segment_distance_without_stationary_jitter(p1, p2) > 0.0


def check_larger_movement_is_kept() -> None:
    p1 = point(0, speed=0.0)
    p2 = point(120, lon=33.0012, speed=0.0, distance=0.12)
    assert segment_distance_without_stationary_jitter(p1, p2) >= 0.1


def check_missing_speed_uses_implied_speed() -> None:
    slow_p1 = point(0, speed=None)
    slow_p2 = point(120, lon=33.00010, speed=None, distance=0.015)
    assert segment_distance_without_stationary_jitter(slow_p1, slow_p2) == 0.0

    fast_p1 = point(0, speed=None)
    fast_p2 = point(10, lon=33.00010, speed=None, distance=0.015)
    assert segment_distance_without_stationary_jitter(fast_p1, fast_p2) > 0.0


def check_parser_reads_english_odometer() -> None:
    xml = """<?xml version="1.0"?>
    <DataSet><General_x0020_Report>
      <License_x0020_Plate>33 TEST 01</License_x0020_Plate>
      <Date_x002F_Time>2026-08-04T08:00:00</Date_x002F_Time>
      <Latitude>36.1</Latitude><Longitude>33.1</Longitude>
      <Distance>0.125</Distance>
      <Distance_x0020_Counter_x0020_km>12345.75</Distance_x0020_Counter_x0020_km>
    </General_x0020_Report></DataSet>"""
    rows = parse_general_report_rows(xml)
    assert len(rows) == 1
    assert rows[0].distance == 0.125
    assert rows[0].odometer == 12345.75


def check_parser_reads_turkish_odometer() -> None:
    xml = """<?xml version="1.0"?>
    <DataSet><General_x0020_Report>
      <License_x0020_Plate>33 TEST 02</License_x0020_Plate>
      <Date_x002F_Time>2026-08-04T08:00:00</Date_x002F_Time>
      <Latitude>36.1</Latitude><Longitude>33.1</Longitude>
      <Mesafe_x0020_Sayacı_x0020_km>54321.5</Mesafe_x0020_Sayacı_x0020_km>
    </General_x0020_Report></DataSet>"""
    rows = parse_general_report_rows(xml)
    assert len(rows) == 1
    assert rows[0].odometer == 54321.5


def check_patch_is_idempotent() -> None:
    apply_consolidated_mileage_logic()
    apply_consolidated_mileage_logic()
    assert core.segment_distance is segment_distance_without_stationary_jitter
    assert core.iter_database_tracks is iter_database_tracks_with_odometer


if __name__ == "__main__":
    check_stationary_jitter_is_zero_even_with_odometer_change()
    check_positive_odometer_can_reduce_calculated_distance()
    check_odometer_cannot_increase_calculated_distance()
    check_zero_odometer_does_not_erase_real_movement()
    check_short_real_movement_is_kept_by_speed()
    check_larger_movement_is_kept()
    check_missing_speed_uses_implied_speed()
    check_parser_reads_english_odometer()
    check_parser_reads_turkish_odometer()
    check_patch_is_idempotent()
    print("OK: Arvento odometer is parsed, bounded, and stationary jitter is excluded")
