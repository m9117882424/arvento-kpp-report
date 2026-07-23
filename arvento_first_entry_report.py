#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Отчёт по первому въезду на территорию.

Берёт события Arvento, оставляет первый въезд каждого автомобиля за день,
подставляет данные водителя из Excel-разнарядки и формирует XLSX-отчёт.
Фильтры по грейду и времени опциональны. В грейде учитывается только число:
7, 7a, 7A, 7b считаются грейдом 7.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any, Iterable, Optional

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

TARGET_GATES = ("KPP 4 TEST", "KPP 5 TEST", "KPP 4", "KPP 5")

ARVENTO_ALIASES = {
    "plate": ("Номерной знак", "Госномер", "Plaka", "License Plate"),
    "datetime": ("Дата / время", "Дата/время", "Tarih / Saat", "Date / Time"),
    "date": ("Дата въезда", "Дата", "Tarih", "Date"),
    "time": ("Время въезда", "Время", "Saat", "Time"),
    "region": ("Область", "Регион", "Геозона", "Region"),
    "lat": ("Latitudine", "Широта", "Latitude", "Enlem"),
    "lon": ("Долгота", "Longitude", "Boylam"),
}

ROSTER_ALIASES = {
    "plate": ("Гос рег знак", "PLAKA", "Госномер", "Номерной знак"),
    "model": ("Марка, модель", "Marka, model", "Модель"),
    "grade": ("Грейд", "SCALA", "Grade"),
    "driver": ("Пользователь", "KULLANICI", "Водитель"),
    "position": ("Должность", "GÖREVİ", "GOREVI"),
    "directorate": ("Дирекция", "Directorate"),
}


@dataclass
class VehicleInfo:
    plate: str
    model: str = ""
    grade: str = ""
    driver: str = ""
    position: str = ""
    directorate: str = ""


@dataclass
class Entry:
    plate: str
    timestamp: datetime
    gate: str
    lat: Optional[float] = None
    lon: Optional[float] = None


@dataclass
class ReportFilters:
    grade_from: Optional[float] = None
    grade_to: Optional[float] = None
    time_from: Optional[time] = None
    time_to: Optional[time] = None


def clean(value: Any) -> str:
    return " ".join(str(value or "").replace("\n", " ").split()).strip()


def normalized(value: Any) -> str:
    return clean(value).lower().replace("ё", "е").replace("ı", "i")


def normalize_plate(value: Any) -> str:
    return "".join(ch for ch in clean(value).upper() if ch.isalnum())


def parse_grade_number(value: Any) -> Optional[float]:
    """Возвращает только числовую часть грейда: 7a -> 7, 14B -> 14."""
    match = re.search(r"\d+(?:[.,]\d+)?", clean(value))
    return float(match.group().replace(",", ".")) if match else None


