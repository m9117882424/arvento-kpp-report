from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from arvento_io import Point


@dataclass
class Gate:
    name: str
    p1: tuple[float, float]
    p2: tuple[float, float]


@dataclass
class Geozone:
    name: str
    zone_type: str
    center: Optional[tuple[float, float]] = None
    radius_m: Optional[float] = None
    points: Optional[list[tuple[float, float]]] = None


@dataclass
class Registry:
    gates: list[Gate]
    zones: list[Geozone]


def load_registry(path: Path) -> Registry:
    if not path.exists():
        raise ValueError(f"Не найден файл настроек геозон: {path}")
    data = json.loads(path.read_text(encoding="utf-8-sig"))

    gates = []
    for item in data.get("gates", []):
        if item.get("enabled", True):
            gates.append(
                Gate(
                    name=str(item.get("name", "КПП")),
                    p1=tuple(item["point1"]),
                    p2=tuple(item["point2"]),
                )
            )

    zones = []
    for item in data.get("geozones", []):
        if not item.get("enabled", True):
            continue
        zone_type = str(item.get("type", "")).lower()
        if zone_type == "circle":
            zones.append(
                Geozone(
                    name=str(item.get("name", "Без названия")),
                    zone_type="circle",
                    center=tuple(item["center"]),
                    radius_m=float(item["radius_m"]),
                )
            )
        elif zone_type == "polygon":
            points = [tuple(point) for point in item.get("points", [])]
            if len(points) >= 3:
                zones.append(
                    Geozone(
                        name=str(item.get("name", "Без названия")),
                        zone_type="polygon",
                        points=points,
                    )
                )

    if not gates:
        raise ValueError("В geozones.json нет включённых КПП")
    return Registry(gates=gates, zones=zones)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371008.8
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    value = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def point_in_polygon(lat: float, lon: float, polygon: list[tuple[float, float]]) -> bool:
    inside = False
    j = len(polygon) - 1
    for i, (yi, xi) in enumerate(polygon):
        yj, xj = polygon[j]
        intersects = ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside


def locate_zone(point: Point, zones: list[Geozone]) -> Optional[str]:
    for zone in zones:
        if zone.zone_type == "circle" and zone.center and zone.radius_m is not None:
            if haversine_m(point.lat, point.lon, zone.center[0], zone.center[1]) <= zone.radius_m:
                return zone.name
        elif zone.zone_type == "polygon" and zone.points:
            if point_in_polygon(point.lat, point.lon, zone.points):
                return zone.name
    return None
