#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Формирование отчёта по проездам через KPP 4/KPP 5 из выгрузки Arvento."""

from __future__ import annotations

import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

KPP4_REGION = "KPP 4 TEST"
KPP5_REGION = "KPP 5 TEST"
KPP5_POINT_1 = (36.15886644084745, 33.549775396083724)
KPP5_POINT_2 = (36.159167964778234, 33.55101996362877)

SIDE_HYSTERESIS_M = 18.0
GATE_MARGIN_M = 45.0
MAX_CROSSING_WINDOW_SECONDS = 15 * 60
EVENT_COOLDOWN_SECONDS = 120
MAX_ODOMETER_DELTA_KM = 20.0
MAX_REASONABLE_SPEED_KMH = 180.0

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


@dataclass
class Gate:
    name: str
    p1: tuple[float, float]
    p2: tuple[float, float]
    sample_count: int


@dataclass
class Event:
    plate: str
    time: datetime
    kind: str
    direction: str
    gate: str
    lat: float
    lon: float
    odometer: Optional[float]
    speed: Optional[float]
    confidence: str


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
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def local_xy(lat: float, lon: float, origin_lat: float, origin_lon: float) -> tuple[float, float]:
    y = (lat - origin_lat) * 111_320.0
    x = (lon - origin_lon) * 111_320.0 * math.cos(math.radians(origin_lat))
    return x, y


def gate_geometry(gate: Gate):
    mid_lat = (gate.p1[0] + gate.p2[0]) / 2
    mid_lon = (gate.p1[1] + gate.p2[1]) / 2
    x1, y1 = local_xy(*gate.p1, mid_lat, mid_lon)
    x2, y2 = local_xy(*gate.p2, mid_lat, mid_lon)
    vx, vy = x2 - x1, y2 - y1
    length = math.hypot(vx, vy)
    if length < 0.1:
        vx, vy, length = 20.0, 0.0, 20.0
    return mid_lat, mid_lon, vx, vy, length


def gate_coords(point: Point, gate: Gate) -> tuple[float, float]:
    mid_lat, mid_lon, vx, vy, length = gate_geometry(gate)
    px, py = local_xy(point.lat, point.lon, mid_lat, mid_lon)
    ux, uy = vx / length, vy / length
    nx, ny = -uy, ux
    along = px * ux + py * uy
    across = px * nx + py * ny
    if ny < 0:
        across = -across
    return along, across


def side(point: Point, gate: Gate) -> int:
    _, across = gate_coords(point, gate)
    if across > SIDE_HYSTERESIS_M:
        return 1
    if across < -SIDE_HYSTERESIS_M:
        return -1
    return 0


def near_gate(point: Point, gate: Gate) -> bool:
    along, across = gate_coords(point, gate)
    length = gate_geometry(gate)[4]
    return abs(across) <= GATE_MARGIN_M and abs(along) <= length / 2 + GATE_MARGIN_M


def crossing_fraction(p1: Point, p2: Point, gate: Gate) -> float:
    a1 = gate_coords(p1, gate)[1]
    a2 = gate_coords(p2, gate)[1]
    d = a2 - a1
    return 0.5 if abs(d) < 1e-12 else max(0.0, min(1.0, -a1 / d))


def haversine_km(p1: Point, p2: Point) -> float:
    r = 6371.0088
    lat1, lat2 = math.radians(p1.lat), math.radians(p2.lat)
    dlat = math.radians(p2.lat - p1.lat)
    dlon = math.radians(p2.lon - p1.lon)
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def segment_distance(p1: Point, p2: Point) -> tuple[float, str]:
    gap = (p2.time - p1.time).total_seconds()
    if gap <= 0:
        return 0.0, "invalid"
    if p1.odometer is not None and p2.odometer is not None:
        delta = p2.odometer - p1.odometer
        if 0 <= delta <= MAX_ODOMETER_DELTA_KM:
            return delta, "odometer"
    if p2.source_distance is not None and 0 <= p2.source_distance <= MAX_ODOMETER_DELTA_KM:
        return p2.source_distance, "source"
    gps = haversine_km(p1, p2)
    if gps / (gap / 3600) <= MAX_REASONABLE_SPEED_KMH:
        return gps, "gps"
    return 0.0, "rejected"


