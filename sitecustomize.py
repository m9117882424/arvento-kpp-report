from __future__ import annotations

"""Apply configured speed-exclusion geozones to all report engines.

Python imports ``sitecustomize`` automatically during application startup. The
patch keeps GPS points in the track for mileage, entry/exit and work-time
calculations, but replaces their speed with ``None`` while they are inside a
geozone whose purpose is ``speed_exclusion``.
"""

from copy import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence


def _install() -> None:
    try:
        import consolidated_report
        import speed_violation_report
        from geozone_registry import load_registry, point_in_zone
    except Exception:
        return

    registry_path = Path(__file__).resolve().parent / "geozones.json"
    try:
        registry = load_registry(registry_path)
    except Exception:
        return
    exclusion_zones = [
        zone for zone in registry.zones if zone.purpose == "speed_exclusion"
    ]
    if not exclusion_zones:
        return

    def excluded(point: Any) -> bool:
        try:
            lat = float(point.lat)
            lon = float(point.lon)
        except (AttributeError, TypeError, ValueError):
            return False
        return any(point_in_zone(lat, lon, zone) for zone in exclusion_zones)

    def without_speed(point: Any) -> Any:
        if not excluded(point):
            return point
        try:
            cloned = copy(point)
            cloned.speed = None
            return cloned
        except Exception:
            values = dict(getattr(point, "__dict__", {}))
            values.update(
                plate=getattr(point, "plate", ""),
                timestamp=getattr(point, "timestamp", getattr(point, "time", None)),
                time=getattr(point, "time", getattr(point, "timestamp", None)),
                lat=getattr(point, "lat", None),
                lon=getattr(point, "lon", None),
                speed=None,
                address=getattr(point, "address", ""),
            )
            return SimpleNamespace(**values)

    original_detect = speed_violation_report.detect_speed_violations
    if not getattr(original_detect, "_speed_exclusion_installed", False):
        def detect_speed_violations(
            track: Sequence[Any],
            registry: Any,
            site_threshold_kmh: float = speed_violation_report.DEFAULT_SITE_SPEED_THRESHOLD_KMH,
            outside_threshold_kmh: float = speed_violation_report.DEFAULT_OUTSIDE_SPEED_THRESHOLD_KMH,
        ):
            filtered = [without_speed(point) for point in track]
            return original_detect(
                filtered,
                registry,
                site_threshold_kmh=site_threshold_kmh,
                outside_threshold_kmh=outside_threshold_kmh,
            )
        detect_speed_violations._speed_exclusion_installed = True
        speed_violation_report.detect_speed_violations = detect_speed_violations

    original_analyze = consolidated_report.analyze_track
    if not getattr(original_analyze, "_speed_exclusion_installed", False):
        def analyze_track(day, display_plate, points, roster, site_polygon, route_polygon):
            filtered = [without_speed(point) for point in points]
            return original_analyze(
                day,
                display_plate,
                filtered,
                roster,
                site_polygon,
                route_polygon,
            )
        analyze_track._speed_exclusion_installed = True
        consolidated_report.analyze_track = analyze_track


_install()
