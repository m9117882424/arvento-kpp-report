#!/usr/bin/env python3
"""Static consistency checks for canonical project entrypoints.

The script does not contact Arvento or PostgreSQL. It checks file names,
operational references, wrapper targets and Python syntax across the repository.
"""
from __future__ import annotations

import py_compile
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
    "generate_kpp_summary_report.py": ("arvento_kpp_report",),
    "generate_first_entry_report.py": ("arvento_first_entry_report_fixed",),
    "generate_prohibited_left_turn_report.py": ("prohibited_left_turn_report",),
    "generate_scheduled_reports.py": ("run_automated_reports",),
}

OPERATIONAL_EXPECTATIONS = {
    "Dockerfile.server": (
        "sync_arvento_gps_to_postgres.py",
    ),
    "docker-compose.server.yml": (
        "name: arvento_report",
        "gps-sync:",
        "sync_arvento_gps_to_postgres.py",
        "run_geofence_editor:app",
        "report-portal:",
        "report_portal:app",
    ),
    "report_portal.py": (
        "generate_kpp_summary_report.py",
        "generate_first_entry_report.py",
        "generate_prohibited_left_turn_report.py",
    ),
    "README.md": tuple(CANONICAL_FILES),
    "SERVER_DEPLOY.md": tuple(CANONICAL_FILES),
}

FORBIDDEN_OPERATIONAL_REFERENCES = {
    "report_portal.py": (
        "generate_kpp_report.py",
        "arvento_kpp_report.py",
        "arvento_first_entry_report_fixed.py",
        "prohibited_left_turn_report.py",
    ),
    "docker-compose.server.yml": (
        "arvento_postgres_sync_v2.py",
        "geofence_editor_api:app",
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
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as exc:
            errors.append(f"Синтаксическая ошибка: {path.relative_to(ROOT)}: {exc.msg}")
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
