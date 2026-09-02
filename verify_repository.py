#!/usr/bin/env python3
"""Static and configuration consistency checks for the repository.

The script does not contact Arvento or PostgreSQL. It validates canonical
entrypoints, cross-module integration, Python syntax, geozone JSON and route
KML before a server image is built.
"""
from __future__ import annotations

import ast
import json
import math
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent

CANONICAL_FILES = {
    "sync_arvento_gps_to_postgres.py": (
        "arvento_postgres_sync_v2",
        "arvento_api_client",
        "parse_arvento_general_report",
    ),
    "run_geofence_editor.py": ("geofence_editor_api",),
    "run_report_portal.py": (
        "report_portal",
        "generate_report_with_thresholds",
        "violation_web_preview",
        "database_status",
        "site_speed_threshold",
        "outside_speed_threshold",
        'id="plateFilter"',
        'id="dbStatus"',
        "/api/database-status",
        "google_maps_url",
        '"Нарушения"',
    ),
    "generate_kpp_summary_report.py": ("arvento_kpp_report",),
    "generate_first_entry_report.py": ("arvento_first_entry_report_fixed",),
    "generate_prohibited_left_turn_report.py": (
        "prohibited_left_turn_report",
        "regional_speed_report",
        "add_violation_map_links",
        "--site-speed-threshold",
        "--outside-speed-threshold",
    ),
    "generate_consolidated_report.py": ("consolidated_multi_report",),
    "generate_scheduled_reports.py": ("run_automated_reports",),
}

REQUIRED_SUPPORT_MODULES = {
    "portal_table_ui.py": (
        "import run_report_portal as portal",
        'id="plateFilter" type="text"',
        'id="plateSuggestions"',
        "sortColumnIndex",
        "data-sort-index",
        "plateFilter.addEventListener('input'",
        "workbook_preview_with_title_rows",
        '"Номерной знак" in values',
    ),
    "consolidated_portal.py": (
        "import portal_table_ui as ui",
        'value="consolidated">Сводный отчёт',
        'id="consolidatedRosters"',
        'name="rosters"',
        "multiple",
        "/api/generate-v3",
        "generate_multi_roster_report",
        "MAX_ROSTER_FILES",
        "report_end_date",
    ),
    "portal_runtime_patch.py": (
        "SPEED_SHEET_NAMES",
        "ROUTE_SHEET_NAME",
        "REGION_SHEET_NAME",
        "json_cell_one_decimal",
        "violation_web_preview",
        "generate_report_with_regional_summary",
        "apply_runtime_patch",
        "current.violation_web_preview = violation_web_preview",
        "current.generate_report_with_thresholds = generate_report_with_regional_summary",
    ),
    "portal_entrypoint.py": (
        "import consolidated_portal as portal",
        "from portal_runtime_patch import apply_runtime_patch",
        "from fleet_dashboard_api import apply_fleet_dashboard_api",
        "apply_runtime_patch()",
        "apply_fleet_dashboard_api(portal.app, portal.implementation.db_url)",
        "app = portal.app",
    ),
    "fleet_dashboard_api.py": (
        "FLEET_API_TOKEN",
        "FLEET_API_TOKEN_SHA256",
        '"/api/v1/fleet/dashboard"',
        '"/api/v1/fleet/vehicles/{plate}"',
        "consolidated_report_cache",
        "public.fuel_events",
        "apply_fleet_dashboard_api",
    ),
    "regional_speed_report.py": (
        "detect_regional_speed_violations",
        "classify_speed_categories",
        "append_regional_speed_sheets",
        'ROUTE_SHEET_NAME = "Скорость Ташуджу - Аккую"',
        'REGION_SHEET_NAME = "Скорость вне региона"',
        'purpose == "speed_exclusion"',
        "_round_speed_sheet",
    ),
    "sitecustomize.py": (
        "Intentionally empty",
        "must not depend on Python startup hooks",
    ),
    "excel_formatting.py": (
        "apply_one_decimal_metrics",
        "detect_header_row",
        "save_report_workbook",
        'cell.number_format = "0.0"',
        'cell.number_format = "0.0%"',
    ),
    "consolidated_multi_report.py": (
        "class DatedRoster",
        "load_rosters",
        "select_roster",
        "generate_multi_roster_report",
        "latest roster dated before",
        "Использованная разнарядка",
    ),
    "consolidated_report.py": (
        "HEADERS = [",
        "load_kml_polygon",
        "validated_speed_indices",
        "analyze_track",
        "Сводный отчет",
        "one_decimal",
        'number_format = "0.0"',
    ),
    "route_akkuyu_tasucu.kml": ("<Polygon>", "<coordinates>"),
    "arvento_first_entry_report_fixed.py": (
        "create_report_without_map_column",
        "sheet.delete_cols(5, 1)",
        "build_report_titles",
        "detect_roster_date",
        "sheet.insert_rows(1, amount=2)",
        'sheet.auto_filter.ref = f"A3:J{max_row}"',
        'sheet.freeze_panes = "A4"',
    ),
    "speed_violation_report.py": (
        "validate_speed_thresholds",
        "MIN_SPEED_EVENT_POINTS = 3",
        "MIN_SPEED_EVENT_DURATION_SECONDS",
        "_event_is_smooth",
        "SUMMARY_SHEET_NAME",
        "MAX_VALID_GPS_SPEED_KMH",
    ),
    "site_boundary_speed.py": (
        "classify_site_state_by_polygon",
        "detect_speed_violations_by_polygon",
        "write_site_boundary_metadata",
        "find_site_boundary",
    ),
    "map_links.py": (
        "google_maps_url",
        "parse_coordinate_pair",
        "SPEED_SHEETS",
        '"Скорость Ташуджу - Аккую"',
        '"Скорость вне региона"',
    ),
    "geozone_registry.py": (
        'SITE_BOUNDARY_PURPOSE = "site_boundary"',
        "find_site_boundary",
        "point_in_zone",
        "load_database_registry",
        "suppress_speed_in_exclusions",
        'source_mode = os.environ.get("GEOFENCE_SOURCE", "auto")',
    ),
    "verify_runtime.py": (
        "portal_entrypoint",
        "detect_regional_speed_violations",
        "classify_speed_category",
        "ROUTE_SHEET_NAME",
        "REGION_SHEET_NAME",
        "json_cell_one_decimal",
        "runtime-проверки",
    ),
}

