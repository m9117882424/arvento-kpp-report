from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from arvento_analysis import analyze_day
from gate_event_times import detect_first_entry_last_exit
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
            first_entry, last_exit = detect_first_entry_last_exit(points, registry)
            summaries, stops = analyze_day(points, registry)
            for summary in summaries:
                summary["first_entry_time"] = first_entry
                summary["last_exit_time"] = last_exit
            day_summaries.extend(summaries)
            all_stops.extend(stops)
            del points

            if index % 100 == 0 or index == len(plates):
                print(f"  обработано автомобилей: {index}/{len(plates)}")

        daily[day] = day_summaries

    return daily, all_stops