def choose_file() -> Path:
    if len(sys.argv) > 1:
        return Path(sys.argv[1]).expanduser().resolve()
    from tkinter import Tk, filedialog
    root = Tk(); root.withdraw()
    selected = filedialog.askopenfilename(title="Выберите выгрузку Arvento", filetypes=[("Excel", "*.xlsx *.xlsm"), ("Все файлы", "*.*")])
    root.destroy()
    if not selected:
        raise SystemExit("Файл не выбран")
    return Path(selected).resolve()


def load_points(path: Path) -> tuple[list[Point], dict[str, int]]:
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = ws.iter_rows(values_only=True)
    headers = [norm(v) for v in next(rows)]
    cols: dict[str, int] = {}
    for key, aliases in ALIASES.items():
        for i, header in enumerate(headers):
            if any(norm(alias) == header or norm(alias) in header for alias in aliases):
                cols[key] = i; break
    missing = [k for k in ("plate", "time", "lat", "lon") if k not in cols]
    if missing:
        raise ValueError("Не найдены колонки: " + ", ".join(missing))
    points: list[Point] = []
    skipped = 0
    for row in rows:
        plate = str(row[cols["plate"]] or "").strip()
        time = as_datetime(row[cols["time"]])
        lat = as_float(row[cols["lat"]]); lon = as_float(row[cols["lon"]])
        if not plate or time is None or lat is None or lon is None:
            skipped += 1; continue
        points.append(Point(
            plate=plate, time=time, lat=lat, lon=lon,
            odometer=as_float(row[cols["odometer"]]) if "odometer" in cols else None,
            source_distance=as_float(row[cols["distance"]]) if "distance" in cols else None,
            speed=as_float(row[cols["speed"]]) if "speed" in cols else None,
            region=str(row[cols["region"]] or "").strip() if "region" in cols else "",
            address=str(row[cols["address"]] or "").strip() if "address" in cols else "",
        ))
    wb.close()
    return points, {"loaded": len(points), "skipped": skipped}


def build_gates(points: list[Point]) -> list[Gate]:
    kpp4 = [p for p in points if KPP4_REGION.lower() in p.region.lower()]
    if not kpp4:
        raise ValueError(f"Не найдена геозона {KPP4_REGION}")
    lat = statistics.median(p.lat for p in kpp4)
    lon = statistics.median(p.lon for p in kpp4)
    lon_delta = 25 / (111_320 * math.cos(math.radians(lat)))
    gate4 = Gate(KPP4_REGION, (lat, lon - lon_delta / 2), (lat, lon + lon_delta / 2), len(kpp4))
    count5 = sum(KPP5_REGION.lower() in p.region.lower() for p in points)
    gate5 = Gate(KPP5_REGION, KPP5_POINT_1, KPP5_POINT_2, count5)
    return [gate4, gate5]


