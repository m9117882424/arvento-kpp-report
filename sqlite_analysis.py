from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from arvento_analysis import analyze_day
from geozone_registry import Registry
from sqlite_store import list_days, list_plates_for_day, load_vehicle_day_points


def analyze_sqlite(
    db_path: Path,
    registry: Registry,
) -> tuple[dict[date, list[dict[str, Any]]], list[dict[str, Any]]]:
    daily: dict[date, list[dict[str, Any]]] = {}
    all_stops: list[dict[str, Any]] = []

    for day_text in list_days(db_path):
        day = date.fromisoformat(day_text)
        day_summaries: list[dict[str, Any]] = []
        plates = list_plates_for_day(db_path, day_text)
        print(f"Анализ {day.strftime('%d.%m.%Y')}: автомобилей {len(plates)}")

        for index, plate in enumerate(plates, start=1):
            points = load_vehicle_day_points(db_path, day_text, plate)
            summaries, stops = analyze_day(points, registry)
            day_summaries.extend(summaries)
            all_stops.extend(stops)
            del points

            if index % 100 == 0 or index == len(plates):
                print(f"  обработано автомобилей: {index}/{len(plates)}")

        daily[day] = day_summaries

    return daily, all_stops
