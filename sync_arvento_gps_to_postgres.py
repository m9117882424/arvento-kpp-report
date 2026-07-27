#!/usr/bin/env python3
"""Synchronize Arvento GPS data into PostgreSQL/PostGIS.

This is the canonical server entrypoint. The current implementation remains in
``arvento_postgres_sync_v2`` for backward compatibility, while deployment fixes
are applied here until the legacy module is fully retired.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import psycopg
from psycopg import sql

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


implementation.ensure_partition = ensure_partition


if __name__ == "__main__":
    implementation.main()
