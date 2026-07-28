#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate the consolidated violations report.

The workbook contains prohibited turns and three validated speed categories:
Akkuyu site, Tasucu–Akkuyu route and outside every configured geozone.
"""
from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path

import prohibited_left_turn_report as turn_report
from confirmed_left_turn_detector import detect_confirmed_violations
from geozone_registry import load_registry
from map_links import add_violation_map_links
from regional_speed_report import (
    append_regional_speed_sheets,
    detect_regional_speed_violations,
)
from site_boundary_speed import write_site_boundary_metadata
from speed_violation_report import (
    DEFAULT_OUTSIDE_SPEED_THRESHOLD_KMH,
    DEFAULT_SITE_SPEED_THRESHOLD_KMH,
    validate_speed_thresholds,
)
from sqlite_store import import_source_to_sqlite


def main() -> None:
    parser = argparse.ArgumentParser(description="Сводный отчёт по нарушениям")
    parser.add_argument("source", nargs="?", help="XLSX, XLSM или CSV выгрузка Arvento")
    parser.add_argument("output", nargs="?", help="Путь итогового XLSX")
    parser.add_argument("--width", type=float, default=turn_report.DEFAULT_WIDTH_M)
    parser.add_argument(
        "--max-minutes", type=float,
        default=turn_report.DEFAULT_MAX_SEQUENCE_SECONDS / 60.0,
    )
    parser.add_argument(
        "--control-minutes", type=float,
        default=turn_report.DEFAULT_CONTROL_WINDOW_SECONDS / 60.0,
    )
    parser.add_argument(
        "--cooldown-minutes", type=float,
        default=turn_report.DEFAULT_COOLDOWN_SECONDS / 60.0,
    )
    parser.add_argument(
        "--site-speed-threshold", type=float,
        default=DEFAULT_SITE_SPEED_THRESHOLD_KMH,
        help="Порог фиксации скорости на площадке, км/ч",
    )
    parser.add_argument(
        "--outside-speed-threshold", type=float,
        default=DEFAULT_OUTSIDE_SPEED_THRESHOLD_KMH,
        help="Порог фиксации скорости на маршруте и вне региона, км/ч",
    )
    args = parser.parse_args()

    try:
        site_threshold, outside_threshold = validate_speed_thresholds(
            args.site_speed_threshold, args.outside_speed_threshold
        )
    except ValueError as exc:
        raise SystemExit(f"Некорректные пороги скорости: {exc}") from exc

    source = Path(args.source).expanduser().resolve() if args.source else turn_report.choose_source()
    if not source.exists():
        raise SystemExit(f"Файл не найден: {source}")
    output = (
        Path(args.output).expanduser().resolve()
        if args.output else source.with_name(source.stem + "_нарушения.xlsx")
    )

    config = source.parent / "geozones.json"
    if not config.exists():
        config = Path(__file__).resolve().parent / "geozones.json"
    registry = load_registry(config)

    width_m = max(1.0, args.width)
    max_seconds = max(1, int(args.max_minutes * 60))
    control_seconds = max(0, int(args.control_minutes * 60))
    cooldown_seconds = max(0, int(args.cooldown_minutes * 60))

    temp_dir = Path(tempfile.mkdtemp(prefix="arvento_violations_"))
    db_path = temp_dir / "points.sqlite3"
    try:
        print(f"Исходный файл: {source}")
        print("Импорт данных во временную SQLite-базу...")
        stats = import_source_to_sqlite(source, db_path)

        turn_violations = []
        site_speed_violations = []
        route_speed_violations = []
        region_speed_violations = []

        for _, track in turn_report.iter_vehicle_tracks(db_path):
            turn_violations.extend(
                detect_confirmed_violations(
                    track,
                    width_m=width_m,
                    max_sequence_seconds=max_seconds,
                    control_window_seconds=control_seconds,
                    cooldown_seconds=cooldown_seconds,
                )
            )
            site_items, route_items, region_items = detect_regional_speed_violations(
                track,
                registry,
                site_threshold_kmh=site_threshold,
                outside_threshold_kmh=outside_threshold,
            )
            site_speed_violations.extend(site_items)
            route_speed_violations.extend(route_items)
            region_speed_violations.extend(region_items)

        for items in (
            turn_violations,
            site_speed_violations,
            route_speed_violations,
            region_speed_violations,
        ):
            items.sort(key=lambda item: (item.plate, item.start))

        turn_report.save_report(
            output,
            turn_violations,
            source,
            width_m,
            max_seconds,
            control_seconds,
        )
        append_regional_speed_sheets(
            output,
            site_speed_violations,
            route_speed_violations,
            region_speed_violations,
            site_threshold_kmh=site_threshold,
            outside_threshold_kmh=outside_threshold,
        )
        write_site_boundary_metadata(output, registry)
        map_link_count = add_violation_map_links(output)

        print(f"Готово: {output}")
        print(f"Загружено GPS-точек: {stats['loaded']}")
        print(f"Запрещённых поворотов: {len(turn_violations)}")
        print(f"Нарушений скорости на площадке: {len(site_speed_violations)}")
        print(f"Нарушений скорости Ташуджу - Аккую: {len(route_speed_violations)}")
        print(f"Нарушений скорости вне региона: {len(region_speed_violations)}")
        print(f"Порог на площадке: {site_threshold:.1f} км/ч")
        print(f"Порог маршрута и вне региона: {outside_threshold:.1f} км/ч")
        print(f"Ссылок на карту добавлено: {map_link_count}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
