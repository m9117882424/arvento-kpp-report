#!/usr/bin/env python3
"""Synchronize Arvento GPS data into PostgreSQL/PostGIS.

This is the canonical server entrypoint. Legacy implementation modules remain
available for backward compatibility, but production dependencies are wired to
the canonical task-oriented API client and parser here.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import psycopg
from psycopg import sql

from arvento_api_client import (
    HEADERS,
    build_general_report_params,
    fetch_general_report_chunk,
)
from parse_arvento_general_report import parse_general_report_rows
import arvento_postgres_sync_v2 as implementation


def ensure_partition(conn: psycopg.Connection, day) -> None:
    """Create a daily partition and its indexes.

    PostgreSQL does not accept bind parameters in a partition-bound DDL clause,
    therefore the validated datetime values are composed as SQL literals.
    """
    start = datetime.combine(day, datetime.min.time(), implementation.TZ)
    end = start + timedelta(days=1)
    name = f"gps_points_{day:%Y_%m_%d}"

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "CREATE TABLE IF NOT EXISTS {} PARTITION OF gps_points "
                "FOR VALUES FROM ({}) TO ({})"
            ).format(
                sql.Identifier(name),
                sql.Literal(start),
                sql.Literal(end),
            )
        )
        cur.execute(
            sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (normalized_plate, event_time)").format(
                sql.Identifier(f"ix_{name}_plate_time"),
                sql.Identifier(name),
            )
        )
        cur.execute(
            sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} USING GIST (position)").format(
                sql.Identifier(f"ix_{name}_position"),
                sql.Identifier(name),
            )
        )
        cur.execute(
            sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (region_name)").format(
                sql.Identifier(f"ix_{name}_region"),
                sql.Identifier(name),
            )
        )


# Canonical dependencies are injected into the backward-compatible implementation.
implementation.HEADERS = HEADERS
implementation.build_params = build_general_report_params
implementation.fetch_chunk = fetch_general_report_chunk
implementation.parse_rows = parse_general_report_rows
implementation.ensure_partition = ensure_partition


if __name__ == "__main__":
    implementation.main()
