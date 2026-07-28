#!/usr/bin/env python3
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


def check_slow_left_exit_after_start_timeout() -> None:
    """Regression for 34MPM501 from the 28.07.2026 distance export.

    The vehicle enters the right/start section, remains there longer than the
    three-minute candidate limit, then continues through the corridor and exits
    physically through the left side. The timeout must not permanently disarm
    detection while the vehicle is still in the start section.
    """
    raw = [
        ("2026-07-28 07:15:08", 36.317429, 33.877834, 26),
        ("2026-07-28 07:15:17", 36.317329, 33.877167, 26),
        ("2026-07-28 07:15:19", 36.317352, 33.877090, 10),
        ("2026-07-28 07:20:24", 36.317337, 33.876514, 5),
        ("2026-07-28 07:20:28", 36.317390, 33.876347, 19),
        ("2026-07-28 07:20:34", 36.317406, 33.875828, 30),
        ("2026-07-28 07:20:38", 36.317352, 33.875347, 43),
        ("2026-07-28 07:20:45", 36.317234, 33.874718, 30),
        ("2026-07-28 07:20:56", 36.317032, 33.873722, 22),
        ("2026-07-28 07:20:59", 36.317123, 33.873615, 11),
        ("2026-07-28 07:21:11", 36.317219, 33.873253, 30),
        ("2026-07-28 07:21:13", 36.317181, 33.872971, 43),
        ("2026-07-28 07:21:16", 36.317131, 33.872505, 56),
    ]
    track = [
        point(datetime.fromisoformat(value), lat, lon, speed, "34MPM501")
        for value, lat, lon, speed in raw
    ]
    violations = detect_confirmed_violations(
        track,
        width_m=legacy.DEFAULT_WIDTH_M,
        max_sequence_seconds=legacy.DEFAULT_MAX_SEQUENCE_SECONDS,
        control_window_seconds=legacy.DEFAULT_CONTROL_WINDOW_SECONDS,
        cooldown_seconds=legacy.DEFAULT_COOLDOWN_SECONDS,
    )
    assert len(violations) == 1, (
        "34MPM501: подтверждённый выход слева после остановки в начале не найден"
    )
    violation = violations[0]
    assert violation.plate == "34MPM501"
    assert violation.start == datetime(2026, 7, 28, 7, 20, 24)
    assert violation.finish == datetime(2026, 7, 28, 7, 21, 16)
    assert violation.finish_position.progress >= EXIT_PROGRESS_MIN


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
    check_slow_left_exit_after_start_timeout()
    check_control_zone_cancellation()
    print(
        "OK: запрещённый поворот требует фактического выхода слева; "
        "возврат вправо, остановка в начале и разрешающая геозона проверены."
    )


if __name__ == "__main__":
    main()