OPERATIONAL_EXPECTATIONS = {
    "Dockerfile.server": (
        "sync_arvento_gps_to_postgres.py",
        "verify_repository.py",
        "verify_runtime.py",
        "verify_fleet_dashboard_api.py",
    ),
    "docker-compose.server.yml": (
        "name: arvento_report",
        "gps-sync:",
        "sync_arvento_gps_to_postgres.py",
        "run_geofence_editor:app",
        "report-portal:",
        "portal_entrypoint:app",
    ),
    "report_portal.py": (
        '"kpp": APP_DIR / "generate_first_entry_report.py"',
        '"efficiency": APP_DIR / "generate_kpp_summary_report.py"',
        '"violation": APP_DIR / "generate_prohibited_left_turn_report.py"',
    ),
    "deploy/nginx/arvento-report.conf.example": (
        "location ^~ /api/v1/fleet/",
        "auth_basic off;",
    ),
    ".env.server.example": (
        "FLEET_API_TOKEN=",
        "FLEET_API_TOKEN_SHA256=",
        "FLEET_API_MAX_PERIOD_DAYS=93",
        "FLEET_API_STATEMENT_TIMEOUT_MS=15000",
    ),
    "geozones.json": (
        '"name": "Площадка АЭС АККУЮ"',
        '"purpose": "site_boundary"',
        '"purpose": "speed_exclusion"',
        '"enabled": true',
        '"type": "polygon"',
    ),
    "README.md": tuple(
        name
        for name in CANONICAL_FILES
        if name not in {"run_report_portal.py", "generate_consolidated_report.py"}
    ),
    "SERVER_DEPLOY.md": tuple(
        name
        for name in CANONICAL_FILES
        if name not in {"run_report_portal.py", "generate_consolidated_report.py"}
    ),
}

FORBIDDEN_OPERATIONAL_REFERENCES = {
    "report_portal.py": (
        'APP_DIR / "generate_kpp_report.py"',
        'APP_DIR / "arvento_kpp_report.py"',
        'APP_DIR / "arvento_first_entry_report_fixed.py"',
        'APP_DIR / "prohibited_left_turn_report.py"',
    ),
    "docker-compose.server.yml": (
        "arvento_postgres_sync_v2.py",
        "geofence_editor_api:app",
        '"report_portal:app"',
        '"portal_table_ui:app"',
        '"consolidated_portal:app"',
    ),
    "sitecustomize.py": (
        "run_report_portal",
        "consolidated_portal",
        "portal_table_ui",
        "_install_portal_region_view",
    ),
    "Dockerfile.server": ("arvento_postgres_sync_v2.py",),
}

FORBIDDEN_ROOT_FILES = ("generate_kpp_report.py",)
EXPECTED_EXCLUSION_NAMES = {
    "Тоннели около Ташуджу",
    "Тоннели около Аккую",
}


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def check_required_tokens(errors: list[str]) -> None:
    for collection, kind in (
        (CANONICAL_FILES, "канонический файл"),
        (REQUIRED_SUPPORT_MODULES, "обязательный модуль"),
    ):
        for name, required_tokens in collection.items():
            path = ROOT / name
            if not path.is_file():
                errors.append(f"Отсутствует {kind}: {name}")
                continue
            content = read(path)
            for token in required_tokens:
                if token not in content:
                    errors.append(f"{name}: отсутствует обязательный элемент {token}")


