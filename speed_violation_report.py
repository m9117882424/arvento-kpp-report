#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Speed-violation detection and Excel sheets for the consolidated violations report.

The site/outside state uses the same KPP crossing logic as the passenger-vehicle
utilisation report. A violation is a continuous sequence of GPS points above the
threshold for the current state. The sequence is split when the state changes,
the speed returns to the permitted range, or the gap between points exceeds five
minutes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from arvento_analysis import EVENT_COOLDOWN_SECONDS, crossing_fraction, near_gate, side
from arvento_io import Point
from geozone_registry import Registry

SITE_SPEED_LIMIT_KMH = 30.0
OUTSIDE_SPEED_LIMIT_KMH = 95.0
SPEED_TOLERANCE_PERCENT = 10.0
SITE_SPEED_THRESHOLD_KMH = SITE_SPEED_LIMIT_KMH * 1.10
OUTSIDE_SPEED_THRESHOLD_KMH = OUTSIDE_SPEED_LIMIT_KMH * 1.10
MAX_SPEED_EVENT_GAP_SECONDS = 5 * 60

SITE_SHEET_NAME = "Скорость на площадке"
OUTSIDE_SHEET_NAME = "Скорость вне площадки"


@dataclass(frozen=True)
class SpeedViolation:
    plate: str
    start_point: Any
    finish_point: Any
    max_point: Any
    point_count: int
    speed_limit_kmh: float
    threshold_kmh: float
    on_site: bool

    @property
    def start(self) -> datetime:
        return self.start_point.timestamp

    @property
    def finish(self) -> datetime:
        return self.finish_point.timestamp

    @property
    def duration_seconds(self) -> int:
        return max(0, int((self.finish - self.start).total_seconds()))

    @property
    def max_speed_kmh(self) -> float:
        return float(self.max_point.speed)


def _as_gate_point(point: Any) -> Point:
    return Point(
        plate=point.plate,
        time=point.timestamp,
        lat=float(point.lat),
        lon=float(point.lon),
        speed=float(point.speed) if point.speed is not None else None,
        address=point.address or "",
    )


def _crossing_events(track: Sequence[Any], registry: Registry) -> dict[int, str]:
    """Return KPP crossing kind keyed by the index of the point after crossing."""
    if len(track) < 2:
        return {}

    first = _as_gate_point(track[0])
    stable_side = {gate.name: side(first, gate) for gate in registry.gates}
    stable_time = {gate.name: first.time for gate in registry.gates}
    last_event_time: datetime | None = None
    events: dict[int, str] = {}

    for index in range(1, len(track)):
        p1 = _as_gate_point(track[index - 1])
        p2 = _as_gate_point(track[index])
        candidates: list[tuple[float, str, datetime]] = []

        for gate in registry.gates:
            side2 = side(p2, gate)
            previous = stable_side[gate.name]
            if side2 != 0 and previous != 0 and side2 != previous:
                elapsed = (p2.time - stable_time[gate.name]).total_seconds()
                if elapsed <= 15 * 60 and (near_gate(p1, gate) or near_gate(p2, gate)):
                    fraction = crossing_fraction(p1, p2, gate)
                    kind = "Въезд" if previous == 1 and side2 == -1 else "Выезд"
                    event_time = p1.time + (p2.time - p1.time) * fraction
                    candidates.append((fraction, kind, event_time))
            if side2 != 0:
                stable_side[gate.name] = side2
                stable_time[gate.name] = p2.time

        if not candidates:
            continue

        _, kind, event_time = min(candidates, key=lambda item: item[0])
        if last_event_time is not None:
            if (event_time - last_event_time).total_seconds() < EVENT_COOLDOWN_SECONDS:
                continue
        events[index] = kind
        last_event_time = event_time

    return events


def classify_site_state(track: Sequence[Any], registry: Registry) -> list[tuple[Any, bool]]:
    """Classify each point as on-site or outside using KPP entry/exit events."""
    if not track:
        return []

    events = _crossing_events(track, registry)
    first_event = events[min(events)] if events else None
    inside = first_event == "Выезд"
    classified: list[tuple[Any, bool]] = [(track[0], inside)]

    for index in range(1, len(track)):
        kind = events.get(index)
        if kind == "Въезд":
            inside = True
        elif kind == "Выезд":
            inside = False
        classified.append((track[index], inside))

    return classified