def parse_clock(value: Optional[str]) -> Optional[time]:
    text = clean(value)
    if not text:
        return None
    for fmt in ("%H", "%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            pass
    raise ValueError(f"Некорректное время: {value}")


def parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    text = clean(value)
    for fmt in (
        "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M",
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
        "%m/%d/%Y %H:%M:%S",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def parse_date_and_time(date_value: Any, time_value: Any) -> Optional[datetime]:
    if isinstance(date_value, datetime):
        date_part = date_value.date()
    else:
        date_part = None
        text = clean(date_value)
        for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%m/%d/%Y"):
            try:
                date_part = datetime.strptime(text, fmt).date()
                break
            except ValueError:
                pass
    if not date_part:
        return None
    if isinstance(time_value, datetime):
        time_part = time_value.time()
    elif isinstance(time_value, time):
        time_part = time_value
    else:
        time_part = parse_clock(clean(time_value))
    return datetime.combine(date_part, time_part or time(0, 0))


def find_column(headers: list[str], aliases: Iterable[str]) -> Optional[int]:
    values = [normalized(h) for h in headers]
    for alias in aliases:
        needle = normalized(alias)
        for index, header in enumerate(values):
            if header == needle or needle in header:
                return index
    return None


def sheet_rows(path: Path):
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        for sheet in workbook.worksheets:
            rows = list(sheet.iter_rows(values_only=True))
            if rows:
                yield sheet.title, rows
    finally:
        workbook.close()


def find_header(rows: list[tuple], aliases: dict[str, Iterable[str]], required: tuple[str, ...]):
    for row_index, row in enumerate(rows[:30]):
        headers = [clean(value) for value in row]
        columns = {key: find_column(headers, names) for key, names in aliases.items()}
        if all(columns.get(key) is not None for key in required):
            return row_index, columns
    return None, None


def load_roster(path: Path) -> dict[str, VehicleInfo]:
    result: dict[str, VehicleInfo] = {}
    for _, rows in sheet_rows(path):
        header_row, columns = find_header(rows, ROSTER_ALIASES, ("plate",))
        if columns is None:
            continue
        for row in rows[header_row + 1:]:
            plate = normalize_plate(row[columns["plate"]] if columns["plate"] < len(row) else "")
            if not plate:
                continue
            def get(name: str) -> str:
                index = columns.get(name)
                return clean(row[index]) if index is not None and index < len(row) else ""
            result[plate] = VehicleInfo(
                plate=plate,
                model=get("model"), grade=get("grade"), driver=get("driver"),
                position=get("position"), directorate=get("directorate"),
            )
    if not result:
        raise ValueError("В разнарядке не найден столбец с госномером")
    return result


def load_entries(path: Path) -> list[Entry]:
    entries: list[Entry] = []
    for _, rows in sheet_rows(path):
        header_row, columns = find_header(rows, ARVENTO_ALIASES, ("plate", "region"))
        if columns is None:
            continue
        if columns.get("datetime") is None and (columns.get("date") is None or columns.get("time") is None):
            continue
        for row in rows[header_row + 1:]:
            def value(name: str):
                index = columns.get(name)
                return row[index] if index is not None and index < len(row) else None
            plate = normalize_plate(value("plate"))
            gate = clean(value("region"))
            if not plate or not any(name.lower() in gate.lower() for name in TARGET_GATES):
                continue
            timestamp = parse_datetime(value("datetime")) if columns.get("datetime") is not None else parse_date_and_time(value("date"), value("time"))
            if not timestamp:
                continue
            try:
                lat = float(str(value("lat")).replace(",", ".")) if value("lat") not in (None, "") else None
                lon = float(str(value("lon")).replace(",", ".")) if value("lon") not in (None, "") else None
            except ValueError:
                lat = lon = None
            entries.append(Entry(plate, timestamp, gate, lat, lon))
    if not entries:
        raise ValueError("В выгрузке Arvento не найдены события въезда через KPP 4/KPP 5")
    first_by_day: dict[tuple[str, object], Entry] = {}
    for entry in entries:
        key = (entry.plate, entry.timestamp.date())
        if key not in first_by_day or entry.timestamp < first_by_day[key].timestamp:
            first_by_day[key] = entry
    return sorted(first_by_day.values(), key=lambda item: (item.timestamp, item.plate))


def apply_filters(entries: list[Entry], roster: dict[str, VehicleInfo], filters: ReportFilters) -> list[Entry]:
    result = []
    for entry in entries:
        info = roster.get(entry.plate)
        if filters.grade_from is not None or filters.grade_to is not None:
            grade = parse_grade_number(info.grade if info else "")
            if grade is None:
                continue
            if filters.grade_from is not None and grade < filters.grade_from:
                continue
            if filters.grade_to is not None and grade > filters.grade_to:
                continue
        current = entry.timestamp.time()
        if filters.time_from is not None and current < filters.time_from:
            continue
        if filters.time_to is not None and current > filters.time_to:
            continue
        result.append(entry)
    return result


def create_report(path: Path, entries: list[Entry], roster: dict[str, VehicleInfo]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Первый въезд"
    headers = ["№", "Номерной знак", "Дата въезда", "Время въезда", "", "Геозона", "Марка, модель", "Грейд", "Водитель", "Должность", "Дирекция"]
    sheet.append(headers)
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="4472C4")
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for number, entry in enumerate(entries, 1):
        info = roster.get(entry.plate, VehicleInfo(entry.plate))
        map_text = "Показать на карте"
        sheet.append([number, entry.plate, entry.timestamp.date(), entry.timestamp.time(), map_text, entry.gate, info.model, info.grade, info.driver, info.position, info.directorate])
        row = sheet.max_row
        if entry.lat is not None and entry.lon is not None:
            sheet.cell(row, 5).hyperlink = f"https://www.google.com/maps?q={entry.lat:.7f},{entry.lon:.7f}"
            sheet.cell(row, 5).style = "Hyperlink"
        sheet.cell(row, 3).number_format = "dd.mm.yyyy"
        sheet.cell(row, 4).number_format = "hh:mm:ss"
    widths = [6, 15, 13, 13, 20, 15, 24, 10, 38, 45, 55]
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for row in sheet.iter_rows():
        for cell in row:
            cell.alignment = Alignment(vertical="center", wrap_text=cell.column >= 9)
    workbook.save(path)


def choose_file(title: str) -> Path:
    from tkinter import Tk, filedialog
    root = Tk(); root.withdraw()
    try:
        selected = filedialog.askopenfilename(title=title, filetypes=[("Excel", "*.xlsx *.xlsm")])
    finally:
        root.destroy()
    if not selected:
        raise SystemExit("Файл не выбран")
    return Path(selected)


def ask_filters() -> ReportFilters:
    from tkinter import Tk, simpledialog
    root = Tk(); root.withdraw()
    try:
        grade_from = simpledialog.askstring("Фильтр", "Грейд от, например 7. Пусто — без фильтра:", parent=root)
        grade_to = simpledialog.askstring("Фильтр", "Грейд до, например 14. Пусто — без фильтра:", parent=root)
        time_from = simpledialog.askstring("Фильтр", "Время от, например 7 или 07:00. Пусто — без фильтра:", parent=root)
        time_to = simpledialog.askstring("Фильтр", "Время до, например 9 или 09:00. Пусто — без фильтра:", parent=root)
    finally:
        root.destroy()
    return ReportFilters(parse_grade_number(grade_from), parse_grade_number(grade_to), parse_clock(time_from), parse_clock(time_to))


def main() -> None:
    parser = argparse.ArgumentParser(description="Отчёт по первому въезду")
    parser.add_argument("arvento", nargs="?")
    parser.add_argument("roster", nargs="?")
    parser.add_argument("output", nargs="?")
    parser.add_argument("--grade-from")
    parser.add_argument("--grade-to")
    parser.add_argument("--time-from")
    parser.add_argument("--time-to")
    parser.add_argument("--no-filter-dialog", action="store_true")
    args = parser.parse_args()

    arvento = Path(args.arvento) if args.arvento else choose_file("Выберите выгрузку Arvento")
    roster_path = Path(args.roster) if args.roster else choose_file("Выберите файл разнарядки")
    output = Path(args.output) if args.output else arvento.with_name(f"Первый въезд {arvento.stem}.xlsx")

    cli_filter = any(value is not None for value in (args.grade_from, args.grade_to, args.time_from, args.time_to))
    filters = ReportFilters(
        parse_grade_number(args.grade_from), parse_grade_number(args.grade_to),
        parse_clock(args.time_from), parse_clock(args.time_to),
    ) if cli_filter or args.no_filter_dialog else ask_filters()

    if filters.grade_from is not None and filters.grade_to is not None and filters.grade_from > filters.grade_to:
        raise ValueError("Минимальный грейд больше максимального")
    if filters.time_from is not None and filters.time_to is not None and filters.time_from > filters.time_to:
        raise ValueError("Начальное время позже конечного")

    roster = load_roster(roster_path)
    entries = apply_filters(load_entries(arvento), roster, filters)
    create_report(output, entries, roster)
    print(f"Готово: {output}")
    print(f"Строк: {len(entries)}")


if __name__ == "__main__":
    main()
