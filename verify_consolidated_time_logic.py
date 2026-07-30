#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke tests for consolidated arrival/departure and date-only preview rules."""
from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path

from arvento_io import Point
from consolidated_time_logic import (
    apply_consolidated_date_preview,
    calculate_arrival_departure,
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
    check_date_preview()
    print("OK: consolidated arrival/departure and date preview rules")
