#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke tests for confirmed entry/exit, worked hours, and date preview."""
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


def outside_outside_track() -> list[Point]:
    return [
        point("04:50", -0.1),
        point("05:10", -0.1),
        point("06:00", -0.1),
        point("06:10", 0.5, 10),
        point("12:00", 0.5),
        point("12:10", -0.1, 10),
        point("13:00", -0.1),
        point("13:10", 0.5, 10),
        point("18:00", 0.5),
        point("18:10", -0.1, 10),
        point("22:50", -0.1),
        point("23:10", -0.1),
    ]


def check_outside_at_five_and_outside_at_23() -> None:
    points = outside_outside_track()
    arrived, departed = calculate_arrival_departure(REPORT_DAY, points, SITE)
    assert arrived is not None and arrived.hour == 6 and arrived.minute == 1
    assert departed is not None and departed.hour == 18 and departed.minute == 8

    worked_hours = calculate_worked_hours(
        REPORT_DAY,
        points,
        SITE,
        inside_km=1.0,
    )
    assert worked_hours is not None
    elapsed = (
        datetime.combine(REPORT_DAY, departed)
        - datetime.combine(REPORT_DAY, arrived)
    ).total_seconds() / 3600.0
    assert 0 < worked_hours < elapsed
    assert worked_hours < 12.2


def check_inside_at_five_and_inside_at_23_is_blank() -> None:
    points = [
        point("04:50", 0.5),
        point("05:10", 0.5),
        point("12:00", 0.6, 10),
        point("22:50", 0.7),
        point("23:10", 0.7),
    ]
    arrived, departed = calculate_arrival_departure(REPORT_DAY, points, SITE)
    assert arrived is None
    assert departed is None
    assert calculate_worked_hours(REPORT_DAY, points, SITE, inside_km=1.0) is None


def check_inside_at_five_and_outside_at_23_has_only_exit() -> None:
    points = [
        point("04:50", 0.5),
        point("05:10", 0.5),
        point("17:00", 0.5),
        point("17:10", -0.1, 10),
        point("22:50", -0.1),
        point("23:10", -0.1),
    ]
    arrived, departed = calculate_arrival_departure(REPORT_DAY, points, SITE)
    assert arrived is None
    assert departed is not None
    assert calculate_worked_hours(REPORT_DAY, points, SITE, inside_km=1.0) is None


def check_outside_at_five_and_inside_at_23_has_only_entry() -> None:
    points = [
        point("04:50", -0.1),
        point("05:10", -0.1),
        point("07:00", -0.1),
        point("07:10", 0.5, 10),
        point("22:50", 0.5),
        point("23:10", 0.5),
    ]
    arrived, departed = calculate_arrival_departure(REPORT_DAY, points, SITE)
    assert arrived is not None
    assert departed is None
    assert calculate_worked_hours(REPORT_DAY, points, SITE, inside_km=1.0) is None


def check_worked_hours_blank_without_site_mileage() -> None:
    points = outside_outside_track()
    assert calculate_worked_hours(REPORT_DAY, points, SITE, inside_km=0.0) is None

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
    check_outside_at_five_and_outside_at_23()
    check_inside_at_five_and_inside_at_23_is_blank()
    check_inside_at_five_and_outside_at_23_has_only_exit()
    check_outside_at_five_and_inside_at_23_has_only_entry()
    check_worked_hours_blank_without_site_mileage()
    check_date_preview()
    print("OK: confirmed entry/exit and worked-hours rules")
