#!/usr/bin/env python3
"""Persist cumulative Arvento odometer values in the GPS point store."""
from __future__ import annotations

import arvento_postgres_sync_v2 as implementation


def insert_rows_with_odometer(conn, rows, group_name: str) -> tuple[int, set[tuple[str, object]]]:
    """Insert new GPS rows and update existing rows only when values changed."""
    processed = 0
    affected: set[tuple[str, object]] = set()

    with conn.cursor() as cur:
        for row in rows:
            event_time = (
                row.timestamp.replace(tzinfo=implementation.TZ)
                if row.timestamp.tzinfo is None
                else row.timestamp
            )
            plate = implementation.normalize_plate(row.plate)
            row_hash = implementation.source_hash(row)
            implementation.ensure_partition(conn, event_time.date())

            cur.execute(
                """
                INSERT INTO gps_points (
                    device_no, plate, normalized_plate, event_time,
                    latitude, longitude, position, speed_kmh, distance_km,
                    odometer_km, address, event_type, driver, pause_duration,
                    idling_duration, ignition_duration, region_name, source_hash
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,
                    ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                )
                ON CONFLICT (source_hash, event_time) DO UPDATE SET
                    distance_km = COALESCE(EXCLUDED.distance_km, gps_points.distance_km),
                    odometer_km = COALESCE(EXCLUDED.odometer_km, gps_points.odometer_km),
                    region_name = CASE
                        WHEN COALESCE(gps_points.region_name, '') = ''
                        THEN EXCLUDED.region_name
                        ELSE gps_points.region_name
                    END
                WHERE
                    (
                        EXCLUDED.distance_km IS NOT NULL
                        AND gps_points.distance_km IS DISTINCT FROM EXCLUDED.distance_km
                    )
                    OR (
                        EXCLUDED.odometer_km IS NOT NULL
                        AND gps_points.odometer_km IS DISTINCT FROM EXCLUDED.odometer_km
                    )
                    OR (
                        COALESCE(gps_points.region_name, '') = ''
                        AND COALESCE(EXCLUDED.region_name, '') <> ''
                    )
                RETURNING 1
                """,
                (
                    row.device_no or "",
                    row.plate,
                    plate,
                    event_time,
                    row.latitude,
                    row.longitude,
                    row.longitude,
                    row.latitude,
                    row.speed,
                    row.distance,
                    getattr(row, "odometer", None),
                    row.address,
                    row.event_type,
                    row.driver,
                    row.pause_duration,
                    row.idling_duration,
                    row.ignition_duration,
                    row.region_name,
                    row_hash,
                ),
            )
            if cur.fetchone():
                processed += 1
                affected.add((plate, event_time.date()))

            cur.execute(
                """
                INSERT INTO vehicles (
                    device_no, plate, normalized_plate, driver, group_name,
                    first_seen_at, last_seen_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (device_no) DO UPDATE SET
                    plate=EXCLUDED.plate,
                    normalized_plate=EXCLUDED.normalized_plate,
                    driver=EXCLUDED.driver,
                    group_name=EXCLUDED.group_name,
                    last_seen_at=GREATEST(vehicles.last_seen_at, EXCLUDED.last_seen_at),
                    updated_at=now()
                """,
                (
                    row.device_no or None,
                    row.plate,
                    plate,
                    row.driver,
                    group_name,
                    event_time,
                    event_time,
                ),
            )

        for plate, day in affected:
            cur.execute(
                """
                INSERT INTO recalculation_queue (normalized_plate, day, reason)
                VALUES (%s,%s,'new_gps_data')
                ON CONFLICT (normalized_plate, day) DO UPDATE SET
                    reason=EXCLUDED.reason,
                    created_at=now(),
                    completed_at=NULL
                """,
                (plate, day),
            )

    return processed, affected


__all__ = ["insert_rows_with_odometer"]
