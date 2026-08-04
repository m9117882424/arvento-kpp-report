#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fast database-status endpoint for the production report portal.

The original endpoint calculated ``max(event_time)`` over the full partitioned
``gps_points`` table. On a multi-gigabyte production database that scanned all
partitions and blocked the single Uvicorn worker. This patch reads the latest
vehicle timestamp from the small ``vehicles`` table and scans only today's GPS
partition for the current-day point count.
"""
from __future__ import annotations

from datetime import tzinfo
from typing import Any, Callable

import psycopg
from fastapi import FastAPI
from fastapi.routing import APIRoute


DATABASE_STATUS_PATH = "/api/database-status"
DATABASE_STATUS_STATEMENT_TIMEOUT_MS = 10_000


def database_status_payload(
    database_url: str,
    timezone_value: tzinfo,
) -> dict[str, Any]:
    query = """
        WITH bounds AS (
            SELECT
                date_trunc('day', now() AT TIME ZONE 'Europe/Istanbul')
                    AT TIME ZONE 'Europe/Istanbul' AS day_start,
                (date_trunc('day', now() AT TIME ZONE 'Europe/Istanbul') + interval '1 day')
                    AT TIME ZONE 'Europe/Istanbul' AS day_end
        )
        SELECT
            (SELECT max(v.last_seen_at) FROM vehicles v) AS latest_event_time,
            now() AS database_time,
            (
                SELECT count(*)
                FROM gps_points g
                CROSS JOIN bounds
                WHERE g.event_time >= bounds.day_start
                  AND g.event_time < bounds.day_end
            ) AS today_points,
            (
                SELECT count(DISTINCT v.normalized_plate)
                FROM vehicles v
                CROSS JOIN bounds
                WHERE v.last_seen_at >= bounds.day_start
                  AND v.last_seen_at < bounds.day_end
            ) AS today_vehicles
    """

    with psycopg.connect(
        database_url,
        connect_timeout=5,
        options=f"-c statement_timeout={DATABASE_STATUS_STATEMENT_TIMEOUT_MS}",
    ) as connection:
        latest, database_time, today_points, today_vehicles = connection.execute(
            query
        ).fetchone()

    if latest is None:
        return {
            "status": "empty",
            "label": "В БД пока нет GPS-записей",
            "latest_display": None,
            "latest_date": None,
            "server_date": database_time.astimezone(timezone_value).date().isoformat(),
            "age_minutes": None,
            "today_points": int(today_points or 0),
            "today_vehicles": int(today_vehicles or 0),
        }

    latest_local = latest.astimezone(timezone_value)
    database_local = database_time.astimezone(timezone_value)
    age_minutes = max(0.0, (database_time - latest).total_seconds() / 60.0)
    if age_minutes <= 60:
        status = "fresh"
        label = "Данные актуальны"
    elif age_minutes <= 180:
        status = "warning"
        label = "Есть задержка синхронизации"
    else:
        status = "stale"
        label = "Данные давно не обновлялись"

    return {
        "status": status,
        "label": label,
        "latest_display": latest_local.strftime("%d.%m.%Y %H:%M:%S"),
        "latest_iso": latest_local.isoformat(),
        "latest_date": latest_local.date().isoformat(),
        "server_date": database_local.date().isoformat(),
        "age_minutes": round(age_minutes, 1),
        "today_points": int(today_points or 0),
        "today_vehicles": int(today_vehicles or 0),
    }


def apply_database_status_patch(
    app: FastAPI,
    db_url: Callable[[], str],
    timezone_value: tzinfo,
) -> None:
    """Replace the existing GET route without leaving duplicate handlers."""
    retained_routes = []
    removed = 0
    for route in app.router.routes:
        if (
            isinstance(route, APIRoute)
            and route.path == DATABASE_STATUS_PATH
            and "GET" in route.methods
        ):
            removed += 1
            continue
        retained_routes.append(route)

    if removed != 1:
        raise RuntimeError(
            f"Ожидался один маршрут {DATABASE_STATUS_PATH}, найдено: {removed}"
        )

    app.router.routes[:] = retained_routes

    def database_status() -> dict[str, Any]:
        return database_status_payload(db_url(), timezone_value)

    app.add_api_route(
        DATABASE_STATUS_PATH,
        database_status,
        methods=["GET"],
        name="database_status",
    )


__all__ = [
    "DATABASE_STATUS_PATH",
    "apply_database_status_patch",
    "database_status_payload",
]
