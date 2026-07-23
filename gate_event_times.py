from __future__ import annotations

from datetime import datetime
from typing import Optional

from arvento_analysis import (
    EVENT_COOLDOWN_SECONDS,
    MAX_CROSSING_WINDOW_SECONDS,
    crossing_fraction,
    near_gate,
    side,
)
from arvento_io import Point
from geozone_registry import Registry


def detect_first_entry_last_exit(
    points: list[Point],
    registry: Registry,
) -> tuple[Optional[datetime], Optional[datetime]]:
    if len(points) < 2:
        return None, None

    vehicle_points = sorted(points, key=lambda point: point.time)
    stable_side = {gate.name: side(vehicle_points[0], gate) for gate in registry.gates}
    stable_time = {gate.name: vehicle_points[0].time for gate in registry.gates}
    last_event_time: Optional[datetime] = None
    first_entry: Optional[datetime] = None
    last_exit: Optional[datetime] = None

    for p1, p2 in zip(vehicle_points, vehicle_points[1:]):
        if p2.time <= p1.time:
            continue
        candidates: list[tuple[float, str]] = []
        for gate in registry.gates:
            side2 = side(p2, gate)
            previous = stable_side[gate.name]
            if side2 != 0 and previous != 0 and side2 != previous:
                elapsed = (p2.time - stable_time[gate.name]).total_seconds()
                if elapsed <= MAX_CROSSING_WINDOW_SECONDS and (near_gate(p1, gate) or near_gate(p2, gate)):
                    kind = "Въезд" if previous == 1 and side2 == -1 else "Выезд"
                    candidates.append((crossing_fraction(p1, p2, gate), kind))
            if side2 != 0:
                stable_side[gate.name] = side2
                stable_time[gate.name] = p2.time

        if not candidates:
            continue
        fraction, kind = min(candidates, key=lambda item: item[0])
        event_time = p1.time + (p2.time - p1.time) * fraction
        if last_event_time and (event_time - last_event_time).total_seconds() < EVENT_COOLDOWN_SECONDS:
            continue
        last_event_time = event_time
        if kind == "Въезд" and first_entry is None:
            first_entry = event_time
        elif kind == "Выезд":
            last_exit = event_time

    return first_entry, last_exit