def analyze(points: list[Point], gates: list[Gate]):
    grouped: dict[str, list[Point]] = defaultdict(list)
    for p in points:
        grouped[p.plate].append(p)
    summaries, events, sessions, diagnostics = [], [], [], []

    for plate in sorted(grouped):
        pts = sorted(grouped[plate], key=lambda p: p.time)
        if len(pts) < 2:
            continue
        stable = {g.name: side(pts[0], g) for g in gates}
        stable_time = {g.name: pts[0].time for g in gates}
        inside = False
        first_event: Optional[str] = None
        entry_time = None; entry_gate = ""; session_km = 0.0
        inside_km = outside_km = total_km = 0.0
        entries = exits = uncertain = 0
        last_event_time: Optional[datetime] = None
        sources = defaultdict(float)

        for p1, p2 in zip(pts, pts[1:]):
            gap = (p2.time - p1.time).total_seconds()
            if gap <= 0:
                continue
            distance, source = segment_distance(p1, p2)
            total_km += distance; sources[source] += distance
            candidates = []
            for gate in gates:
                s2 = side(p2, gate)
                previous = stable[gate.name]
                if s2 != 0 and previous != 0 and s2 != previous:
                    elapsed = (p2.time - stable_time[gate.name]).total_seconds()
                    if elapsed <= MAX_CROSSING_WINDOW_SECONDS and (near_gate(p1, gate) or near_gate(p2, gate)):
                        kind = "Въезд" if previous == 1 and s2 == -1 else "Выезд"
                        direction = "Север → Юг" if kind == "Въезд" else "Юг → Север"
                        candidates.append((crossing_fraction(p1, p2, gate), gate, kind, direction))
                if s2 != 0:
                    stable[gate.name] = s2; stable_time[gate.name] = p2.time

            candidate = min(candidates, key=lambda x: x[0]) if candidates else None
            if candidate:
                fraction, gate, kind, direction = candidate
                event_time = p1.time + (p2.time - p1.time) * fraction
                if last_event_time and (event_time - last_event_time).total_seconds() < EVENT_COOLDOWN_SECONDS:
                    candidate = None
            if not candidate:
                if inside:
                    inside_km += distance; session_km += distance
                else:
                    outside_km += distance
                continue

            fraction, gate, kind, direction = candidate
            event_time = p1.time + (p2.time - p1.time) * fraction
            before, after = distance * fraction, distance * (1 - fraction)
            if first_event is None:
                first_event = kind
                if kind == "Выезд":
                    inside = True; entry_time = pts[0].time; entry_gate = "Начало периода"
                    diagnostics.append([plate, pts[0].time, "Первым событием был выезд: начало периода принято как внутри", pts[0].lat, pts[0].lon])

            if kind == "Въезд":
                if inside:
                    inside_km += distance; session_km += distance
                    diagnostics.append([plate, event_time, f"Повторный въезд через {gate.name} без выезда", p2.lat, p2.lon])
                    continue
                outside_km += before; inside_km += after
                inside = True; entry_time = event_time; entry_gate = gate.name; session_km = after; entries += 1
            else:
                if not inside:
                    outside_km += distance
                    diagnostics.append([plate, event_time, f"Выезд через {gate.name} без найденного въезда", p2.lat, p2.lon])
                    continue
                inside_km += before; outside_km += after; session_km += before; exits += 1
                sessions.append([plate, entry_time or pts[0].time, event_time, entry_gate or "Начало периода", gate.name,
                                 int((event_time - (entry_time or pts[0].time)).total_seconds()), session_km, "Завершён"])
                inside = False; entry_time = None; entry_gate = ""; session_km = 0.0

            lat = p1.lat + (p2.lat - p1.lat) * fraction
            lon = p1.lon + (p2.lon - p1.lon) * fraction
            odo = None
            if p1.odometer is not None and p2.odometer is not None:
                odo = p1.odometer + (p2.odometer - p1.odometer) * fraction
            events.append(Event(plate, event_time, kind, direction, gate.name, lat, lon, odo, p2.speed, "Высокая" if gap <= 30 else "Средняя"))
            last_event_time = event_time

        if inside and entry_time is not None:
            sessions.append([plate, entry_time, None, entry_gate, "", None, session_km, "Не найден выезд"])
        summaries.append([plate, pts[0].time, pts[-1].time,
                          "Внутри" if first_event == "Выезд" else "Снаружи", "Внутри" if inside else "Снаружи",
                          entries, exits, inside_km, outside_km, total_km,
                          inside_km + outside_km - total_km, uncertain,
                          sources["odometer"], sources["source"] + sources["gps"], sources["rejected"]])
    return summaries, events, sessions, diagnostics


def style_sheet(ws, freeze: str = "A2"):
    ws.freeze_panes = freeze
    ws.auto_filter.ref = ws.dimensions
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for col in range(1, ws.max_column + 1):
        width = min(max(len(str(ws.cell(r, col).value or "")) for r in range(1, min(ws.max_row, 300) + 1)) + 2, 45)
        ws.column_dimensions[get_column_letter(col)].width = max(width, 10)


