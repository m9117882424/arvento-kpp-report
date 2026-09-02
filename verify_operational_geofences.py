#!/usr/bin/env python3
"""Offline checks for static bootstrap and report-ready geofence roles."""
from __future__ import annotations

from pathlib import Path

from geozone_registry import Registry, find_site_boundary, load_static_registry
from operational_geofences import _static_features


BASE = Path(__file__).resolve().parent


def main() -> None:
    registry = load_static_registry(BASE / "geozones.json")
    assert registry.source.startswith("static:")
    assert len(registry.gates) == 2
    assert find_site_boundary(registry).name == "Площадка АЭС АККУЮ"

    features = _static_features(
        BASE / "geozones.json",
        BASE / "route_akkuyu_tasucu.kml",
    )
    types = [item[2] for item in features]
    assert types.count("SITE") == 1
    assert types.count("GATE") == 2
    assert types.count("ROUTE") == 1
    assert types.count("SPEED_EXCLUSION") == 2
    assert all(item[3]["type"] in {"LineString", "Polygon"} for item in features)

    # Registry keeps database route geometry next to gates/zones, allowing all
    # report engines to prefer the same versioned source over the legacy KML.
    route = [(36.0, 33.0), (36.0, 34.0), (37.0, 34.0)]
    database_registry = Registry(
        gates=registry.gates,
        zones=registry.zones,
        route_polygon=route,
        source="database",
        version_refs=("ROUTE@1#1",),
    )
    assert database_registry.route_polygon is route
    assert database_registry.version_refs == ("ROUTE@1#1",)
    print("OK: operational geofence bootstrap contains site, gates, route, and exclusions")


if __name__ == "__main__":
    main()
