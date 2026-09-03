#!/usr/bin/env python3
"""Deterministic checks for typed production configuration."""
from __future__ import annotations

import tempfile
from pathlib import Path

from runtime_settings import (
    ConfigurationError,
    GeofenceEditorSettings,
    ReportRuntimeSettings,
    load_env_file,
    validate_server_environment,
)

ROOT = Path(__file__).resolve().parent


def valid_environment() -> dict[str, str]:
    return {
        "POSTGRES_DB": "arvento_report",
        "POSTGRES_USER": "arvento_report",
        "POSTGRES_PASSWORD": "0123456789abcdef",
        "DATABASE_URL": "postgresql://arvento_report:0123456789abcdef@postgres:5432/arvento_report",
        "ARVENTO_USER": "operator",
        "ARVENTO_PIN1": "pin-one",
        "ARVENTO_PIN2": "pin-two",
        "ARVENTO_GROUP": "TSM",
    }


def expect_error(source: dict[str, str], expected: str) -> None:
    try:
        validate_server_environment(source)
    except ConfigurationError as exc:
        assert expected in str(exc), str(exc)
    else:
        raise AssertionError(f"Ожидалась ошибка настройки: {expected}")


def main() -> None:
    source = valid_environment()
    validate_server_environment(source)

    report = ReportRuntimeSettings.from_mapping(source)
    assert report.download_ttl_seconds == 1800
    assert report.download_max_bytes == 150 * 1024 * 1024
    assert report.max_concurrent_generations == 1
    assert report.generation_job_ttl_seconds == 3600

    custom = dict(source)
    custom.update(
        {
            "REPORT_DOWNLOAD_MAX_BYTES": "4096",
            "REPORT_GENERATION_JOB_MAX_ENTRIES": "75",
            "DEFAULT_MAP_PROVIDER": "OSM",
            "OSM_FALLBACK_ENABLED": "no",
            "MAP_CENTER_LAT": "36.2",
            "MAP_CENTER_LON": "33.6",
            "MAP_DEFAULT_ZOOM": "17",
        }
    )
    assert ReportRuntimeSettings.from_mapping(custom).download_max_bytes == 4096
    geofence = GeofenceEditorSettings.from_mapping(custom)
    assert geofence.default_map_provider == "osm"
    assert not geofence.osm_fallback_enabled
    assert geofence.map_default_zoom == 17

    invalid = dict(source)
    invalid["REPORT_GENERATION_QUEUE_TIMEOUT_SECONDS"] = "never"
    expect_error(invalid, "REPORT_GENERATION_QUEUE_TIMEOUT_SECONDS должна быть числом")
    invalid = dict(source)
    invalid["MAP_CENTER_LAT"] = "100"
    expect_error(invalid, "MAP_CENTER_LAT должна быть в диапазоне")
    invalid = dict(source)
    invalid["POSTGRES_PASSWORD"] = "unsafe@password"
    expect_error(invalid, "URL-кодирования")

    with tempfile.TemporaryDirectory(prefix="arvento_env_") as name:
        path = Path(name) / ".env"
        path.write_text("A=1\nB=two\n", encoding="utf-8")
        assert load_env_file(path) == {"A": "1", "B": "two"}
        path.write_text("A=1\nA=2\n", encoding="utf-8")
        try:
            load_env_file(path)
        except ConfigurationError as exc:
            assert "повторяется A" in str(exc)
        else:
            raise AssertionError("Повторяющаяся переменная не обнаружена")

    for module_name in (
        "download_store.py",
        "generation_control.py",
        "generation_jobs.py",
        "geofence_editor_api.py",
    ):
        text = (ROOT / module_name).read_text(encoding="utf-8")
        assert "from runtime_settings import" in text, module_name
        assert "os.environ" not in text, module_name

    print("OK: typed production settings and env-file validation verified")


if __name__ == "__main__":
    main()
