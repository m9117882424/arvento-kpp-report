#!/usr/bin/env python3
"""Offline repository checks for transactional SQL migrations."""
from __future__ import annotations

import inspect

from database_migrations import (
    _validate_applied_migration,
    discover_migrations,
    migration_checksum,
    run_migrations,
)
import portal_entrypoint


def main() -> None:
    migrations = discover_migrations()
    assert migrations, "Должна существовать хотя бы одна миграция"
    assert [path.name for path in migrations] == [
        "001_cache_freshness.sql",
        "002_operational_geofences.sql",
        "003_roster_cache_policy.sql",
        "004_efficiency_gps_daily_metrics.sql",
        "005_geofence_independent_checks.sql",
        "006_efficiency_gps_metric_variants.sql",
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
    assert "row_factory=psycopg.rows.tuple_row" in inspect.getsource(run_migrations)
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
    metric_sql = migrations[3].read_text(encoding="utf-8")
    assert "efficiency_gps_daily_metrics" in metric_sql
    assert "metrics JSONB NOT NULL" in metric_sql
    assert "UNIQUE (report_day, normalized_plate)" in metric_sql
    checks_sql = migrations[4].read_text(encoding="utf-8")
    for column in ("parking_enabled", "allowed_area_enabled", "speed_rule", "speed_threshold_kmh"):
        assert column in checks_sql
    assert "UPDATE geofences" in checks_sql
    # PostgreSQL CHECK accepts UNKNOWN (NULL); explicit non-NULL is required.
    assert "speed_threshold_kmh IS NOT NULL" in checks_sql
    variants_sql = migrations[5].read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS efficiency_gps_metric_variants" in variants_sql
    assert "cache_key TEXT PRIMARY KEY" in variants_sql
    assert "UNIQUE (report_day, normalized_plate)" not in variants_sql
    assert "FROM efficiency_gps_daily_metrics" in variants_sql
    assert "ON CONFLICT (cache_key) DO NOTHING" in variants_sql
    print(f"OK: versioned migrations registered; files={len(migrations)}")


if __name__ == "__main__":
    main()
