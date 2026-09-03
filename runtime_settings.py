#!/usr/bin/env python3
"""Typed runtime settings shared by deployment validation and web processes."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping


class ConfigurationError(RuntimeError):
    """Raised when an operator-provided setting is missing or invalid."""


def _text(source: Mapping[str, str], name: str, default: str = "") -> str:
    return str(source.get(name, default)).strip()


def _required(source: Mapping[str, str], name: str) -> str:
    value = _text(source, name)
    if not value:
        raise ConfigurationError(f"{name} не заполнена")
    return value


def _integer(
    source: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    raw = _text(source, name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} должна быть целым числом") from exc
    if value < minimum or (maximum is not None and value > maximum):
        suffix = f"–{maximum}" if maximum is not None else " или больше"
        raise ConfigurationError(f"{name} должна быть в диапазоне {minimum}{suffix}")
    return value


def _floating(
    source: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float | None = None,
) -> float:
    raw = _text(source, name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} должна быть числом") from exc
    if value < minimum or (maximum is not None and value > maximum):
        suffix = f"–{maximum:g}" if maximum is not None else " или больше"
        raise ConfigurationError(f"{name} должна быть в диапазоне {minimum:g}{suffix}")
    return value


def _boolean(source: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _text(source, name, "true" if default else "false").casefold()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} должна быть true или false")


@dataclass(frozen=True, slots=True)
class ReportRuntimeSettings:
    download_dir: Path
    download_ttl_seconds: int
    download_max_files: int
    download_max_bytes: int
    max_concurrent_generations: int
    generation_queue_timeout_seconds: float
    generation_job_ttl_seconds: int
    generation_job_max_entries: int

    @classmethod
    def from_mapping(cls, source: Mapping[str, str]) -> "ReportRuntimeSettings":
        total_mb = _floating(
            source,
            "REPORT_DOWNLOAD_MAX_TOTAL_MB",
            150.0,
            minimum=0.001,
        )
        explicit_bytes = _text(source, "REPORT_DOWNLOAD_MAX_BYTES")
        max_bytes = (
            _integer(
                source,
                "REPORT_DOWNLOAD_MAX_BYTES",
                1,
                minimum=1,
            )
            if explicit_bytes
            else int(total_mb * 1024 * 1024)
        )
        return cls(
            download_dir=Path(
                _text(
                    source,
                    "REPORT_DOWNLOAD_DIR",
                    "/tmp/arvento-report-downloads",
                )
            ),
            download_ttl_seconds=_integer(
                source,
                "REPORT_DOWNLOAD_TTL_SECONDS",
                1800,
                minimum=60,
            ),
            download_max_files=_integer(
                source,
                "REPORT_DOWNLOAD_MAX_FILES",
                20,
                minimum=1,
            ),
            download_max_bytes=max_bytes,
            max_concurrent_generations=_integer(
                source,
                "REPORT_MAX_CONCURRENT_GENERATIONS",
                1,
                minimum=1,
            ),
            generation_queue_timeout_seconds=_floating(
                source,
                "REPORT_GENERATION_QUEUE_TIMEOUT_SECONDS",
                5.0,
                minimum=0.1,
            ),
            generation_job_ttl_seconds=_integer(
                source,
                "REPORT_GENERATION_JOB_TTL_SECONDS",
                3600,
                minimum=300,
            ),
            generation_job_max_entries=_integer(
                source,
                "REPORT_GENERATION_JOB_MAX_ENTRIES",
                50,
                minimum=1,
            ),
        )


@dataclass(frozen=True, slots=True)
class GeofenceEditorSettings:
    database_url: str
    google_maps_api_key: str
    default_map_provider: str
    osm_fallback_enabled: bool
    map_center_lat: float
    map_center_lon: float
    map_default_zoom: int

    @classmethod
    def from_mapping(cls, source: Mapping[str, str]) -> "GeofenceEditorSettings":
        provider = _text(source, "DEFAULT_MAP_PROVIDER", "google").casefold()
        if provider not in {"google", "osm"}:
            raise ConfigurationError("DEFAULT_MAP_PROVIDER должна быть google или osm")
        return cls(
            database_url=_required(source, "DATABASE_URL"),
            google_maps_api_key=_text(source, "GOOGLE_MAPS_API_KEY"),
            default_map_provider=provider,
            osm_fallback_enabled=_boolean(source, "OSM_FALLBACK_ENABLED", True),
            map_center_lat=_floating(
                source, "MAP_CENTER_LAT", 36.145, minimum=-90.0, maximum=90.0
            ),
            map_center_lon=_floating(
                source, "MAP_CENTER_LON", 33.535, minimum=-180.0, maximum=180.0
            ),
            map_default_zoom=_integer(
                source, "MAP_DEFAULT_ZOOM", 15, minimum=1, maximum=22
            ),
        )


@lru_cache(maxsize=1)
def report_runtime_settings() -> ReportRuntimeSettings:
    return ReportRuntimeSettings.from_mapping(os.environ)


@lru_cache(maxsize=1)
def geofence_editor_settings() -> GeofenceEditorSettings:
    return GeofenceEditorSettings.from_mapping(os.environ)


def load_env_file(path: Path) -> dict[str, str]:
    """Load the simple KEY=VALUE format accepted by the production env file."""
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigurationError(f"{path}:{line_number}: ожидается KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise ConfigurationError(f"{path}:{line_number}: пустое имя переменной")
        if key in values:
            raise ConfigurationError(f"{path}:{line_number}: повторяется {key}")
        values[key] = value.strip()
    return values


def validate_server_environment(source: Mapping[str, str]) -> None:
    """Validate secrets and typed web settings before a production build."""
    required = (
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "DATABASE_URL",
        "ARVENTO_USER",
        "ARVENTO_PIN1",
        "ARVENTO_PIN2",
        "ARVENTO_GROUP",
    )
    values = {name: _required(source, name) for name in required}
    if "CHANGE_ME" in values["POSTGRES_PASSWORD"] or "CHANGE_ME" in values["DATABASE_URL"]:
        raise ConfigurationError("замените CHANGE_ME в POSTGRES_PASSWORD и DATABASE_URL")
    if "@postgres:5432/" not in values["DATABASE_URL"]:
        raise ConfigurationError(
            "DATABASE_URL внутри Docker должен использовать host postgres:5432"
        )
    if any(character in values["POSTGRES_PASSWORD"] for character in "@:/?#[]"):
        raise ConfigurationError(
            "POSTGRES_PASSWORD содержит символы, требующие URL-кодирования; "
            "используйте URL-safe пароль"
        )
    ReportRuntimeSettings.from_mapping(source)
    GeofenceEditorSettings.from_mapping(source)


__all__ = [
    "ConfigurationError",
    "GeofenceEditorSettings",
    "ReportRuntimeSettings",
    "geofence_editor_settings",
    "load_env_file",
    "report_runtime_settings",
    "validate_server_environment",
]
