#!/usr/bin/env python3
"""Offline checks for cache watermarks and nullable report values."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from business_rules import CONSOLIDATED_CALCULATION_VERSION
from cache_freshness import cache_day_is_fresh
from consolidated_cache import _as_optional_float


NOW = datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc)


def ready_row() -> dict:
    return {
        "status": "SUCCESS",
        "row_count": 207,
        "cached_gps_max_event_time": NOW,
        "cached_gps_max_received_at": NOW,
        "cached_distance_max_fetched_at": NOW,
        "cached_roster_loaded_at": NOW,
        "cached_geofence_updated_at": NOW,
        "cached_calculation_version": CONSOLIDATED_CALCULATION_VERSION,
        "cached_source_vehicle_count": 207,
        "source_gps_max_event_time": NOW,
        "source_gps_max_received_at": NOW,
        "source_distance_max_fetched_at": NOW,
        "source_roster_loaded_at": NOW,
        "source_geofence_updated_at": NOW,
        "source_vehicle_count": 207,
    }


def main() -> None:
    row = ready_row()
    assert cache_day_is_fresh(row)

    late_gps = dict(row, source_gps_max_received_at=NOW + timedelta(seconds=1))
    assert not cache_day_is_fresh(late_gps)

    refreshed_distance = dict(
        row, source_distance_max_fetched_at=NOW + timedelta(seconds=1)
    )
    assert not cache_day_is_fresh(refreshed_distance)

    replaced_roster = dict(row, source_roster_loaded_at=NOW + timedelta(seconds=1))
    assert not cache_day_is_fresh(replaced_roster)

    changed_geofence = dict(row, source_geofence_updated_at=NOW + timedelta(seconds=1))
    assert not cache_day_is_fresh(changed_geofence)

    old_algorithm = dict(row, cached_calculation_version="legacy")
    assert not cache_day_is_fresh(old_algorithm)

    extra_vehicle = dict(row, source_vehicle_count=208)
    assert not cache_day_is_fresh(extra_vehicle)

    empty = dict(row)
    empty.update(
        status="EMPTY",
        row_count=0,
        cached_source_vehicle_count=0,
        source_vehicle_count=0,
        cached_gps_max_event_time=None,
        cached_gps_max_received_at=None,
        source_gps_max_event_time=None,
        source_gps_max_received_at=None,
    )
    assert cache_day_is_fresh(empty)

    assert _as_optional_float(None) is None
    assert _as_optional_float("") is None
    assert _as_optional_float("bad") is None
    assert _as_optional_float("0") == 0.0

    print("OK: cache freshness and nullable worked-hours semantics verified")


if __name__ == "__main__":
    main()
