#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke tests for confirmed entry/exit, worked hours, mileage filter, and roster company."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time
from pathlib import Path

import consolidated_report as core
from arvento_io import Point
from consolidated_time_logic import (
    PREFERRED_COMPANY_HEADER,
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


def check_mileage_difference_filter() -> None:
    import consolidated_time_logic as logic

    original = logic._ORIGINAL_ANALYZE_TRACK
    base = core.ReportRow(
        day=REPORT_DAY,
        company="",
        plate="TEST",
        user="",
        grade="",
        max_speed=None,
        route_max_speed=None,
        site_max_speed=None,
        total_km=50.0,
        inside_km=40.0,
        outside_km=10.0,
        distance_difference_km=40.0,
        inside_percent=80.0,
        outside_percent=20.0,
        percent_difference=60.0,
        departure=None,
        arrival=None,
        weekday="Четверг",
        entry_time=time(6, 0),
        exit_time=time(18, 0),
        worked_hours=10.0,
        boundary_violation=0,
        personal_use=0,
        weekend_work=0,
        night_work=0,
        in_roster=True,
        raw_points=2,
        retained_points=2,
        valid_speed_points=0,
        max_distance_from_site_km=0.0,
    )
    try:
        logic._ORIGINAL_ANALYZE_TRACK = lambda *_args, **_kwargs: base
        suppressed = logic.analyze_track_with_operational_times(
            REPORT_DAY, "TEST", outside_outside_track(), {}, SITE, SITE
        )
        assert suppressed is not None
        assert suppressed.entry_time is None
        assert suppressed.exit_time is None
        assert suppressed.worked_hours is None

        logic._ORIGINAL_ANALYZE_TRACK = lambda *_args, **_kwargs: replace(
            base,
            inside_km=39.0,
            outside_km=11.0,
        )
        retained = logic.analyze_track_with_operational_times(
            REPORT_DAY, "TEST", outside_outside_track(), {}, SITE, SITE
        )
        assert retained is not None
        assert retained.entry_time is not None
        assert retained.exit_time is not None
        assert retained.worked_hours is not None
    finally:
        logic._ORIGINAL_ANALYZE_TRACK = original


def check_preferred_company_header() -> None:
    assert core.ROSTER_ALIASES["company"][0] == PREFERRED_COMPANY_HEADER
    headers = ["Компания", PREFERRED_COMPANY_HEADER, "Гос рег знак"]
    assert core.find_column(headers, core.ROSTER_ALIASES["company"]) == 1


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
    check_mileage_difference_filter()
    check_preferred_company_header()
    check_date_preview()
    print("OK: confirmed times, mileage filter, and roster company rules")