def check_operational_references(errors: list[str]) -> None:
    for name, expected_tokens in OPERATIONAL_EXPECTATIONS.items():
        path = ROOT / name
        if not path.is_file():
            errors.append(f"Отсутствует операционный файл: {name}")
            continue
        content = read(path)
        for token in expected_tokens:
            if token not in content:
                errors.append(f"{name}: отсутствует каноническая ссылка {token}")

    for name, forbidden_tokens in FORBIDDEN_OPERATIONAL_REFERENCES.items():
        path = ROOT / name
        if not path.is_file():
            continue
        content = read(path)
        for token in forbidden_tokens:
            if token in content:
                errors.append(f"{name}: используется устаревшая операционная ссылка {token}")


def check_forbidden_files(errors: list[str]) -> None:
    for name in FORBIDDEN_ROOT_FILES:
        if (ROOT / name).exists():
            errors.append(f"Лишний неканонический исполняемый файл: {name}")


def check_python_syntax(errors: list[str]) -> int:
    checked = 0
    for path in sorted(ROOT.rglob("*.py")):
        if any(part in {".git", ".venv", "venv", "__pycache__"} for part in path.parts):
            continue
        checked += 1
        try:
            ast.parse(read(path), filename=str(path))
        except (SyntaxError, UnicodeError) as exc:
            errors.append(f"Синтаксическая ошибка: {path.relative_to(ROOT)}: {exc}")
    return checked


def check_geozones(errors: list[str]) -> None:
    path = ROOT / "geozones.json"
    try:
        data = json.loads(read(path))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"geozones.json: файл не читается: {exc}")
        return

    enabled = [item for item in data.get("geozones", []) if item.get("enabled", True)]
    boundaries = [item for item in enabled if str(item.get("purpose", "")).lower() == "site_boundary"]
    if len(boundaries) != 1:
        errors.append(f"geozones.json: ожидается одна граница площадки, найдено {len(boundaries)}")

    exclusions = [item for item in enabled if str(item.get("purpose", "")).lower() == "speed_exclusion"]
    names = {str(item.get("name", "")).strip() for item in exclusions}
    if names != EXPECTED_EXCLUSION_NAMES:
        errors.append(
            "geozones.json: неверный набор тоннельных зон: "
            + ", ".join(sorted(names or {"<пусто>"}))
        )

    for item in enabled:
        zone_type = str(item.get("type", "")).lower()
        if zone_type == "polygon":
            points = item.get("points", [])
            if len(points) < 3:
                errors.append(f"geozones.json: в полигоне «{item.get('name')}» меньше трёх точек")
            for point in points:
                if not isinstance(point, list) or len(point) < 2:
                    errors.append(f"geozones.json: некорректная точка в «{item.get('name')}»")
                    break
                try:
                    lat, lon = float(point[0]), float(point[1])
                except (TypeError, ValueError):
                    errors.append(f"geozones.json: нечисловая координата в «{item.get('name')}»")
                    break
                if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
                    errors.append(f"geozones.json: координата вне допустимого диапазона в «{item.get('name')}»")
                    break


def check_route_kml(errors: list[str]) -> None:
    path = ROOT / "route_akkuyu_tasucu.kml"
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        errors.append(f"route_akkuyu_tasucu.kml: файл не читается: {exc}")
        return

    text = next(
        (
            element.text
            for element in root.iter()
            if element.tag.rsplit("}", 1)[-1] == "coordinates" and element.text
        ),
        None,
    )
    if not text:
        errors.append("route_akkuyu_tasucu.kml: координаты полигона не найдены")
        return

    points: list[tuple[float, float]] = []
    try:
        for token in text.split():
            lon_text, lat_text, *_ = token.split(",")
            points.append((float(lat_text), float(lon_text)))
    except (TypeError, ValueError) as exc:
        errors.append(f"route_akkuyu_tasucu.kml: некорректная координата: {exc}")
        return
    if points and points[0] == points[-1]:
        points.pop()
    if len(points) < 3:
        errors.append("route_akkuyu_tasucu.kml: в маршруте меньше трёх уникальных точек")
        return
    if not all(math.isfinite(lat) and math.isfinite(lon) for lat, lon in points):
        errors.append("route_akkuyu_tasucu.kml: найдены нечисловые координаты")
        return
    area = abs(
        sum(
            points[index][1] * points[(index + 1) % len(points)][0]
            - points[(index + 1) % len(points)][1] * points[index][0]
            for index in range(len(points))
        )
    ) / 2.0
    if area <= 1e-8:
        errors.append("route_akkuyu_tasucu.kml: полигон маршрута имеет нулевую площадь")


def main() -> int:
    errors: list[str] = []
    check_required_tokens(errors)
    check_operational_references(errors)
    check_forbidden_files(errors)
    checked = check_python_syntax(errors)
    check_geozones(errors)
    check_route_kml(errors)

    print(f"Проверено Python-файлов: {checked}")
    print("Канонические исполняемые файлы:")
    for name in CANONICAL_FILES:
        print(f"  - {name}")

    if errors:
        print("\nОШИБКИ:")
        for error in errors:
            print(f"  - {error}")
        return 1

    print("\nOK: код, точки входа, геозоны, KML и операционные ссылки согласованы.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
