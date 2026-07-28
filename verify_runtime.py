#!/usr/bin/env python3
"""Small runtime smoke tests executed during the server image build.

Pure report-logic checks can also be run directly on the server host. The full
portal import requires the application dependencies installed in the Docker
image. During image build the check is strict and must not be skipped.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from arvento_first_entry_report import ReportFilters
from arvento_first_entry_report_fixed import build_report_titles, extract_date_from_text
from arvento_reports import round_metric, round_ratio
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

    duration = timedelta(hours=3, minutes=4, seconds=56, microseconds=176000)
    assert portal_table_ui.implementation.json_cell(duration) == "3:04:56", (
        "В интерфейсе времени остались доли секунды"
    )
    long_duration = timedelta(hours=27, minutes=2, seconds=3, microseconds=900000)
    assert portal_table_ui.implementation.json_cell(long_duration) == "27:02:04", (
        "Продолжительность более суток отображается неверно"
    )

    paths = {route.path for route in portal_table_ui.app.routes}
    assert "/api/database-status" in paths
    assert "/api/generate-v2" in paths
    return True


def main() -> None:
    assert extract_date_from_text("23.07.2026 SON GUNCEL.xlsx") == date(2026, 7, 23)
    assert extract_date_from_text("gps_2026-07-24.csv") == date(2026, 7, 24)
    title, subtitle = build_report_titles(
        date(2026, 7, 24),
        date(2026, 7, 23),
        ReportFilters(time_from=time(7, 0), time_to=time(9, 0)),
    )
    assert title == (
        "Отчет по времени въезда служебных автомобилей в утреннее время за "
        "24.07.2026 (с 7:00 до 09:00) без учёта повторных проездов через геозону."
    )
    assert subtitle == (
        "(принадлежность водителей проставлена по разнарядке от 23.07.2026)"
    )

    assert round_metric(12.349) == 12.3, "Пробег не округлён до одного знака"
    assert round_metric(12.351) == 12.4, "Округление пробега работает неверно"
    assert round_ratio(0.32654) == 0.327, "Процент не округлён до одного отображаемого знака"

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
            "OK: runtime-проверки заголовка КПП, округления, времени, геозоны, "
            "нарушений, фильтра, сортировки, карты и статуса БД пройдены."
        )
    else:
        print(
            "OK: runtime-проверки заголовка КПП, округления и расчётной логики "
            "пройдены; проверка веб-портала будет выполнена при Docker-сборке."
        )


if __name__ == "__main__":
    main()
