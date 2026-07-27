#!/usr/bin/env python3
"""Small runtime smoke tests executed during the server image build.

Pure report-logic checks can also be run directly on the server host. The full
portal import requires the application dependencies installed in the Docker
image. During image build the check is strict and must not be skipped.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from geozone_registry import find_site_boundary, load_registry, point_in_zone
from map_links import google_maps_url, parse_coordinate_pair
from site_boundary_speed import classify_site_state_by_polygon
from speed_violation_report import _event_is_smooth, validate_speed_thresholds


STRICT_ENV = "ARVENTO_RUNTIME_CHECK_STRICT"
OPTIONAL_PORTAL_DEPENDENCIES = {
    "fastapi",
    "openpyxl",
    "psycopg",
    "pydantic",
    "starlette",
    "uvicorn",
}


@dataclass
class FakePoint:
    timestamp: datetime
    speed: float
    lat: float = 36.145
    lon: float = 33.55
    plate: str = "TEST"
    address: str = ""


def points(*speeds: float, seconds: int = 10) -> list[FakePoint]:
    start = datetime(2026, 1, 1, 8, 0, 0)
    return [
        FakePoint(start + timedelta(seconds=index * seconds), speed)
        for index, speed in enumerate(speeds)
    ]


def strict_mode() -> bool:
    return os.environ.get(STRICT_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def check_portal_runtime() -> bool:
    """Import and inspect the final ASGI portal when runtime deps are available."""
    try:
        import portal_table_ui
    except ModuleNotFoundError as exc:
        missing = (exc.name or "").split(".", 1)[0]
        if strict_mode() or missing not in OPTIONAL_PORTAL_DEPENDENCIES:
            raise
        print(
            "ПРЕДУПРЕЖДЕНИЕ: проверка импорта веб-портала пропущена на хосте — "
            f"не установлен пакет {missing!r}. Полная проверка будет выполнена "
            "внутри Docker при сборке report-portal."
        )
        return False

    html = portal_table_ui.implementation.HTML
    for token in (
        'value="violation">Нарушения',
        'id="siteSpeedThreshold"',
        'id="outsideSpeedThreshold"',
        'id="plateFilter" type="text"',
        'id="plateSuggestions"',
        'class="sortable',
        'class="sort-indicator"',
        '.sort-indicator::before',
        '.sort-indicator::after',
        'cursor:pointer !important',
        'data-sort-index',
        "plateFilter.addEventListener('input'",
        'id="dbStatus"',
        '/api/database-status',
        '/api/generate-v2',
        'target="_blank"',
    ):
        assert token in html, f"В интерфейсе отсутствует обязательный элемент: {token}"

    paths = {route.path for route in portal_table_ui.app.routes}
    assert "/api/database-status" in paths
    assert "/api/generate-v2" in paths
    return True


def main() -> None:
    assert _event_is_smooth(points(40, 45, 50)), "Плавная последовательность отклонена"
    assert not _event_is_smooth(points(40, 120, 42)), "Одиночный выброс не отфильтрован"
    assert not _event_is_smooth(points(40, 45)), "Событие из двух точек не должно быть валидным"
    assert not _event_is_smooth(points(40, 45, 50, seconds=4)), "Слишком короткое событие принято"

    assert validate_speed_thresholds(33, 104.5) == (33.0, 104.5)
    try:
        validate_speed_thresholds(120, 100)
    except ValueError:
        pass
    else:
        raise AssertionError("Некорректное соотношение порогов не отклонено")

    registry = load_registry(Path(__file__).resolve().parent / "geozones.json")
    boundary = find_site_boundary(registry)
    assert boundary.name == "Площадка АЭС АККУЮ"
    assert len(boundary.points or []) == 12
    assert point_in_zone(36.145, 33.55, boundary), "Центральная точка площадки не распознана"
    assert not point_in_zone(36.17, 33.55, boundary), "Внешняя точка ошибочно попала на площадку"

    jitter_track = [
        FakePoint(datetime(2026, 1, 1, 8, 0, 0), 40, 36.145, 33.55),
        FakePoint(datetime(2026, 1, 1, 8, 0, 10), 42, 36.17, 33.55),
        FakePoint(datetime(2026, 1, 1, 8, 0, 20), 44, 36.145, 33.55),
    ]
    states = [inside for _, inside in classify_site_state_by_polygon(jitter_track, registry)]
    assert states == [True, True, True], "Одиночный GPS-выброс за границу не сглажен"

    assert parse_coordinate_pair("36.145, 33.55") == (36.145, 33.55)
    assert google_maps_url(36.145, 33.55).startswith("https://www.google.com/maps/search/")

    portal_checked = check_portal_runtime()
    if portal_checked:
        print(
            "OK: runtime-проверки геозоны, нарушений, фильтра, значков сортировки, "
            "карты и статуса БД пройдены."
        )
    else:
        print(
            "OK: runtime-проверки расчётной логики пройдены; проверка веб-портала "
            "будет выполнена при Docker-сборке."
        )


if __name__ == "__main__":
    main()
