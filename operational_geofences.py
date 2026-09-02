#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PostGIS schema and safe bootstrap for operational report geofences."""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import psycopg


OPERATIONAL_TYPES = {"SITE", "GATE", "ROUTE", "SPEED_EXCLUSION"}
POLYGON_TYPES = {"SITE", "ROUTE", "SPEED_EXCLUSION"}
ALLOWED_TYPES = OPERATIONAL_TYPES | {
    "PARKING",
    "FORBIDDEN_AREA",
    "ROAD_CORRIDOR",
    "WORK_ZONE",
    "CONTROL_POINT",
}


def ensure_geofence_schema(connection: psycopg.Connection[Any]) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS geofences (
                id BIGSERIAL PRIMARY KEY,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                geofence_type TEXT NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );

            CREATE TABLE IF NOT EXISTS geofence_versions (
                id BIGSERIAL PRIMARY KEY,
                geofence_id BIGINT NOT NULL REFERENCES geofences(id) ON DELETE CASCADE,
                version INTEGER NOT NULL,
                geometry geometry(Geometry, 4326) NOT NULL,
                valid_from TIMESTAMPTZ NOT NULL DEFAULT now(),
                valid_to TIMESTAMPTZ,
                comment TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (geofence_id, version)
            );

            CREATE INDEX IF NOT EXISTS ix_geofence_versions_geometry
            ON geofence_versions USING GIST (geometry);

            CREATE UNIQUE INDEX IF NOT EXISTS ux_geofence_versions_current
            ON geofence_versions (geofence_id)
            WHERE valid_to IS NULL;
            """
        )


def _polygon_geojson(points: list[list[float]]) -> dict[str, Any]:
    ring = [[float(lon), float(lat)] for lat, lon in points]
    if ring and ring[0] != ring[-1]:
        ring.append(list(ring[0]))
    return {"type": "Polygon", "coordinates": [ring]}


def _route_geojson(path: Path) -> dict[str, Any]:
    root = ET.parse(path).getroot()
    element = next(
        (item for item in root.iter() if item.tag.split("}")[-1] == "coordinates"),
        None,
    )
    if element is None or not (element.text or "").strip():
        raise ValueError(f"В {path.name} отсутствуют координаты маршрута")
    ring: list[list[float]] = []
    for token in (element.text or "").split():
        lon, lat, *_rest = token.split(",")
        ring.append([float(lon), float(lat)])
    if ring and ring[0] != ring[-1]:
        ring.append(list(ring[0]))
    return {"type": "Polygon", "coordinates": [ring]}


def _static_features(geozones_path: Path, route_kml: Path) -> list[tuple[str, str, str, dict]]:
    data = json.loads(geozones_path.read_text(encoding="utf-8-sig"))
    result: list[tuple[str, str, str, dict]] = []
    for index, item in enumerate(data.get("gates", []), start=1):
        if not item.get("enabled", True):
            continue
        code = f"GATE_KPP_{index}"
        name = str(item.get("name") or f"КПП {index}")
        coordinates = [
            [float(item["point1"][1]), float(item["point1"][0])],
            [float(item["point2"][1]), float(item["point2"][0])],
        ]
        result.append((code, name, "GATE", {"type": "LineString", "coordinates": coordinates}))

    exclusion_index = 0
    for item in data.get("geozones", []):
        if not item.get("enabled", True) or str(item.get("type", "")).casefold() != "polygon":
            continue
        purpose = str(item.get("purpose", "")).casefold()
        if purpose == "site_boundary":
            code, feature_type = "SITE_MAIN", "SITE"
        elif purpose == "speed_exclusion":
            exclusion_index += 1
            code, feature_type = f"SPEED_EXCLUSION_{exclusion_index}", "SPEED_EXCLUSION"
        else:
            continue
        result.append(
            (
                code,
                str(item.get("name") or code),
                feature_type,
                _polygon_geojson(item.get("points") or []),
            )
        )

    result.append(
        (
            "ROUTE_AKKUYU_TASUCU",
            "Маршрут Ташуджу - Аккую",
            "ROUTE",
            _route_geojson(route_kml),
        )
    )
    return result


def seed_static_operational_geofences(
    connection: psycopg.Connection[Any],
    geozones_path: Path,
    route_kml: Path,
) -> int:
    """Seed current static geometry only when no operational zones exist."""
    ensure_geofence_schema(connection)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM geofences WHERE upper(geofence_type) = ANY(%s::text[])",
            (sorted(OPERATIONAL_TYPES),),
        )
        if int(cursor.fetchone()[0]) > 0:
            return 0

        inserted = 0
        for code, name, feature_type, geometry in _static_features(geozones_path, route_kml):
            cursor.execute(
                """
                INSERT INTO geofences(code, name, geofence_type, is_active)
                VALUES (%s,%s,%s,TRUE)
                ON CONFLICT (code) DO NOTHING
                RETURNING id
                """,
                (code, name, feature_type),
            )
            row = cursor.fetchone()
            if row is None:
                continue
            cursor.execute(
                """
                INSERT INTO geofence_versions(geofence_id, version, geometry, comment)
                VALUES (%s,1,ST_SetSRID(ST_GeomFromGeoJSON(%s),4326),%s)
                """,
                (row[0], json.dumps(geometry), "Импортировано из production JSON/KML"),
            )
            inserted += 1
    return inserted


__all__ = [
    "ALLOWED_TYPES",
    "OPERATIONAL_TYPES",
    "POLYGON_TYPES",
    "ensure_geofence_schema",
    "seed_static_operational_geofences",
]
