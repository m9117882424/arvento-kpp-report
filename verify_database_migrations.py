#!/usr/bin/env python3
"""Offline repository checks for transactional SQL migrations."""
from __future__ import annotations

from database_migrations import discover_migrations, migration_checksum
import portal_entrypoint


def main() -> None:
    migrations = discover_migrations()
    assert migrations, "Должна существовать хотя бы одна миграция"
    assert [path.name for path in migrations] == [
        "001_cache_freshness.sql",
        "002_operational_geofences.sql",
    ]
    assert len(migration_checksum(migrations[0])) == 64
    sql = migrations[0].read_text(encoding="utf-8")
    for column in (
        "gps_max_received_at",
        "distance_max_fetched_at",
        "roster_loaded_at",
        "geofence_updated_at",
        "calculation_version",
        "source_vehicle_count",
    ):
        assert column in sql
    assert portal_entrypoint.app.state.database_migrations_registered is True
    geofence_sql = migrations[1].read_text(encoding="utf-8")
    assert "geofence_versions" in geofence_sql
    print(f"OK: versioned migrations registered; files={len(migrations)}")


if __name__ == "__main__":
    main()
