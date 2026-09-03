#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Source-watermark checks for the consolidated daily cache."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import psycopg.rows

from business_rules import CONSOLIDATED_CALCULATION_VERSION, TIMEZONE_NAME


TZ = ZoneInfo(TIMEZONE_NAME)


@dataclass(frozen=True, slots=True)
class SourceWatermark:
    gps_max_event_time: datetime | None
    gps_max_received_at: datetime | None
    distance_max_fetched_at: datetime | None
    roster_loaded_at: datetime | None
    geofence_updated_at: datetime | None
    source_vehicle_count: int


@dataclass(frozen=True, slots=True)
class CacheCoverage:
    expected_days: int
    ready_days: int
    stale_days: tuple[date, ...]
    stale_reasons: tuple[tuple[date, tuple[str, ...]], ...] = ()

    @property
    def complete(self) -> bool:
        return self.ready_days == self.expected_days


def load_source_watermark(
    cursor: psycopg.Cursor[Any], report_day: date
) -> SourceWatermark:
    start_at = datetime.combine(report_day, time.min, tzinfo=TZ)
    finish_at = start_at + timedelta(days=1)
    cursor.execute(
        """
        SELECT MAX(event_time), MAX(received_at),
               COUNT(DISTINCT normalized_plate)
        FROM gps_points
        WHERE event_time >= %s AND event_time < %s
        """,
        (start_at, finish_at),
    )
    gps_max_event, gps_max_received, vehicle_count = cursor.fetchone()
    cursor.execute(
        """
        SELECT MAX(fetched_at)
        FROM vehicle_distance_daily
        WHERE report_day=%s
        """,
        (report_day,),
    )
    distance_max_fetched = cursor.fetchone()[0]
    cursor.execute(
        """
        SELECT loaded_at
        FROM consolidated_roster_snapshots
        WHERE roster_day <= %s
        ORDER BY roster_day DESC
        LIMIT 1
        """,
        (report_day,),
    )
    roster_row = cursor.fetchone()
    cursor.execute(
        """
        SELECT MAX(GREATEST(g.updated_at, v.created_at))
        FROM geofences AS g
        JOIN geofence_versions AS v
          ON v.geofence_id=g.id AND v.valid_to IS NULL
        WHERE g.is_active
          AND upper(g.geofence_type) = ANY(%s::text[])
        """,
        (["GATE", "ROUTE", "SITE", "SPEED_EXCLUSION"],),
    )
    geofence_updated_at = cursor.fetchone()[0]
    return SourceWatermark(
        gps_max_event_time=gps_max_event,
        gps_max_received_at=gps_max_received,
        distance_max_fetched_at=distance_max_fetched,
        roster_loaded_at=roster_row[0] if roster_row else None,
        geofence_updated_at=geofence_updated_at,
        source_vehicle_count=int(vehicle_count or 0),
    )


def _covers(cached: datetime | None, source: datetime | None) -> bool:
    return source is None or (cached is not None and cached >= source)


def cache_day_stale_reasons(row: dict[str, Any]) -> tuple[str, ...]:
    """Explain why one cached day cannot be reused.

    Roster revisions are intentionally not a cache-invalidation reason. Roster
    attributes are lightweight reference data and are overlaid from the current
    central roster when cached GPS metrics are rendered.
    """
    reasons: list[str] = []
    source_vehicle_count = int(row.get("source_vehicle_count") or 0)
    row_count = int(row.get("row_count") or 0)
    status = str(row.get("status") or "")

    if source_vehicle_count == 0:
        status_matches = status == "EMPTY" and row_count == 0
    else:
        status_matches = status == "SUCCESS" and row_count > 0
    if not status_matches:
        reasons.append("status")

    # A central roster must exist for the report date, but replacing that roster
    # does not invalidate expensive GPS/geofence calculations.
    if row.get("source_roster_loaded_at") is None:
        reasons.append("missing_roster")
    if row.get("cached_calculation_version") != CONSOLIDATED_CALCULATION_VERSION:
        reasons.append("algorithm")
    if int(row.get("cached_source_vehicle_count") or 0) != source_vehicle_count:
        reasons.append("vehicle_count")

    if not _covers(
        row.get("cached_gps_max_event_time"),
        row.get("source_gps_max_event_time"),
    ):
        reasons.append("gps_event")
    if not _covers(
        row.get("cached_gps_max_received_at"),
        row.get("source_gps_max_received_at"),
    ):
        reasons.append("gps_received")
    if not _covers(
        row.get("cached_distance_max_fetched_at"),
        row.get("source_distance_max_fetched_at"),
    ):
        reasons.append("vehicle_distance")
    if not _covers(
        row.get("cached_geofence_updated_at"),
        row.get("source_geofence_updated_at"),
    ):
        reasons.append("geofence")

    return tuple(reasons)


