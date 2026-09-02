#!/usr/bin/env python3
"""Offline repository checks for transactional SQL migrations."""
from __future__ import annotations

from database_migrations import (
    _validate_applied_migration,
    discover_migrations,
    migration_checksum,
)
import portal_entrypoint


def main() -> None:
    migrations = discover_migrations()
    assert migrations, "Должна существовать хотя бы одна миграция"
    assert [path.name for path in migrations] == [
        "001_cache_freshness.sql",
        "002_operational_geofences.sql",
    ]
    assert len(migration_checksum(migrations[0])) == 64
    checksum = migration_checksum(migrations[0])
    assert _validate_applied_migration(
        "001", migrations[0].name, checksum, (migrations[0].name, checksum)
    )
    assert not _validate_applied_migration("001", migrations[0].name, checksum, None)
    try:
        _validate_applied_migration("001", migrations[0].name, checksum, ("old.sql", checksum))
    except RuntimeError:
        pass
    else:
        raise AssertionError("Изменённая применённая миграция должна отклоняться")
    runner_source = (migrations[0].parents[2] / "database_migrations.py").read_text(
        encoding="utf-8"
    )
    assert "ON CONFLICT (version) DO NOTHING" in runner_source
    assert "RETURNING version" in runner_source
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
