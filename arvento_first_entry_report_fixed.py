#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Запуск отчёта по первому въезду с устойчивым определением стороны КПП.

Исправляет пропуски пересечений, когда промежуточная GPS-точка попадает в
пограничную зону менее MIN_SIDE_DISTANCE_M от линии ворот.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Literal

import arvento_first_entry_report as base


def stable_side(distance_m: float) -> int:
    if distance_m >= base.MIN_SIDE_DISTANCE_M:
        return 1
    if distance_m <= -base.MIN_SIDE_DISTANCE_M:
        return -1
    return 0


def detect_coordinate_crossings(points: list[base.Point]) -> list[base.Crossing]:
    """Определяет пересечения через последнюю устойчивую сторону линии КПП.

    Последовательность вида +34 м -> -0,56 м -> -8,68 м теперь считается
    одним въездом: точка -0,56 м находится в пограничной зоне и не сбрасывает
    последнюю устойчивую внешнюю сторону.
    """
    by_plate: dict[str, list[base.Point]] = defaultdict(list)
    for point in points:
        by_plate[point.plate].append(point)

    crossings: list[base.Crossing] = []
    for plate, vehicle_points in by_plate.items():
        vehicle_points.sort(key=lambda item: item.timestamp)
        last_by_gate_direction: dict[tuple[str, str], datetime] = {}

        stable: dict[str, tuple[int, base.Point] | None] = {}
        for gate_name, gate in base.GATE_SEGMENTS.items():
            first_point = vehicle_points[0] if vehicle_points else None
            if first_point is None:
                stable[gate_name] = None
                continue
            first_side = stable_side(base.signed_gate_distance_m(first_point, gate))
            stable[gate_name] = (first_side, first_point) if first_side else None

        for current in vehicle_points[1:]:
            for gate_name, gate in base.GATE_SEGMENTS.items():
                current_side = stable_side(base.signed_gate_distance_m(current, gate))
                if current_side == 0:
                    continue

                previous = stable.get(gate_name)
                if previous is None:
                    stable[gate_name] = (current_side, current)
                    continue

                previous_side, previous_point = previous
                if current_side == previous_side:
                    stable[gate_name] = (current_side, current)
                    continue

                gap = (current.timestamp - previous_point.timestamp).total_seconds()
                if gap <= 0 or gap > base.MAX_GPS_GAP_SECONDS:
                    stable[gate_name] = (current_side, current)
                    continue

                direction: Literal["entry", "exit"] = (
                    "entry" if previous_side == 1 and current_side == -1 else "exit"
                )
                fraction = base.crossing_fraction(previous_point, current, gate)
                stable[gate_name] = (current_side, current)
                if fraction is None:
                    continue

                timestamp = previous_point.timestamp + (
                    current.timestamp - previous_point.timestamp
                ) * fraction
                key = (gate_name, direction)
                prior_event = last_by_gate_direction.get(key)
                if prior_event and (
                    timestamp - prior_event
                ).total_seconds() < base.CROSSING_COOLDOWN_SECONDS:
                    continue

                lat = previous_point.lat + (current.lat - previous_point.lat) * fraction
                lon = previous_point.lon + (current.lon - previous_point.lon) * fraction
                crossings.append(
                    base.Crossing(
                        plate=plate,
                        timestamp=timestamp,
                        gate=gate_name,
                        direction=direction,
                        lat=lat,
                        lon=lon,
                    )
                )
                last_by_gate_direction[key] = timestamp

    return sorted(crossings, key=lambda item: (item.timestamp, item.plate, item.gate))


base.detect_coordinate_crossings = detect_coordinate_crossings


if __name__ == "__main__":
    try:
        base.main()
    except Exception as exc:
        print(f"ОШИБКА: {exc}")
        raise
