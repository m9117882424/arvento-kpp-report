#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Отчёт о запрещённом повороте налево по последовательному проезду 4 точек.

Нарушение фиксируется, если автомобиль последовательно попадает в радиусы
контрольных точек 1 -> 2 -> 3 -> 4 за допустимое время.

Поддерживаемые исходники: XLSX, XLSM, CSV — те же выгрузки Arvento, что и для
основного отчёта проекта.
"""

from __future__ import annotations

import argparse
import math
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import Tk, filedialog
from typing import Iterable

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from sqlite_store import import_source_to_sqlite


CONTROL_POINTS: tuple[tuple[float, float], ...] = (
    (36.3172538740116, 33.874474367432896),
    (36.31719343922596, 33.874075574912666),
    (36.31714948662511, 33.8735029497554),
    (36.31720168033584, 33.87281614041499),
)

DEFAULT_RADIUS_M = 25.0
DEFAULT_MAX_SEQUENCE_SECONDS = 10 * 60
DEFAULT_COOLDOWN_SECONDS = 5 * 60


@dataclass(frozen=True)
class TrackPoint:
    plate: str
    timestamp: datetime
    lat: float
    lon: float
    speed: float | None
    address: str


@dataclass(frozen=True)
class Violation:
    plate: str
    start: datetime
    finish: datetime
    matched: tuple[TrackPoint, TrackPoint, TrackPoint, TrackPoint]
    distances_m: tuple[float, float, float, float]

    @property
    def duration_seconds(self) -> int:
        return max(0, int((self.finish - self.start).total_seconds()))


def choose_source() -> Path:
    root = Tk()
    root.withdraw()
    selected = filedialog.askopenfilename(
        title="Выберите координатную выгрузку Arvento",
        filetypes=[("Excel / CSV", "*.xlsx *.xlsm *.csv"), ("Все файлы", "*.*")],
    )
    root.destroy()
    if not selected:
        raise SystemExit("Файл не выбран")
    return Path(selected).resolve()


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2
    )
    return 2.0 * radius * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def iter_vehicle_tracks(db_path: Path) -> Iterable[tuple[str, list[TrackPoint]]]:
    with sqlite3.connect(db_path) as connection:
        plates = [
            row[0]
            for row in connection.execute("SELECT DISTINCT plate FROM points ORDER BY plate")
        ]
        for index, plate in enumerate(plates, start=1):
            rows = connection.execute(
                """
                SELECT plate, ts, lat, lon, speed, address
                FROM points
                WHERE plate=?
                ORDER BY ts
                """,
                (plate,),
            )
            track = [
                TrackPoint(
                    plate=row[0],
                    timestamp=datetime.fromisoformat(row[1]),
                    lat=float(row[2]),
                    lon=float(row[3]),
                    speed=float(row[4]) if row[4] is not None else None,
                    address=row[5] or "",
                )
                for row in rows
            ]
            if track:
                yield plate, track
            if index % 100 == 0 or index == len(plates):
                print(f"Обработано автомобилей: {index}/{len(plates)}")


def detect_violations(
    track: list[TrackPoint],
    radius_m: float,
    max_sequence_seconds: int,
    cooldown_seconds: int,
) -> list[Violation]:
    violations: list[Violation] = []
    matched: list[TrackPoint] = []
    matched_distances: list[float] = []
    expected_index = 0
    last_violation_time: datetime | None = None

    for point in track:
        if last_violation_time is not None:
            if (point.timestamp - last_violation_time).total_seconds() < cooldown_seconds:
                continue

        if matched and (
            point.timestamp - matched[0].timestamp
        ).total_seconds() > max_sequence_seconds:
            matched.clear()
            matched_distances.clear()
            expected_index = 0

        distances = [
            haversine_m(point.lat, point.lon, lat, lon)
            for lat, lon in CONTROL_POINTS
        ]

        # Повторный заезд в первую точку перезапускает незавершённую последовательность.
        if expected_index > 0 and distances[0] <= radius_m:
            matched = [point]
            matched_distances = [distances[0]]
            expected_index = 1
            continue

        if distances[expected_index] > radius_m:
            continue

        matched.append(point)
        matched_distances.append(distances[expected_index])
        expected_index += 1

        if expected_index < len(CONTROL_POINTS):
            continue

        violation = Violation(
            plate=point.plate,
            start=matched[0].timestamp,
            finish=matched[-1].timestamp,
            matched=(matched[0], matched[1], matched[2], matched[3]),
            distances_m=(
                matched_distances[0],
                matched_distances[1],
                matched_distances[2],
                matched_distances[3],
            ),
        )
        violations.append(violation)
        last_violation_time = point.timestamp
        matched = []
        matched_distances = []
        expected_index = 0

    return violations


def style_sheet(sheet) -> None:
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    sheet.sheet_view.showGridLines = False
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="C00000")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    sheet.row_dimensions[1].height = 45
    for column in range(1, sheet.max_column + 1):
        width = min(
            max(
                len(str(sheet.cell(row, column).value or ""))
                for row in range(1, min(sheet.max_row, 300) + 1)
            ) + 2,
            34,
        )
        sheet.column_dimensions[get_column_letter(column)].width = max(width, 12)


def save_report(
    path: Path,
    violations: list[Violation],
    source: Path,
    radius_m: float,
    max_sequence_seconds: int,
) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Нарушения"
    sheet.append(
        [
            "№",
            "Госномер",
            "Дата",
            "Начало манёвра",
            "Окончание манёвра",
            "Продолжительность",
            "Время точки 1",
            "Время точки 2",
            "Время точки 3",
            "Время точки 4",
            "Скорость в точке 1",
            "Скорость в точке 2",
            "Скорость в точке 3",
            "Скорость в точке 4",
            "Расстояние до точки 1, м",
            "Расстояние до точки 2, м",
            "Расстояние до точки 3, м",
            "Расстояние до точки 4, м",
            "Координаты точки 1",
            "Координаты точки 2",
            "Координаты точки 3",
            "Координаты точки 4",
            "Адрес начала",
            "Адрес окончания",
        ]
    )

    for number, item in enumerate(violations, start=1):
        sheet.append(
            [
                number,
                item.plate,
                item.start.date(),
                item.start,
                item.finish,
                item.duration_seconds / 86400.0,
                item.matched[0].timestamp,
                item.matched[1].timestamp,
                item.matched[2].timestamp,
                item.matched[3].timestamp,
                item.matched[0].speed,
                item.matched[1].speed,
                item.matched[2].speed,
                item.matched[3].speed,
                round(item.distances_m[0], 1),
                round(item.distances_m[1], 1),
                round(item.distances_m[2], 1),
                round(item.distances_m[3], 1),
                f"{item.matched[0].lat:.7f}, {item.matched[0].lon:.7f}",
                f"{item.matched[1].lat:.7f}, {item.matched[1].lon:.7f}",
                f"{item.matched[2].lat:.7f}, {item.matched[2].lon:.7f}",
                f"{item.matched[3].lat:.7f}, {item.matched[3].lon:.7f}",
                item.matched[0].address,
                item.matched[3].address,
            ]
        )

    for row in sheet.iter_rows(min_row=2):
        row[2].number_format = "dd.mm.yyyy"
        for index in (3, 4, 6, 7, 8, 9):
            row[index].number_format = "dd.mm.yyyy hh:mm:ss"
        row[5].number_format = "[h]:mm:ss"
        for index in range(10, 14):
            row[index].number_format = "0.0"
        for index in range(14, 18):
            row[index].number_format = "0.0"
    style_sheet(sheet)

    settings = workbook.create_sheet("Параметры")
    settings.append(["Параметр", "Значение"])
    settings.append(["Исходный файл", str(source)])
    settings.append(["Радиус контрольной точки, м", radius_m])
    settings.append(["Максимальное время последовательности", max_sequence_seconds / 86400.0])
    settings.cell(4, 2).number_format = "[h]:mm:ss"
    for index, (lat, lon) in enumerate(CONTROL_POINTS, start=1):
        settings.append([f"Контрольная точка {index}", f"{lat}, {lon}"])
    settings.append(["Количество нарушений", len(violations)])
    style_sheet(settings)

    workbook.save(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Отчёт о запрещённом повороте налево")
    parser.add_argument("source", nargs="?", help="XLSX, XLSM или CSV выгрузка Arvento")
    parser.add_argument("output", nargs="?", help="Путь итогового XLSX")
    parser.add_argument("--radius", type=float, default=DEFAULT_RADIUS_M)
    parser.add_argument(
        "--max-minutes",
        type=float,
        default=DEFAULT_MAX_SEQUENCE_SECONDS / 60.0,
        help="Максимальное время прохождения точек 1-4",
    )
    parser.add_argument(
        "--cooldown-minutes",
        type=float,
        default=DEFAULT_COOLDOWN_SECONDS / 60.0,
        help="Защита от повторной регистрации одного манёвра",
    )
    args = parser.parse_args()

    source = Path(args.source).expanduser().resolve() if args.source else choose_source()
    if not source.exists():
        raise SystemExit(f"Файл не найден: {source}")
    output = (
        Path(args.output).expanduser().resolve()
        if args.output
        else source.with_name(source.stem + "_запрещенный_поворот.xlsx")
    )

    temp_dir = Path(tempfile.mkdtemp(prefix="arvento_left_turn_"))
    db_path = temp_dir / "points.sqlite3"
    try:
        print(f"Исходный файл: {source}")
        print("Импорт данных во временную SQLite-базу...")
        stats = import_source_to_sqlite(source, db_path)

        violations: list[Violation] = []
        for plate, track in iter_vehicle_tracks(db_path):
            violations.extend(
                detect_violations(
                    track,
                    radius_m=args.radius,
                    max_sequence_seconds=max(1, int(args.max_minutes * 60)),
                    cooldown_seconds=max(0, int(args.cooldown_minutes * 60)),
                )
            )

        violations.sort(key=lambda item: (item.start, item.plate))
        save_report(
            output,
            violations,
            source,
            args.radius,
            max(1, int(args.max_minutes * 60)),
        )
        print(f"Готово: {output}")
        print(f"Загружено GPS-точек: {stats['loaded']}")
        print(f"Найдено нарушений: {len(violations)}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
