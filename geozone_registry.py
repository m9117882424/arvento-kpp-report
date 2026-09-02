from __future__ import annotations

import json
import logging
import math
import os
from copy import copy
from dataclasses import is_dataclass, replace
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

import psycopg
import psycopg.rows

from arvento_io import Point

SITE_BOUNDARY_PURPOSE = "site_boundary"
LOGGER = logging.getLogger(__name__)


@dataclass
class Gate:
    name: str
    p1: tuple[float, float]
    p2: tuple[float, float]


@dataclass
class Geozone:
    name: str
    zone_type: str
    purpose: str = ""
    center: Optional[tuple[float, float]] = None
    radius_m: Optional[float] = None
    points: Optional[list[tuple[float, float]]] = None


@dataclass
class Registry:
    gates: list[Gate]
    zones: list[Geozone]
    route_polygon: Optional[list[tuple[float, float]]] = None
    source: str = "static"
    version_refs: tuple[str, ...] = ()


def load_static_registry(path: Path) -> Registry:
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
        purpose = str(item.get("purpose", "")).strip().lower()
        if zone_type == "circle":
            zones.append(
                Geozone(
                    name=str(item.get("name", "Без названия")),
                    zone_type="circle",
                    purpose=purpose,
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
                        purpose=purpose,
                        points=points,
                    )
                )

    if not gates:
        raise ValueError("В geozones.json нет включённых КПП")
    return Registry(gates=gates, zones=zones, source=f"static:{path.name}")


def _polygon_parts(geometry: dict) -> list[list[tuple[float, float]]]:
    geometry_type = str(geometry.get("type", ""))
    coordinates = geometry.get("coordinates") or []
    if geometry_type == "Polygon":
        coordinate_parts = [coordinates]
    elif geometry_type == "MultiPolygon":
        coordinate_parts = coordinates
    else:
        return []

    result: list[list[tuple[float, float]]] = []
    for polygon in coordinate_parts:
        exterior = polygon[0] if polygon else []
        points = [(float(lat), float(lon)) for lon, lat, *_rest in exterior]
        if len(points) >= 2 and points[0] == points[-1]:
            points.pop()
        if len(points) >= 3:
            result.append(points)
    return result


def load_database_registry(database_url: str) -> Registry:
    """Build the operational registry from active versioned PostGIS features."""
    with psycopg.connect(database_url, row_factory=psycopg.rows.dict_row) as connection:
        rows = list(
            connection.execute(
                """
                SELECT g.code, g.name, g.geofence_type, v.id AS version_id,
                       v.version, ST_AsGeoJSON(v.geometry)::json AS geometry
                FROM geofences AS g
                JOIN geofence_versions AS v
                  ON v.geofence_id=g.id AND v.valid_to IS NULL
                WHERE g.is_active
                ORDER BY g.geofence_type, g.code
                """
            ).fetchall()
        )

    gates: list[Gate] = []
    zones: list[Geozone] = []
    routes: list[list[tuple[float, float]]] = []
    versions: list[str] = []
    for row in rows:
        feature_type = str(row["geofence_type"] or "").strip().upper()
        geometry = row["geometry"] or {}
        geometry_type = str(geometry.get("type", ""))
        coordinates = geometry.get("coordinates") or []
        versions.append(f"{row['code']}@{row['version']}#{row['version_id']}")

        if feature_type == "GATE" and geometry_type == "LineString" and len(coordinates) >= 2:
            first, last = coordinates[0], coordinates[-1]
            gates.append(
                Gate(
                    name=str(row["name"]),
                    p1=(float(first[1]), float(first[0])),
                    p2=(float(last[1]), float(last[0])),
                )
            )
            continue

        parts = _polygon_parts(geometry)
        if feature_type == "ROUTE":
            routes.extend(parts)
            continue

        if feature_type == "SITE":
            purpose = SITE_BOUNDARY_PURPOSE
        elif feature_type in {"SPEED_EXCLUSION", "TUNNEL"}:
            purpose = "speed_exclusion"
        else:
            purpose = feature_type.casefold()
        for index, points in enumerate(parts, start=1):
            name = str(row["name"])
            if len(parts) > 1:
                name = f"{name} ({index})"
            zones.append(
                Geozone(
                    name=name,
                    zone_type="polygon",
                    purpose=purpose,
                    points=points,
                )
            )

    registry = Registry(
        gates=gates,
        zones=zones,
        route_polygon=routes[0] if len(routes) == 1 else None,
        source="database",
        version_refs=tuple(versions),
    )
    find_site_boundary(registry)
    if not registry.gates:
        raise ValueError("В PostGIS нет активных линий КПП с типом GATE")
    if len(routes) != 1:
        raise ValueError("В PostGIS должна быть ровно одна активная зона ROUTE")
    return registry


