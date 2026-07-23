from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime
from typing import Any, Optional

from arvento_io import Point
from geozone_registry import Gate, Registry, locate_zone

STOP_GAP_SECONDS = 10 * 60
SIDE_HYSTERESIS_M = 18.0
GATE_MARGIN_M = 45.0
MAX_CROSSING_WINDOW_SECONDS = 15 * 60
EVENT_COOLDOWN_SECONDS = 120
MAX_ODOMETER_DELTA_KM = 20.0
MAX_REASONABLE_SPEED_KMH = 180.0


def local_xy(lat: float, lon: float, origin_lat: float, origin_lon: float) -> tuple[float, float]:
    return (
        (lon - origin_lon) * 111_320.0 * math.cos(math.radians(origin_lat)),
        (lat - origin_lat) * 111_320.0,
    )


def gate_geometry(gate: Gate) -> tuple[float, float, float, float, float]:
    mid_lat = (gate.p1[0] + gate.p2[0]) / 2.0
    mid_lon = (gate.p1[1] + gate.p2[1]) / 2.0
    x1, y1 = local_xy(*gate.p1, mid_lat, mid_lon)
    x2, y2 = local_xy(*gate.p2, mid_lat, mid_lon)
    vx, vy = x2 - x1, y2 - y1
    length = math.hypot(vx, vy)
    if length < 0.1:
        vx, vy, length = 20.0, 0.0, 20.0
    return mid_lat, mid_lon, vx, vy, length


def gate_coords(point: Point, gate: Gate) -> tuple[float, float]:
    mid_lat, mid_lon, vx, vy, length = gate_geometry(gate)
    px, py = local_xy(point.lat, point.lon, mid_lat, mid_lon)
    ux, uy = vx / length, vy / length
    nx, ny = -uy, ux
    along = px * ux + py * uy
    across = px * nx + py * ny
    if ny < 0:
        across = -across
    return along, across


def side(point: Point, gate: Gate) -> int:
    across = gate_coords(point, gate)[1]
    if across > SIDE_HYSTERESIS_M:
        return 1
    if across < -SIDE_HYSTERESIS_M:
        return -1
    return 0


def near_gate(point: Point, gate: Gate) -> bool:
    along, across = gate_coords(point, gate)
    return (
        abs(across) <= GATE_MARGIN_M
        and abs(along) <= gate_geometry(gate)[4] / 2.0 + GATE_MARGIN_M
    )


def crossing_fraction(p1: Point, p2: Point, gate: Gate) -> float:
    a1 = gate_coords(p1, gate)[1]
    a2 = gate_coords(p2, gate)[1]
    delta = a2 - a1
    if abs(delta) < 1e-12:
        return 0.5
    return max(0.0, min(1.0, -a1 / delta))


def haversine_km(p1: Point, p2: Point) -> float:
    radius = 6371.0088
    lat1 = math.radians(p1.lat)
    lat2 = math.radians(p2.lat)
    dlat = math.radians(p2.lat - p1.lat)
    dlon = math.radians(p2.lon - p1.lon)
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def segment_distance(p1: Point, p2: Point) -> float:
    gap = (p2.time - p1.time).total_seconds()
    if gap <= 0:
        return 0.0
    if p1.odometer is not None and p2.odometer is not None:
        delta = p2.odometer - p1.odometer
        if 0 <= delta <= MAX_ODOMETER_DELTA_KM:
            return delta
    if p2.source_distance is not None and 0 <= p2.source_distance <= MAX_ODOMETER_DELTA_KM:
        return p2.source_distance
    gps = haversine_km(p1, p2)
    return gps if gps / (gap / 3600.0) <= MAX_REASONABLE_SPEED_KMH else 0.0


