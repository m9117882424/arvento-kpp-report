from __future__ import annotations

"""Runtime fixes for violation preview and one-decimal web formatting."""

from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

import report_portal as implementation
import run_report_portal as current
from regional_speed_report import REGION_SHEET_NAME, ROUTE_SHEET_NAME, SITE_SHEET_NAME
from speed_violation_report import TURN_SHEET_NAME


_original_json_cell = implementation.json_cell


def json_cell_one_decimal(value: Any) -> Any:
    if isinstance(value, float):
        return f"{value:.1f}"
    return _original_json_cell(value)


implementation.json_cell = json_cell_one_decimal
current.implementation.json_cell = json_cell_one_decimal


def violation_web_preview(path: Path) -> tuple[list[str], list[list[Any]], int]:
    columns = [
        "Госномер",
        "Тип нарушения",
        "Дата",
        "Начало",
        "Окончание",
        "Максимальная скорость, км/ч",
        "Порог, км/ч",
        "Адрес",
        "Карта",
    ]
    records: list[tuple[str, datetime | None, list[Any]]] = []
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        for sheet_name in (
            TURN_SHEET_NAME,
            SITE_SHEET_NAME,
            ROUTE_SHEET_NAME,
            REGION_SHEET_NAME,
        ):
            if sheet_name not in workbook.sheetnames:
                continue
            sheet = workbook[sheet_name]
            headers = current._header_map(sheet)
            for row in sheet.iter_rows(min_row=2, values_only=True):
                plate = str(current._value(row, headers, "Госномер") or "").strip()
                if not plate:
                    continue

                if sheet_name == TURN_SHEET_NAME:
                    start = current._value(row, headers, "Начало прохода")
                    finish = current._value(row, headers, "Окончание прохода")
                    start_speed = current._as_number(
                        current._value(row, headers, "Скорость в начале")
                    )
                    finish_speed = current._as_number(
                        current._value(row, headers, "Скорость в конце")
                    )
                    speeds = [value for value in (start_speed, finish_speed) if value is not None]
                    max_speed = max(speeds) if speeds else None
                    address_parts = [
                        str(current._value(row, headers, "Адрес начала") or "").strip(),
                        str(current._value(row, headers, "Адрес окончания") or "").strip(),
                    ]
                    address = " → ".join(part for part in address_parts if part)
                    map_url = current._map_url(
                        current._value(row, headers, "Координаты начала")
                        or current._value(row, headers, "Координаты окончания")
                    )
                    threshold = None
                else:
                    start = current._value(row, headers, "Начало нарушения")
                    finish = current._value(row, headers, "Окончание нарушения")
                    max_speed = current._as_number(
                        current._value(row, headers, "Максимальная скорость, км/ч")
                    )
                    threshold = current._as_number(
                        current._value(row, headers, "Порог фиксации, км/ч")
                    )
                    address = str(
                        current._value(row, headers, "Адрес максимума") or ""
                    ).strip()
                    map_url = current._map_url(
                        current._value(row, headers, "Координаты максимума")
                    )

                event_date = (
                    start.date()
                    if isinstance(start, datetime)
                    else current._value(row, headers, "Дата")
                )
                display = [
                    plate,
                    sheet_name,
                    json_cell_one_decimal(event_date),
                    json_cell_one_decimal(start),
                    json_cell_one_decimal(finish),
                    json_cell_one_decimal(max_speed),
                    json_cell_one_decimal(threshold),
                    address,
                    map_url,
                ]
                records.append(
                    (plate, start if isinstance(start, datetime) else None, display)
                )
    finally:
        workbook.close()

    records.sort(key=lambda item: (item[0], item[1] or datetime.min, item[2][1]))
    rows = [item[2] for item in records]
    return columns, rows, len(rows)


current.violation_web_preview = violation_web_preview
