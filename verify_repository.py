#!/usr/bin/env python3
"""Static consistency checks for canonical project entrypoints.

The script does not contact Arvento or PostgreSQL. It checks file names,
operational references, wrapper targets and Python syntax across the repository.
"""
from __future__ import annotations

import ast
import sys
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
        "json_cell_one_decimal",
        "Скорость Ташуджу - Аккую",
        "Скорость вне региона",
        'f"{value:.1f}"',
        "current.violation_web_preview = violation_web_preview",
    ),
    "portal_entrypoint.py": (
        "import consolidated_portal as base",
        "import portal_runtime_patch",
        "app = base.app",
    ),
    "regional_speed_report.py": (
        "detect_regional_speed_violations",
        "append_regional_speed_sheets",
        "ROUTE_SHEET_NAME",
        "REGION_SHEET_NAME",
        "speed_exclusion",
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
    ),
    "route_akkuyu_tasucu.kml": (
        "<Polygon>",
        "<coordinates>",
    ),
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
        "add_violation_map_links",
    ),
    "geozone_registry.py": (
        'SITE_BOUNDARY_PURPOSE = "site_boundary"',
        "find_site_boundary",
        "point_in_zone",
    ),
    "verify_runtime.py": (
        "_event_is_smooth",
        "consolidated_portal",
        "plateSuggestions",
        "data-sort-index",
        "dbStatus",
        "Площадка АЭС АККУЮ",
        "runtime-проверки",
    ),
}

OPERATIONAL_EXPECTATIONS = {
    "Dockerfile.server": (
        "sync_arvento_gps_to_postgres.py",
        "verify_repository.py",
        "verify_runtime.py",
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
    "geozones.json": (
        '"name": "Площадка АЭС АККУЮ"',
        '"purpose": "site_boundary"',
        '"purpose": "speed_exclusion"',
        '"enabled": true',
        '"type": "polygon"',
    ),
    "README.md": tuple(
        name for name in CANONICAL_FILES
        if name not in {"run_report_portal.py", "generate_consolidated_report.py"}
    ),
    "SERVER_DEPLOY.md": tuple(
        name for name in CANONICAL_FILES
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
    "Dockerfile.server": (
        "arvento_postgres_sync_v2.py",
    ),
}

FORBIDDEN_ROOT_FILES = (
    "generate_kpp_report.py",
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def check_canonical_files(errors: list[str]) -> None:
    for name, required_tokens in CANONICAL_FILES.items():
        path = ROOT / name
        if not path.is_file():
            errors.append(f"Отсутствует канонический файл: {name}")
            continue
        content = read(path)
        for token in required_tokens:
            if token not in content:
                errors.append(f"{name}: не найдена ожидаемая связь с {token}")

    for name, required_tokens in REQUIRED_SUPPORT_MODULES.items():
        path = ROOT / name
        if not path.is_file():
            errors.append(f"Отсутствует обязательный модуль: {name}")
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
    """Parse every Python file without creating __pycache__ artifacts."""
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


def main() -> int:
    errors: list[str] = []
    check_canonical_files(errors)
    check_operational_references(errors)
    check_forbidden_files(errors)
    checked = check_python_syntax(errors)

    print(f"Проверено Python-файлов: {checked}")
    print("Канонические исполняемые файлы:")
    for name in CANONICAL_FILES:
        print(f"  - {name}")

    if errors:
        print("\nОШИБКИ:")
        for error in errors:
            print(f"  - {error}")
        return 1

    print("\nOK: имена, операционные ссылки и синтаксис согласованы.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