def group_by_plate(ws, start_row: int = 2):
    start = start_row
    while start <= ws.max_row:
        plate = ws.cell(start, 1).value
        end = start
        while end + 1 <= ws.max_row and ws.cell(end + 1, 1).value == plate:
            end += 1
        if end > start:
            ws.row_dimensions.group(start, end, hidden=False)
        start = end + 1


def save_report(path: Path, source: Path, gates: list[Gate], stats, summaries, events, sessions, diagnostics):
    wb = Workbook()
    ws = wb.active; ws.title = "Сводка"
    ws.append(["Госномер", "Начало периода", "Конец периода", "Состояние в начале", "Состояние в конце",
               "Въездов", "Выездов", "Пробег внутри, км", "Пробег снаружи, км", "Общий пробег, км",
               "Проверка суммы, км", "Неуверенных пересечений", "По одометру, км", "По источнику/GPS, км", "Отброшено, км"])
    for row in summaries: ws.append(row)
    for row in ws.iter_rows(min_row=2):
        row[1].number_format = row[2].number_format = "dd.mm.yyyy hh:mm:ss"
        for c in range(7, 15): row[c].number_format = "0.000"
    style_sheet(ws)

    we = wb.create_sheet("Проезды КПП")
    we.append(["Госномер", "Дата/время", "Событие", "Направление", "КПП", "Широта", "Долгота", "Одометр, км", "Скорость, км/ч", "Достоверность"])
    for e in sorted(events, key=lambda x: (x.plate, x.time)):
        we.append([e.plate, e.time, e.kind, e.direction, e.gate, e.lat, e.lon, e.odometer, e.speed, e.confidence])
    for row in we.iter_rows(min_row=2):
        row[1].number_format = "dd.mm.yyyy hh:mm:ss"
        row[5].number_format = row[6].number_format = "0.000000"
        row[7].number_format = row[8].number_format = "0.000"
    style_sheet(we); group_by_plate(we)

    wi = wb.create_sheet("Периоды внутри")
    wi.append(["Госномер", "Время въезда", "Время выезда", "КПП въезда", "КПП выезда", "Время внутри", "Пробег внутри, км", "Статус"])
    for plate, entry, exit_, gate_in, gate_out, seconds, km, status in sorted(sessions, key=lambda x: (x[0], x[1])):
        duration = "" if seconds is None else f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
        wi.append([plate, entry, exit_, gate_in, gate_out, duration, km, status])
    for row in wi.iter_rows(min_row=2):
        row[1].number_format = row[2].number_format = "dd.mm.yyyy hh:mm:ss"
        row[6].number_format = "0.000"
    style_sheet(wi); group_by_plate(wi)

    wd = wb.create_sheet("Диагностика")
    wd.append(["Госномер", "Дата/время", "Причина", "Широта", "Долгота"])
    for row in sorted(diagnostics, key=lambda x: (x[0], x[1])): wd.append(row)
    for row in wd.iter_rows(min_row=2):
        row[1].number_format = "dd.mm.yyyy hh:mm:ss"
        row[3].number_format = row[4].number_format = "0.000000"
    style_sheet(wd); group_by_plate(wd)

    info = wb.create_sheet("Параметры")
    info.append(["Параметр", "Значение"])
    info.append(["Исходный файл", str(source)])
    info.append(["Загружено / пропущено", f"{stats['loaded']} / {stats['skipped']}"])
    for gate in gates:
        info.append([gate.name, f"{gate.p1} -> {gate.p2}; точек зоны: {gate.sample_count}"])
    style_sheet(info)
    wb.save(path)


def main():
    source = choose_file()
    print(f"Чтение: {source}")
    points, stats = load_points(source)
    gates = build_gates(points)
    for g in gates:
        print(f"{g.name}: {g.p1} -> {g.p2}; точек зоны: {g.sample_count}")
    summaries, events, sessions, diagnostics = analyze(points, gates)
    output = source.with_name(source.stem + "_КПП_отчет.xlsx")
    save_report(output, source, gates, stats, summaries, events, sessions, diagnostics)
    print(f"Готово: {output}")
    print(f"Автомобилей: {len(summaries)}; событий: {len(events)}; периодов внутри: {len(sessions)}")


if __name__ == "__main__":
    main()
