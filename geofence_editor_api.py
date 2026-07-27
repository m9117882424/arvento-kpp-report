#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
DATABASE_URL = os.environ["DATABASE_URL"]
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
DEFAULT_MAP_PROVIDER = os.environ.get("DEFAULT_MAP_PROVIDER", "google").strip().lower()
OSM_FALLBACK_ENABLED = os.environ.get("OSM_FALLBACK_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
MAP_CENTER_LAT = float(os.environ.get("MAP_CENTER_LAT", "36.145"))
MAP_CENTER_LON = float(os.environ.get("MAP_CENTER_LON", "33.535"))
MAP_DEFAULT_ZOOM = int(os.environ.get("MAP_DEFAULT_ZOOM", "15"))

app = FastAPI(title="Arvento Geofence Editor", version="1.0.0")


class GeofencePayload(BaseModel):
    code: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=255)
    geofence_type: str = Field(min_length=1, max_length=50)
    geometry: dict[str, Any]
    comment: str | None = None
    is_active: bool = True


def ensure_schema() -> None:
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute(
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
        conn.commit()


@app.on_event("startup")
def startup() -> None:
    ensure_schema()


@app.get("/")
def editor() -> FileResponse:
    return FileResponse(BASE_DIR / "web" / "geofence_editor.html")


@app.get("/health")
def health() -> dict[str, str]:
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1")
        cur.fetchone()
    return {"status": "ok"}


@app.get("/api/config")
def config() -> dict[str, Any]:
    return {
        "googleMapsApiKey": GOOGLE_MAPS_API_KEY,
        "defaultMapProvider": DEFAULT_MAP_PROVIDER,
        "osmFallbackEnabled": OSM_FALLBACK_ENABLED,
        "center": [MAP_CENTER_LAT, MAP_CENTER_LON],
        "zoom": MAP_DEFAULT_ZOOM,
    }


@app.get("/api/geofences")
def list_geofences() -> dict[str, Any]:
    with psycopg.connect(DATABASE_URL, row_factory=psycopg.rows.dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT g.id, g.code, g.name, g.geofence_type, g.is_active,
                   v.version, v.comment,
                   ST_AsGeoJSON(v.geometry)::json AS geometry
            FROM geofences g
            JOIN geofence_versions v ON v.geofence_id = g.id AND v.valid_to IS NULL
            ORDER BY g.name
            """
        )
        features = []
        for row in cur.fetchall():
            features.append({
                "type": "Feature",
                "id": row["id"],
                "properties": {
                    "id": row["id"],
                    "code": row["code"],
                    "name": row["name"],
                    "geofence_type": row["geofence_type"],
                    "is_active": row["is_active"],
                    "version": row["version"],
                    "comment": row["comment"],
                },
                "geometry": row["geometry"],
            })
    return {"type": "FeatureCollection", "features": features}


def validate_geometry(cur: psycopg.Cursor, geometry: dict[str, Any]) -> None:
    geojson = json.dumps(geometry)
    cur.execute(
        """
        SELECT ST_IsValid(g), ST_IsEmpty(g), ST_SRID(g), GeometryType(g)
        FROM (SELECT ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326) AS g) s
        """,
        (geojson,),
    )
    valid, empty, srid, geometry_type = cur.fetchone()
    if not valid or empty or srid != 4326:
        raise HTTPException(status_code=422, detail="Некорректная или пустая геометрия")
    if geometry_type not in {"POINT", "LINESTRING", "POLYGON", "MULTIPOLYGON"}:
        raise HTTPException(status_code=422, detail=f"Неподдерживаемый тип геометрии: {geometry_type}")


@app.post("/api/geofences", status_code=201)
def create_geofence(payload: GeofencePayload) -> dict[str, Any]:
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        validate_geometry(cur, payload.geometry)
        try:
            cur.execute(
                """
                INSERT INTO geofences(code, name, geofence_type, is_active)
                VALUES(%s, %s, %s, %s)
                RETURNING id
                """,
                (payload.code.strip(), payload.name.strip(), payload.geofence_type.strip().upper(), payload.is_active),
            )
            geofence_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO geofence_versions(geofence_id, version, geometry, comment)
                VALUES(%s, 1, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), %s)
                """,
                (geofence_id, json.dumps(payload.geometry), payload.comment),
            )
            conn.commit()
        except psycopg.errors.UniqueViolation as exc:
            conn.rollback()
            raise HTTPException(status_code=409, detail="Геозона с таким кодом уже существует") from exc
    return {"id": geofence_id, "version": 1}


@app.put("/api/geofences/{geofence_id}")
def update_geofence(geofence_id: int, payload: GeofencePayload) -> dict[str, Any]:
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        validate_geometry(cur, payload.geometry)
        cur.execute("SELECT id FROM geofences WHERE id=%s FOR UPDATE", (geofence_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Геозона не найдена")
        cur.execute(
            """
            UPDATE geofences
            SET code=%s, name=%s, geofence_type=%s, is_active=%s, updated_at=now()
            WHERE id=%s
            """,
            (payload.code.strip(), payload.name.strip(), payload.geofence_type.strip().upper(), payload.is_active, geofence_id),
        )
        cur.execute("UPDATE geofence_versions SET valid_to=now() WHERE geofence_id=%s AND valid_to IS NULL", (geofence_id,))
        cur.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM geofence_versions WHERE geofence_id=%s", (geofence_id,))
        version = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO geofence_versions(geofence_id, version, geometry, comment)
            VALUES(%s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), %s)
            """,
            (geofence_id, version, json.dumps(payload.geometry), payload.comment),
        )
        conn.commit()
    return {"id": geofence_id, "version": version}


@app.delete("/api/geofences/{geofence_id}", status_code=204)
def delete_geofence(geofence_id: int) -> None:
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("UPDATE geofences SET is_active=FALSE, updated_at=now() WHERE id=%s", (geofence_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Геозона не найдена")
        conn.commit()