def load_registry(path: Path) -> Registry:
    """Load PostGIS operational zones when complete, otherwise use static files."""
    source_mode = os.environ.get("GEOFENCE_SOURCE", "auto").strip().casefold()
    if source_mode not in {"auto", "database", "static"}:
        raise ValueError("GEOFENCE_SOURCE должен быть auto, database или static")
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if source_mode != "static" and database_url:
        try:
            return load_database_registry(database_url)
        except (psycopg.Error, ValueError, TypeError, KeyError, IndexError) as exc:
            if source_mode == "database":
                raise ValueError(f"Не удалось загрузить обязательные геозоны PostGIS: {exc}") from exc
            LOGGER.warning("PostGIS geofences are incomplete; using static registry: %s", exc)
    return load_static_registry(path)


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


def point_in_zone(lat: float, lon: float, zone: Geozone) -> bool:
    if zone.zone_type == "circle" and zone.center and zone.radius_m is not None:
        return haversine_m(lat, lon, zone.center[0], zone.center[1]) <= zone.radius_m
    if zone.zone_type == "polygon" and zone.points:
        return point_in_polygon(lat, lon, zone.points)
    return False


def suppress_speed_in_exclusions(
    track: Sequence[Any], registry: Registry
) -> list[Any]:
    """Clone tunnel points with speed hidden while preserving mileage fields."""
    exclusions = [zone for zone in registry.zones if zone.purpose == "speed_exclusion"]
    if not exclusions:
        return list(track)
    result: list[Any] = []
    for point in track:
        excluded = any(
            point_in_zone(float(point.lat), float(point.lon), zone)
            for zone in exclusions
        )
        if not excluded:
            result.append(point)
            continue
        if is_dataclass(point):
            result.append(replace(point, speed=None))
        else:
            cloned = copy(point)
            cloned.speed = None
            result.append(cloned)
    return result


def find_site_boundary(registry: Registry) -> Geozone:
    matches = [zone for zone in registry.zones if zone.purpose == SITE_BOUNDARY_PURPOSE]
    if not matches:
        raise ValueError(
            "Не задана активная геозона площадки с назначением site_boundary/SITE"
        )
    if len(matches) > 1:
        names = ", ".join(zone.name for zone in matches)
        raise ValueError(f"Найдено несколько границ площадки: {names}")
    zone = matches[0]
    if zone.zone_type != "polygon" or not zone.points or len(zone.points) < 3:
        raise ValueError("Граница площадки должна быть полигоном минимум из трёх точек")
    return zone


def is_inside_site(point: Point, registry: Registry) -> bool:
    return point_in_zone(point.lat, point.lon, find_site_boundary(registry))


def locate_zone(point: Point, zones: list[Geozone]) -> Optional[str]:
    """Locate a business/parking zone, excluding the overall site boundary."""
    for zone in zones:
        if zone.purpose == SITE_BOUNDARY_PURPOSE:
            continue
        if point_in_zone(point.lat, point.lon, zone):
            return zone.name
    return None