def cache_day_is_fresh(row: dict[str, Any]) -> bool:
    """Return whether one cache-day row covers the current calculation sources."""
    return not cache_day_stale_reasons(row)


def load_cache_coverage(
    connection: psycopg.Connection[Any], start_day: date, end_day: date
) -> CacheCoverage:
    """Compare stored cache watermarks with current heavy calculation sources."""
    query = """
        WITH requested AS (
            SELECT generate_series(%s::date, %s::date, interval '1 day')::date AS report_day
        ), gps AS (
            SELECT
                (event_time AT TIME ZONE 'Europe/Istanbul')::date AS report_day,
                MAX(event_time) AS max_event_time,
                MAX(received_at) AS max_received_at,
                COUNT(DISTINCT normalized_plate) AS vehicle_count
            FROM gps_points
            WHERE event_time >= (%s::date::timestamp AT TIME ZONE 'Europe/Istanbul')
              AND event_time < ((%s::date + 1)::timestamp AT TIME ZONE 'Europe/Istanbul')
            GROUP BY 1
        ), distance AS (
            SELECT report_day, MAX(fetched_at) AS max_fetched_at
            FROM vehicle_distance_daily
            WHERE report_day BETWEEN %s AND %s
            GROUP BY report_day
        )
        SELECT
            requested.report_day,
            cache.status,
            cache.row_count,
            cache.gps_max_event_time AS cached_gps_max_event_time,
            cache.gps_max_received_at AS cached_gps_max_received_at,
            cache.distance_max_fetched_at AS cached_distance_max_fetched_at,
            cache.roster_loaded_at AS cached_roster_loaded_at,
            cache.geofence_updated_at AS cached_geofence_updated_at,
            cache.calculation_version AS cached_calculation_version,
            cache.source_vehicle_count AS cached_source_vehicle_count,
            gps.max_event_time AS source_gps_max_event_time,
            gps.max_received_at AS source_gps_max_received_at,
            distance.max_fetched_at AS source_distance_max_fetched_at,
            roster.loaded_at AS source_roster_loaded_at,
            geofence.updated_at AS source_geofence_updated_at,
            COALESCE(gps.vehicle_count, 0) AS source_vehicle_count
        FROM requested
        LEFT JOIN consolidated_cache_days AS cache USING (report_day)
        LEFT JOIN gps USING (report_day)
        LEFT JOIN distance USING (report_day)
        LEFT JOIN LATERAL (
            SELECT loaded_at
            FROM consolidated_roster_snapshots
            WHERE roster_day <= requested.report_day
            ORDER BY roster_day DESC
            LIMIT 1
        ) AS roster ON TRUE
        LEFT JOIN LATERAL (
            SELECT MAX(GREATEST(g.updated_at, v.created_at)) AS updated_at
            FROM geofences AS g
            JOIN geofence_versions AS v
              ON v.geofence_id=g.id AND v.valid_to IS NULL
            WHERE g.is_active
              AND upper(g.geofence_type) = ANY(
                  ARRAY['GATE','ROUTE','SITE','SPEED_EXCLUSION']::text[]
              )
        ) AS geofence ON TRUE
        ORDER BY requested.report_day
    """
    with connection.cursor(row_factory=psycopg.rows.dict_row) as cursor:
        cursor.execute(
            query,
            (start_day, end_day, start_day, end_day, start_day, end_day),
        )
        rows = list(cursor.fetchall())

    stale_reasons = tuple(
        (row["report_day"], reasons)
        for row in rows
        if (reasons := cache_day_stale_reasons(row))
    )
    stale_days = tuple(report_day for report_day, _reasons in stale_reasons)
    return CacheCoverage(
        expected_days=len(rows),
        ready_days=len(rows) - len(stale_days),
        stale_days=stale_days,
        stale_reasons=stale_reasons,
    )


__all__ = [
    "CacheCoverage",
    "SourceWatermark",
    "cache_day_is_fresh",
    "cache_day_stale_reasons",
    "load_cache_coverage",
    "load_source_watermark",
]