def analyze_day(points: list[Point], registry: Registry) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[str, list[Point]] = defaultdict(list)
    for point in points:
        grouped[point.plate].append(point)

    summaries: list[dict[str, Any]] = []
    stops: list[dict[str, Any]] = []

    for plate in sorted(grouped):
        vehicle_points = sorted(grouped[plate], key=lambda point: point.time)
        if len(vehicle_points) < 2:
            continue

        stable_side = {gate.name: side(vehicle_points[0], gate) for gate in registry.gates}
        stable_time = {gate.name: vehicle_points[0].time for gate in registry.gates}
        inside = False
        first_event: Optional[str] = None
        entries = exits = 0
        inside_km = outside_km = total_km = 0.0
        inside_seconds = moving_seconds = stopped_seconds = 0.0
        stop_count = 0
        last_event_time: Optional[datetime] = None

        for p1, p2 in zip(vehicle_points, vehicle_points[1:]):
            gap = (p2.time - p1.time).total_seconds()
            if gap <= 0:
                continue
            distance = segment_distance(p1, p2)
            total_km += distance

            candidates = []
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

            candidate = min(candidates, key=lambda item: item[0]) if candidates else None
            if candidate:
                fraction, kind = candidate
                event_time = p1.time + (p2.time - p1.time) * fraction
                if last_event_time and (event_time - last_event_time).total_seconds() < EVENT_COOLDOWN_SECONDS:
                    candidate = None

            portions: list[tuple[bool, float]]
            if candidate:
                fraction, kind = candidate
                if first_event is None:
                    first_event = kind
                    if kind == "Выезд":
                        inside = True
                if kind == "Въезд":
                    portions = [(False, fraction), (True, 1.0 - fraction)]
                    if not inside:
                        entries += 1
                        inside = True
                        last_event_time = p1.time + (p2.time - p1.time) * fraction
                else:
                    portions = [(True, fraction), (False, 1.0 - fraction)]
                    if inside:
                        exits += 1
                        inside = False
                        last_event_time = p1.time + (p2.time - p1.time) * fraction
            else:
                portions = [(inside, 1.0)]

            for is_inside, fraction in portions:
                if fraction <= 0:
                    continue
                part_distance = distance * fraction
                part_seconds = gap * fraction
                if is_inside:
                    inside_km += part_distance
                    inside_seconds += part_seconds
                    if gap > STOP_GAP_SECONDS:
                        stopped_seconds += part_seconds
                    else:
                        moving_seconds += part_seconds
                else:
                    outside_km += part_distance

            if gap > STOP_GAP_SECONDS and any(is_inside and fraction > 0 for is_inside, fraction in portions):
                zone_before = locate_zone(p1, registry.zones)
                zone_after = locate_zone(p2, registry.zones)
                if zone_before and zone_before == zone_after:
                    zone = zone_before
                    confidence = "Высокая"
                elif zone_before or zone_after:
                    zone = zone_before or zone_after
                    confidence = "Средняя"
                else:
                    zone = "Вне зарегистрированных геозон"
                    confidence = "Низкая"
                stop_count += 1
                stops.append(
                    {
                        "date": p1.time.date(),
                        "plate": plate,
                        "start": p1.time,
                        "end": p2.time,
                        "seconds": gap,
                        "zone": zone,
                        "confidence": confidence,
                        "lat_before": p1.lat,
                        "lon_before": p1.lon,
                        "lat_after": p2.lat,
                        "lon_after": p2.lon,
                    }
                )

        inside_percent = inside_km / total_km if total_km else 0.0
        summaries.append(
            {
                "plate": plate,
                "first_time": vehicle_points[0].time,
                "last_time": vehicle_points[-1].time,
                "start_state": "Внутри" if first_event == "Выезд" else "Снаружи",
                "end_state": "Внутри" if inside else "Снаружи",
                "entries": entries,
                "exits": exits,
                "inside_km": inside_km,
                "outside_km": outside_km,
                "total_km": total_km,
                "inside_percent": inside_percent,
                "outside_percent": 1.0 - inside_percent if total_km else 0.0,
                "inside_seconds": inside_seconds,
                "moving_seconds": moving_seconds,
                "stopped_seconds": stopped_seconds,
                "stop_count": stop_count,
                "moving_percent": moving_seconds / inside_seconds if inside_seconds else 0.0,
                "stopped_percent": stopped_seconds / inside_seconds if inside_seconds else 0.0,
            }
        )

    return summaries, stops


def analyze_by_day(points: list[Point], registry: Registry) -> tuple[dict[date, list[dict[str, Any]]], list[dict[str, Any]]]:
    grouped: dict[date, list[Point]] = defaultdict(list)
    for point in points:
        grouped[point.time.date()].append(point)

    daily: dict[date, list[dict[str, Any]]] = {}
    stops: list[dict[str, Any]] = []
    for day in sorted(grouped):
        summaries, day_stops = analyze_day(grouped[day], registry)
        daily[day] = summaries
        stops.extend(day_stops)
    return daily, stops
