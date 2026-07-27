#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Запуск отчёта по первому въезду с устойчивым определением стороны КПП.

Исправляет пропуски пересечений, когда промежуточная GPS-точка попадает в
пограничную зону менее MIN_SIDE_DISTANCE_M от линии ворот. Итоговый лист
«Первый въезд» не содержит отдельного столбца со ссылкой на карту.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

import arvento_first_entry_report as base


_original_load_coordinate_points = base.load_coordinate_points
_original_create_report = base.create_report


def detect_csv_encoding(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1251", "cp1254"):
        try:
            with path.open("r", encoding=encoding, newline="") as stream:
                stream.read(256 * 1024)
            return encoding
        except UnicodeDecodeError:
            continue
    raise ValueError("Не удалось определить кодировку CSV")


def detect_csv_delimiter(path: Path, encoding: str) -> str:
    with path.open("r", encoding=encoding, newline="") as stream:
        sample = stream.read(64 * 1024)
    try:
        return csv.Sniffer().sniff(sample, delimiters=";,\t|").delimiter
    except csv.Error:
        return ","


def load_coordinate_points(path: Path) -> list[base.Point]:
    """Загружает координатные точки из XLSX/XLSM или CSV."""
    if path.suffix.lower() != ".csv":
        return _original_load_coordinate_points(path)

    encoding = detect_csv_encoding(path)
    delimiter = detect_csv_delimiter(path, encoding)
    points: list[base.Point] = []
    columns: dict[str, int | None] | None = None
    epoch = datetime(1899, 12, 30)

    with path.open("r", encoding=encoding, newline="") as stream:
        reader = csv.reader(stream, delimiter=delimiter)
        for row in reader:
            if columns is None:
                headers = [base.clean(value) for value in row]
                candidate = {
                    key: base.find_column(headers, aliases)
                    for key, aliases in base.COORD_ALIASES.items()
                }
                if all(candidate.get(key) is not None for key in ("plate", "timestamp", "lat", "lon")):
                    columns = candidate
                continue

            plate = base.normalize_plate(base.get_value(row, columns["plate"]))
            timestamp = base.parse_datetime(base.get_value(row, columns["timestamp"]), epoch)
            lat = base.as_float(base.get_value(row, columns["lat"]))
            lon = base.as_float(base.get_value(row, columns["lon"]))
            if not plate or timestamp is None or lat is None or lon is None:
                continue

            points.append(
                base.Point(
                    plate=plate,
                    timestamp=timestamp,
                    lat=lat,
                    lon=lon,
                    region=base.clean(base.get_value(row, columns.get("region"))),
                )
            )

    if columns is None:
        raise ValueError(
            "В координатной CSV-выгрузке не найдены колонки: "
            "госномер, дата/время, широта, долгота"
        )
    if not points:
        raise ValueError("В координатной CSV-выгрузке нет пригодных GPS-точек")
    return points


def stable_side(distance_m: float) -> int:
    if distance_m >= base.MIN_SIDE_DISTANCE_M:
        return 1
    if distance_m <= -base.MIN_SIDE_DISTANCE_M:
        return -1
    return 0


def detect_coordinate_crossings(points: list[base.Point]) -> list[base.Crossing]:
    """Определяет пересечения через последнюю устойчивую сторону линии КПП.

    Последовательность вида +34 м -> -0,56 м -> -8,68 м теперь считается
    одним въездом: точка -0,56 м находится в пограничной зоне и не сбрасывает
    последнюю устойчивую внешнюю сторону.
    """
    by_plate: dict[str, list[base.Point]] = defaultdict(list)
    for point in points:
        by_plate[point.plate].append(point)

    crossings: list[base.Crossing] = []
    for plate, vehicle_points in by_plate.items():
        vehicle_points.sort(key=lambda item: item.timestamp)
        last_by_gate_direction: dict[tuple[str, str], datetime] = {}

        stable: dict[str, tuple[int, base.Point] | None] = {}
        for gate_name, gate in base.GATE_SEGMENTS.items():
            first_point = vehicle_points[0] if vehicle_points else None
            if first_point is None:
                stable[gate_name] = None
                continue
            first_side = stable_side(base.signed_gate_distance_m(first_point, gate))
            stable[gate_name] = (first_side, first_point) if first_side else None

        for current in vehicle_points[1:]:
            for gate_name, gate in base.GATE_SEGMENTS.items():
                current_side = stable_side(base.signed_gate_distance_m(current, gate))
                if current_side == 0:
                    continue

                previous = stable.get(gate_name)
                if previous is None:
                    stable[gate_name] = (current_side, current)
                    continue

                previous_side, previous_point = previous
                if current_side == previous_side:
                    stable[gate_name] = (current_side, current)
                    continue

                gap = (current.timestamp - previous_point.timestamp).total_seconds()
                if gap <= 0 or gap > base.MAX_GPS_GAP_SECONDS:
                    stable[gate_name] = (current_side, current)
                    continue

                direction: Literal["entry", "exit"] = (
                    "entry" if previous_side == 1 and current_side == -1 else "exit"
                )
                fraction = base.crossing_fraction(previous_point, current, gate)
                stable[gate_name] = (current_side, current)
                if fraction is None:
                    continue

                timestamp = previous_point.timestamp + (
                    current.timestamp - previous_point.timestamp
                ) * fraction
                key = (gate_name, direction)
                prior_event = last_by_gate_direction.get(key)
                if prior_event and (
                    timestamp - prior_event
                ).total_seconds() < base.CROSSING_COOLDOWN_SECONDS:
                    continue

                lat = previous_point.lat + (current.lat - previous_point.lat) * fraction
                lon = previous_point.lon + (current.lon - previous_point.lon) * fraction
                crossings.append(
                    base.Crossing(
                        plate=plate,
                        timestamp=timestamp,
                        gate=gate_name,
                        direction=direction,
                        lat=lat,
                        lon=lon,
                    )
                )
                last_by_gate_direction[key] = timestamp

    return sorted(crossings, key=lambda item: (item.timestamp, item.plate, item.gate))


def create_report_without_map_column(*args: Any, **kwargs: Any) -> None:
    """Build the standard report and remove its dedicated map-link column."""
    _original_create_report(*args, **kwargs)
    output_path = Path(args[0] if args else kwargs["output_path"])

    workbook = load_workbook(output_path)
    try:
        if "Первый въезд" not in workbook.sheetnames:
            raise RuntimeError("В отчёте отсутствует лист «Первый въезд»")
        sheet = workbook["Первый въезд"]

        # Column E in the base report contains only «Показать на карте» links.
        sheet.delete_cols(5, 1)
        max_row = max(sheet.max_row, 1)
        sheet.auto_filter.ref = f"A1:J{max_row}"

        widths = [6, 15, 13, 13, 15, 24, 10, 38, 45, 55]
        for index, width in enumerate(widths, 1):
            sheet.column_dimensions[get_column_letter(index)].width = width
        sheet.column_dimensions["K"].hidden = True

        workbook.save(output_path)
    finally:
        workbook.close()


base.load_coordinate_points = load_coordinate_points
base.detect_coordinate_crossings = detect_coordinate_crossings
base.create_report = create_report_without_map_column


if __name__ == "__main__":
    try:
        base.main()
    except Exception as exc:
        print(f"ОШИБКА: {exc}")
        raise
