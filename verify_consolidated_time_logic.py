#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke tests for consolidated operational-time and date-preview rules."""
from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path

import consolidated_report as core
from arvento_io import Point
from consolidated_time_logic import (
    apply_consolidated_date_preview,
    apply_consolidated_time_logic,
    calculate_arrival_departure,
    calculate_worked_hours,
)

REPORT_DAY = date(2026, 7, 30)
SITE = [(0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0)]


def point(clock: str, lon: float, speed: float = 0.0) -> Point:
    return Point(
        plate="TEST",
        time=datetime.combine(REPORT_DAY, time.fromisoformat(clock)),
        lat=0.5,
        lon=lon,
        speed=speed,
    )


def check_outside_at_five() -> None:
    arrived, departed = calculate_arrival_departure(
        REPORT_DAY,
        [
            point("04:50", -0.1),
            point("05:10", -0.1),
            point("06:00", -0.1),
            point("06:10", 0.5, 10),
            point("18:00", 0.5),
            point("18:10", -0.1, 10),
            point("22:50", -0.1),
        ],
        SITE,
    )
    assert arrived is not None and arrived.hour == 6 and arrived.minute == 1
    assert departed is not None and departed.hour == 18 and departed.minute == 8


def check_inside_at_five() -> None:
    arrived, departed = calculate_arrival_departure(
        REPORT_DAY,
        [
            point("04:50", 0.5),
            point("05:10", 0.5),
            point("07:00", 0.5),
            point("07:02", 0.5003, 10),
            point("07:04", 0.5003),
            point("17:00", 0.5003),
            point("17:02", 0.5006, 10),
            point("17:04", 0.5006),
            point("22:50", 0.5006),
        ],
        SITE,
    )
    assert arrived == time(7, 0)
    assert departed == time(17, 4)


def check_worked_hours_clipped_to_window() -> None:
    worked_hours = calculate_worked_hours(
        REPORT_DAY,
        [
            point("04:00", 0.2),
            point("12:00", 0.3),
            point("23:59", 0.4),
        ],
        SITE,
        inside_km=1.0,
    )
    assert worked_hours is not None
    assert abs(worked_hours - 18.0) < 1e-9


def check_worked_hours_blank_without_site_mileage() -> None:
    worked_hours = calculate_worked_hours(
        REPORT_DAY,
        [
            point("06:00", 0.5),
            point("10:00", 0.5),
        ],
        SITE,
        inside_km=0.0,
    )
    assert worked_hours is None

    apply_consolidated_time_logic()
    row = core.analyze_track(
        REPORT_DAY,
        "TEST",
        [
            point("06:00", 0.5),
            point("10:00", 0.5),
        ],
        {},
        SITE,
        SITE,
    )
    assert row is not None
    assert row.inside_km == 0.0
    assert row.worked_hours is None


def check_site_mileage_outside_window_returns_zero() -> None:
    worked_hours = calculate_worked_hours(
        REPORT_DAY,
        [
            point("23:10", 0.2),
            point("23:20", 0.3),
        ],
        SITE,
        inside_km=1.0,
    )
    assert worked_hours == 0.0


def check_date_preview() -> None:
    class FakeImplementation:
        @staticmethod
        def workbook_preview(_path: Path):
            return ["Дата", "Госномер"], [["30.07.2026 00:00:00", "TEST"]], 1

    implementation = FakeImplementation()
    apply_consolidated_date_preview(implementation)
    columns, rows, total = implementation.workbook_preview(Path("unused.xlsx"))
    assert columns == ["Дата", "Госномер"]
    assert rows == [["30.07.2026", "TEST"]]
    assert total == 1


if __name__ == "__main__":
    check_outside_at_five()
    check_inside_at_five()
    check_worked_hours_clipped_to_window()
    check_worked_hours_blank_without_site_mileage()
    check_site_mileage_outside_window_returns_zero()
    check_date_preview()
    print("OK: consolidated operational-time and date-preview rules")
