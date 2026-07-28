from __future__ import annotations

"""Process-wide report rules loaded by Python during interpreter startup.

Only calculation-neutral infrastructure is installed here:

* tunnel speeds are hidden from legacy speed engines and the consolidated
  report without removing GPS points from mileage/time calculations;
* measurable Excel indicators are stored and displayed with one decimal.

Portal behaviour is deliberately handled by ``portal_entrypoint.py`` instead
of importing the web application from ``sitecustomize``.
"""

from copy import copy
from dataclasses import is_dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence


def _install_speed_exclusions() -> None:
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
        """Clone a GPS point with speed removed, preserving mileage fields."""
        if not excluded(point):
            return point

        if is_dataclass(point):
            try:
                return replace(point, speed=None)
            except (TypeError, ValueError):
                pass

        try:
            cloned = copy(point)
            cloned.speed = None
            return cloned
        except Exception:
            pass

        values = dict(getattr(point, "__dict__", {}))
        for name in (
            "plate",
            "timestamp",
            "time",
            "lat",
            "lon",
            "odometer",
            "source_distance",
            "speed",
            "region",
            "address",
        ):
            if name not in values and hasattr(point, name):
                values[name] = getattr(point, name)
        if "timestamp" not in values and "time" in values:
            values["timestamp"] = values["time"]
        if "time" not in values and "timestamp" in values:
            values["time"] = values["timestamp"]
        values["speed"] = None
        return SimpleNamespace(**values)

    original_detect = speed_violation_report.detect_speed_violations
    if not getattr(original_detect, "_speed_exclusion_installed", False):
        def detect_speed_violations(
            track: Sequence[Any],
            registry: Any,
            site_threshold_kmh: float = speed_violation_report.DEFAULT_SITE_SPEED_THRESHOLD_KMH,
            outside_threshold_kmh: float = speed_violation_report.DEFAULT_OUTSIDE_SPEED_THRESHOLD_KMH,
        ):
            return original_detect(
                [without_speed(point) for point in track],
                registry,
                site_threshold_kmh=site_threshold_kmh,
                outside_threshold_kmh=outside_threshold_kmh,
            )

        detect_speed_violations._speed_exclusion_installed = True
        speed_violation_report.detect_speed_violations = detect_speed_violations

    original_analyze = consolidated_report.analyze_track
    if not getattr(original_analyze, "_speed_exclusion_installed", False):
        def analyze_track(day, display_plate, points, roster, site_polygon, route_polygon):
            return original_analyze(
                day,
                display_plate,
                [without_speed(point) for point in points],
                roster,
                site_polygon,
                route_polygon,
            )

        analyze_track._speed_exclusion_installed = True
        consolidated_report.analyze_track = analyze_track


def _install_excel_rounding() -> None:
    try:
        from openpyxl.styles.numbers import is_date_format
        from openpyxl.workbook.workbook import Workbook
    except Exception:
        return

    original_save = Workbook.save
    if getattr(original_save, "_one_decimal_installed", False):
        return

    coordinate_tokens = (
        "координат",
        "latitude",
        "longitude",
        "широт",
        "долгот",
        "latitudine",
        "enlem",
        "boylam",
    )

    def detect_header_row(sheet) -> int:
        upper = min(max(sheet.max_row, 1), 10)
        candidates: list[tuple[int, int]] = []
        for row_number in range(1, upper + 1):
            populated = sum(
                1
                for cell in sheet[row_number]
                if cell.value not in (None, "")
            )
            candidates.append((populated, row_number))
        return max(candidates, default=(0, 1))[1]

    def apply_rounding(workbook: Workbook) -> None:
        for sheet in workbook.worksheets:
            header_row = detect_header_row(sheet)
            headers = {
                column: str(sheet.cell(header_row, column).value or "").strip().casefold()
                for column in range(1, sheet.max_column + 1)
            }
            for row in sheet.iter_rows(min_row=header_row + 1):
                for cell in row:
                    value = cell.value
                    if not isinstance(value, float):
                        continue
                    header = headers.get(cell.column, "")
                    if any(token in header for token in coordinate_tokens):
                        continue
                    number_format = str(cell.number_format or "General")
                    lowered_format = number_format.casefold()
                    if is_date_format(number_format) or any(
                        token in lowered_format
                        for token in ("yy", "dd", "hh", "ss", "[h]")
                    ):
                        continue
                    if "%" in number_format:
                        cell.number_format = "0.0%"
                    else:
                        cell.value = round(value, 1)
                        cell.number_format = "0.0"

    def save_with_rounding(self: Workbook, filename) -> None:
        apply_rounding(self)
        original_save(self, filename)

    save_with_rounding._one_decimal_installed = True
    Workbook.save = save_with_rounding


_install_speed_exclusions()
_install_excel_rounding()
