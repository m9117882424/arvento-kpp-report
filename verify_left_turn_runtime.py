#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runtime regression checks for prohibited-left-turn detection."""
from __future__ import annotations

from datetime import datetime, timedelta

import prohibited_left_turn_report as legacy
from confirmed_left_turn_detector import (
    EXIT_PROGRESS_MIN,
    detect_confirmed_violations,
)


def point(
    timestamp: datetime,
    lat: float,
    lon: float,
    speed: float = 15.0,
    plate: str = "TEST",
) -> legacy.TrackPoint:
    return legacy.TrackPoint(
        plate=plate,
        timestamp=timestamp,
        lat=lat,
        lon=lon,
        speed=speed,
        address="",
    )


def coordinate_at_progress(progress: float) -> tuple[float, float]:
    target = max(0.0, min(1.0, progress)) * legacy.POLYLINE_LENGTH_M
    for index, (start, finish) in enumerate(
        zip(legacy.CUMULATIVE_M, legacy.CUMULATIVE_M[1:])
    ):
        if target > finish:
            continue
        fraction = 0.0 if finish == start else (target - start) / (finish - start)
        p1 = legacy.CORRIDOR_POINTS[index]
        p2 = legacy.CORRIDOR_POINTS[index + 1]
        return (
            p1[0] + (p2[0] - p1[0]) * fraction,
            p1[1] + (p2[1] - p1[1]) * fraction,
        )
    return legacy.CORRIDOR_POINTS[-1]


def extend_endpoint(
    endpoint: tuple[float, float],
    neighbour: tuple[float, float],
    factor: float,
) -> tuple[float, float]:
    return (
        endpoint[0] + (endpoint[0] - neighbour[0]) * factor,
        endpoint[1] + (endpoint[1] - neighbour[1]) * factor,
    )


def check_right_side_return() -> None:
    """Regression for 46AJP552: entered right, reversed, exited right."""
    raw = [
        ("2026-07-28 19:51:53", 36.317406, 33.877708, 11),
        ("2026-07-28 19:51:59", 36.317394, 33.877487, 0),
        ("2026-07-28 19:52:09", 36.317390, 33.877136, 18),
        ("2026-07-28 19:52:53", 36.317215, 33.874279, 29),
        ("2026-07-28 19:53:06", 36.317120, 33.873646, 11),
        ("2026-07-28 19:53:27", 36.317146, 33.873489, 6),
        ("2026-07-28 19:53:33", 36.317146, 33.873703, 13),
        ("2026-07-28 19:53:43", 36.317249, 33.874245, 18),
        ("2026-07-28 19:54:50", 36.317348, 33.877335, 15),
        ("2026-07-28 19:55:00", 36.317375, 33.877773, 15),
    ]
    track = [
        point(datetime.fromisoformat(value), lat, lon, speed, "46AJP552")
        for value, lat, lon, speed in raw
    ]
    violations = detect_confirmed_violations(
        track,
        width_m=legacy.DEFAULT_WIDTH_M,
        max_sequence_seconds=5 * 60,
        control_window_seconds=legacy.DEFAULT_CONTROL_WINDOW_SECONDS,
        cooldown_seconds=legacy.DEFAULT_COOLDOWN_SECONDS,
    )
    assert not violations, (
        "Возврат и выход через правую сторону ошибочно признан запрещённым поворотом"
    )


def build_valid_left_exit() -> list[legacy.TrackPoint]:
    start_time = datetime(2026, 1, 1, 8, 0, 0)
    right_outside = extend_endpoint(
        legacy.CORRIDOR_POINTS[0], legacy.CORRIDOR_POINTS[1], 0.40
    )
    left_outside = extend_endpoint(
        legacy.CORRIDOR_POINTS[-1], legacy.CORRIDOR_POINTS[-2], 0.40
    )
    coordinates = [
        right_outside,
        coordinate_at_progress(0.05),
        coordinate_at_progress(0.35),
        coordinate_at_progress(0.65),
        coordinate_at_progress(0.85),
        coordinate_at_progress(0.98),
        left_outside,
    ]
    return [
        point(start_time + timedelta(seconds=index * 15), lat, lon, 20)
        for index, (lat, lon) in enumerate(coordinates)
    ]


def check_valid_left_exit() -> None:
    violations = detect_confirmed_violations(
        build_valid_left_exit(),
        width_m=legacy.DEFAULT_WIDTH_M,
        max_sequence_seconds=legacy.DEFAULT_MAX_SEQUENCE_SECONDS,
        control_window_seconds=legacy.DEFAULT_CONTROL_WINDOW_SECONDS,
        cooldown_seconds=legacy.DEFAULT_COOLDOWN_SECONDS,
    )
    assert len(violations) == 1, "Фактический выход через левую сторону не зафиксирован"
    violation = violations[0]
    assert violation.finish_position.progress >= EXIT_PROGRESS_MIN
    assert violation.finish_position.distance_m > legacy.DEFAULT_WIDTH_M / 2.0


def check_control_zone_cancellation() -> None:
    track = build_valid_left_exit()
    track.append(
        point(
            track[-1].timestamp + timedelta(seconds=30),
            legacy.CONTROL_ZONE_CENTER[0],
            legacy.CONTROL_ZONE_CENTER[1],
            10,
        )
    )
    violations = detect_confirmed_violations(
        track,
        width_m=legacy.DEFAULT_WIDTH_M,
        max_sequence_seconds=legacy.DEFAULT_MAX_SEQUENCE_SECONDS,
        control_window_seconds=legacy.DEFAULT_CONTROL_WINDOW_SECONDS,
        cooldown_seconds=legacy.DEFAULT_COOLDOWN_SECONDS,
    )
    assert not violations, "Разрешающая контрольная геозона не отменила нарушение"


def main() -> None:
    check_right_side_return()
    check_valid_left_exit()
    check_control_zone_cancellation()
    print(
        "OK: запрещённый поворот подтверждается только фактическим выходом слева; "
        "возврат вправо и разрешающая геозона проверены."
    )


if __name__ == "__main__":
    main()