def detect_speed_violations(
    track: Sequence[Any],
    registry: Registry,
) -> tuple[list[SpeedViolation], list[SpeedViolation]]:
    """Detect grouped speed violations on the site and outside the site."""
    site_violations: list[SpeedViolation] = []
    outside_violations: list[SpeedViolation] = []
    active: dict[str, Any] | None = None

    def close_active() -> None:
        nonlocal active
        if active is None:
            return
        violation = SpeedViolation(
            plate=active["start"].plate,
            start_point=active["start"],
            finish_point=active["finish"],
            max_point=active["max_point"],
            point_count=active["point_count"],
            speed_limit_kmh=active["limit"],
            threshold_kmh=active["threshold"],
            on_site=active["on_site"],
        )
        (site_violations if violation.on_site else outside_violations).append(violation)
        active = None

    for point, on_site in classify_site_state(track, registry):
        limit = SITE_SPEED_LIMIT_KMH if on_site else OUTSIDE_SPEED_LIMIT_KMH
        threshold = SITE_SPEED_THRESHOLD_KMH if on_site else OUTSIDE_SPEED_THRESHOLD_KMH
        speed = float(point.speed) if point.speed is not None else None
        exceeds = speed is not None and speed > threshold

        if active is not None:
            gap = (point.timestamp - active["finish"].timestamp).total_seconds()
            if (
                active["on_site"] != on_site
                or gap <= 0
                or gap > MAX_SPEED_EVENT_GAP_SECONDS
                or not exceeds
            ):
                close_active()

        if not exceeds:
            continue

        if active is None:
            active = {
                "start": point,
                "finish": point,
                "max_point": point,
                "point_count": 1,
                "limit": limit,
                "threshold": threshold,
                "on_site": on_site,
            }
            continue

        active["finish"] = point
        active["point_count"] += 1
        if speed > float(active["max_point"].speed):
            active["max_point"] = point

    close_active()
    return site_violations, outside_violations


def _style_sheet(sheet) -> None:
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


def _write_speed_sheet(workbook, title: str, violations: Sequence[SpeedViolation], index: int) -> None:
    if title in workbook.sheetnames:
        del workbook[title]
    sheet = workbook.create_sheet(title, index)
    sheet.append([
        "№",
        "Госномер",
        "Дата",
        "Начало нарушения",
        "Окончание нарушения",
        "Продолжительность",
        "Ограничение, км/ч",
        "Допуск, %",
        "Порог фиксации, км/ч",
        "Максимальная скорость, км/ч",
        "Превышение порога, км/ч",
        "Точек нарушения",
        "Координаты максимума",
        "Адрес максимума",
    ])

    for number, item in enumerate(violations, start=1):
        max_point = item.max_point
        sheet.append([
            number,
            item.plate,
            item.start.date(),
            item.start,
            item.finish,
            item.duration_seconds / 86400.0,
            item.speed_limit_kmh,
            SPEED_TOLERANCE_PERCENT / 100.0,
            item.threshold_kmh,
            item.max_speed_kmh,
            item.max_speed_kmh - item.threshold_kmh,
            item.point_count,
            f"{max_point.lat:.7f}, {max_point.lon:.7f}",
            max_point.address,
        ])

    for row in sheet.iter_rows(min_row=2):
        row[2].number_format = "dd.mm.yyyy"
        row[3].number_format = "dd.mm.yyyy hh:mm:ss"
        row[4].number_format = "dd.mm.yyyy hh:mm:ss"
        row[5].number_format = "[h]:mm:ss"
        row[6].number_format = "0.0"
        row[7].number_format = "0%"
        row[8].number_format = "0.0"
        row[9].number_format = "0.0"
        row[10].number_format = "0.0"
    _style_sheet(sheet)


def append_speed_sheets(
    workbook_path: Path,
    site_violations: Sequence[SpeedViolation],
    outside_violations: Sequence[SpeedViolation],
) -> None:
    """Append or replace both speed sheets in an existing violations workbook."""
    workbook = load_workbook(workbook_path)
    _write_speed_sheet(workbook, SITE_SHEET_NAME, site_violations, 1)
    _write_speed_sheet(workbook, OUTSIDE_SHEET_NAME, outside_violations, 2)

    if "Параметры" in workbook.sheetnames:
        settings = workbook["Параметры"]
        settings.append(["Ограничение скорости на площадке, км/ч", SITE_SPEED_LIMIT_KMH])
        settings.append(["Порог фиксации на площадке (+10%), км/ч", SITE_SPEED_THRESHOLD_KMH])
        settings.append(["Ограничение скорости вне площадки, км/ч", OUTSIDE_SPEED_LIMIT_KMH])
        settings.append(["Порог фиксации вне площадки (+10%), км/ч", OUTSIDE_SPEED_THRESHOLD_KMH])
        settings.append(["Разделение площадка/вне площадки", "по въездам и выездам через КПП 4 и КПП 5"])
        settings.append(["Максимальный разрыв одного нарушения", MAX_SPEED_EVENT_GAP_SECONDS / 86400.0])
        settings.cell(settings.max_row, 2).number_format = "[h]:mm:ss"
        settings.append(["Нарушений скорости на площадке", len(site_violations)])
        settings.append(["Нарушений скорости вне площадки", len(outside_violations)])

    workbook.save(workbook_path)
