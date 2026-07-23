from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from openpyxl import load_workbook


ALIASES = {
    "plate": ["номерной знак", "госномер", "plaka", "license plate"],
    "time": ["дата / время", "дата/время", "tarih / saat", "date / time"],
    "odometer": ["одометр", "odometer"],
    "distance": ["расстояние (км)", "расстояние", "distance"],
    "speed": ["скорость", "speed"],
    "region": ["область", "регион", "region"],
    "address": ["адрес", "address"],
    "lat": ["latitudine", "широта", "latitude", "enlem"],
    "lon": ["долгота", "longitude", "boylam"],
}


@dataclass
class Point:
    plate: str
    time: datetime
    lat: float
    lon: float
    odometer: Optional[float] = None
    source_distance: Optional[float] = None
    speed: Optional[float] = None
    region: str = ""
    address: str = ""


def norm(value: Any) -> str:
    return " ".join(str(value or "").replace("\n", " ").split()).strip().lower().replace("ё", "е")


def as_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def as_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if value in (None, ""):
        return None
    text = str(value).strip()
    for fmt in (
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def detect_columns(headers: list[Any]) -> dict[str, int]:
    normalized = [norm(value) for value in headers]
    result: dict[str, int] = {}
    for key, aliases in ALIASES.items():
        for index, header in enumerate(normalized):
            if any(norm(alias) == header or norm(alias) in header for alias in aliases):
                result[key] = index
                break
    return result


def is_header_row(row: Iterable[Any]) -> bool:
    columns = detect_columns(list(row))
    return all(key in columns for key in ("plate", "time", "lat", "lon"))


def rows_to_points(headers: list[Any], rows: Iterable[Iterable[Any]]) -> tuple[list[Point], int]:
    columns = detect_columns(headers)
    missing = [key for key in ("plate", "time", "lat", "lon") if key not in columns]
    if missing:
        raise ValueError("Не найдены обязательные колонки: " + ", ".join(missing))

    points: list[Point] = []
    skipped = 0
    for source_row in rows:
        row = list(source_row)
        if len(row) <= max(columns.values()):
            skipped += 1
            continue
        plate = str(row[columns["plate"]] or "").strip()
        timestamp = as_datetime(row[columns["time"]])
        lat = as_float(row[columns["lat"]])
        lon = as_float(row[columns["lon"]])
        if not plate or timestamp is None or lat is None or lon is None:
            skipped += 1
            continue
        points.append(
            Point(
                plate=plate,
                time=timestamp,
                lat=lat,
                lon=lon,
                odometer=as_float(row[columns["odometer"]]) if "odometer" in columns else None,
                source_distance=as_float(row[columns["distance"]]) if "distance" in columns else None,
                speed=as_float(row[columns["speed"]]) if "speed" in columns else None,
                region=str(row[columns["region"]] or "").strip() if "region" in columns else "",
                address=str(row[columns["address"]] or "").strip() if "address" in columns else "",
            )
        )
    return points, skipped


def load_excel(path: Path) -> tuple[list[Point], dict[str, int]]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    sheet = workbook.worksheets[0]
    rows = list(sheet.iter_rows(values_only=True))
    workbook.close()
    header_index = next((index for index, row in enumerate(rows[:50]) if is_header_row(row)), None)
    if header_index is None:
        raise ValueError("Не найдена строка заголовков в Excel")
    points, skipped = rows_to_points(list(rows[header_index]), rows[header_index + 1 :])
    return points, {"loaded": len(points), "skipped": skipped}


def read_csv_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1251", "cp1254"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("Не удалось определить кодировку CSV")


def load_csv(path: Path) -> tuple[list[Point], dict[str, int]]:
    text = read_csv_text(path)
    try:
        delimiter = csv.Sniffer().sniff(text[:10000], delimiters=";,\t|").delimiter
    except csv.Error:
        delimiter = ";"
    rows = list(csv.reader(text.splitlines(), delimiter=delimiter))
    header_index = next((index for index, row in enumerate(rows[:100]) if is_header_row(row)), None)
    if header_index is None:
        raise ValueError("Не найдена строка заголовков в CSV")
    points, skipped = rows_to_points(rows[header_index], rows[header_index + 1 :])
    return points, {"loaded": len(points), "skipped": skipped}


def load_points(path: Path) -> tuple[list[Point], dict[str, int]]:
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        return load_excel(path)
    if suffix == ".csv":
        return load_csv(path)
    raise ValueError("Поддерживаются только XLSX, XLSM и CSV")
