#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Canonical ASGI entrypoint for the report portal.

The compatibility implementation remains in ``report_portal.py``. This module
adds current user-facing names, validated speed controls, grouped violation
preview and a client-side plate filter.
"""

from __future__ import annotations

import base64
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from openpyxl import load_workbook

import report_portal as implementation
from speed_violation_report import (
    DEFAULT_OUTSIDE_SPEED_THRESHOLD_KMH,
    DEFAULT_SITE_SPEED_THRESHOLD_KMH,
    OUTSIDE_SHEET_NAME,
    SITE_SHEET_NAME,
    TURN_SHEET_NAME,
    validate_speed_thresholds,
)


_original_generate_report = implementation.generate_report


# User-facing report name and configurable speed thresholds.
implementation.HTML = implementation.HTML.replace(
    '<option value="violation">Запрещённый поворот</option>',
    '<option value="violation">Нарушения</option>',
)
implementation.HTML = implementation.HTML.replace(
    '        <div class="kpp-only"><label for="gradeFrom">Грейд от</label>',
    f'''        <div class="violation-only hidden">
          <label for="siteSpeedThreshold">Порог на площадке, км/ч</label>
          <input id="siteSpeedThreshold" name="site_speed_threshold" type="number"
                 min="5" max="200" step="0.1" value="{DEFAULT_SITE_SPEED_THRESHOLD_KMH:g}">
        </div>
        <div class="violation-only hidden">
          <label for="outsideSpeedThreshold">Порог вне площадки, км/ч</label>
          <input id="outsideSpeedThreshold" name="outside_speed_threshold" type="number"
                 min="20" max="250" step="0.1" value="{DEFAULT_OUTSIDE_SPEED_THRESHOLD_KMH:g}">
        </div>
        <div class="violation-only hidden" style="grid-column: span 2">
          <div class="note">Нарушение засчитывается только по плавной последовательности минимум из трёх GPS-точек продолжительностью не менее 10 секунд. Одиночные скачки скорости исключаются.</div>
        </div>
        <div class="kpp-only"><label for="gradeFrom">Грейд от</label>''',
)
implementation.HTML = implementation.HTML.replace(
    '    <div class="table-wrap"><table id="resultTable"></table></div>',
    '''    <div id="plateFilterBox" class="hidden" style="display:flex; gap:12px; align-items:end; margin-bottom:14px; max-width:520px">
      <div style="flex:1">
        <label for="plateFilter">Фильтр по госномеру</label>
        <select id="plateFilter"><option value="">Все госномера</option></select>
      </div>
      <div id="plateFilterCount" class="muted" style="padding-bottom:10px"></div>
    </div>
    <div class="table-wrap"><table id="resultTable"></table></div>''',
)
implementation.HTML = implementation.HTML.replace(
    "    tr:last-child td { border-bottom: 0; }",
    "    tr:last-child td { border-bottom: 0; }\n    tr.plate-start td { border-top: 2px solid #98a2b3; }",
)
implementation.HTML = implementation.HTML.replace(
    "  const isEfficiency = type === 'efficiency';\n  const needsRoster = isKpp || isEfficiency;",
    "  const isEfficiency = type === 'efficiency';\n  const isViolation = type === 'violation';\n  const needsRoster = isKpp || isEfficiency;",
)
implementation.HTML = implementation.HTML.replace(
    "  document.querySelectorAll('.efficiency-only').forEach(x => x.classList.toggle('hidden', !isEfficiency));",
    "  document.querySelectorAll('.efficiency-only').forEach(x => x.classList.toggle('hidden', !isEfficiency));\n"
    "  document.querySelectorAll('.violation-only').forEach(x => x.classList.toggle('hidden', !isViolation));\n"
    "  document.getElementById('siteSpeedThreshold').required = isViolation;\n"
    "  document.getElementById('outsideSpeedThreshold').required = isViolation;",
)
implementation.HTML = implementation.HTML.replace(
    "const previewNote = document.getElementById('previewNote');\nlet excelBase64 = '';",
    "const previewNote = document.getElementById('previewNote');\n"
    "const plateFilterBox = document.getElementById('plateFilterBox');\n"
    "const plateFilter = document.getElementById('plateFilter');\n"
    "const plateFilterCount = document.getElementById('plateFilterCount');\n"
    "let tableColumns = [];\nlet tableRows = [];\nlet excelBase64 = '';",
)
implementation.HTML = implementation.HTML.replace(
    "function renderTable(columns, rows) {\n"
    "  const head = `<thead><tr>${columns.map(value => `<th>${esc(value)}</th>`).join('')}</tr></thead>`;\n"
    "  const body = `<tbody>${rows.map(row => `<tr>${row.map(value => `<td>${esc(value)}</td>`).join('')}</tr>`).join('')}</tbody>`;\n"
    "  table.innerHTML = head + body;\n"
    "}",
    "function drawFilteredTable() {\n"
    "  const plateIndex = tableColumns.indexOf('Госномер');\n"
    "  const selected = plateFilter.value;\n"
    "  const rows = selected && plateIndex >= 0 ? tableRows.filter(row => String(row[plateIndex] ?? '') === selected) : tableRows;\n"
    "  const head = `<thead><tr>${tableColumns.map(value => `<th>${esc(value)}</th>`).join('')}</tr></thead>`;\n"
    "  let previousPlate = null;\n"
    "  const bodyRows = rows.map(row => {\n"
    "    const plate = plateIndex >= 0 ? String(row[plateIndex] ?? '') : '';\n"
    "    const className = plate && plate !== previousPlate ? ' class=\"plate-start\"' : '';\n"
    "    previousPlate = plate;\n"
    "    return `<tr${className}>${row.map(value => `<td>${esc(value)}</td>`).join('')}</tr>`;\n"
    "  }).join('');\n"
    "  table.innerHTML = head + `<tbody>${bodyRows}</tbody>`;\n"
    "  plateFilterCount.textContent = `Показано: ${rows.length} из ${tableRows.length}`;\n"
    "}\n"
    "function renderTable(columns, rows) {\n"
    "  tableColumns = columns;\n"
    "  tableRows = rows;\n"
    "  const plateIndex = columns.indexOf('Госномер');\n"
    "  if (plateIndex >= 0) {\n"
    "    const plates = [...new Set(rows.map(row => String(row[plateIndex] ?? '')).filter(Boolean))].sort();\n"
    "    plateFilter.innerHTML = '<option value=\"\">Все госномера</option>' + plates.map(value => `<option value=\"${esc(value)}\">${esc(value)}</option>`).join('');\n"
    "    plateFilterBox.classList.remove('hidden');\n"
    "  } else {\n"
    "    plateFilterBox.classList.add('hidden');\n"
    "  }\n"
    "  drawFilteredTable();\n"
    "}\n"
    "plateFilter.addEventListener('change', drawFilteredTable);",
)
implementation.HTML = implementation.HTML.replace(
    "  const data = new FormData(form);",
    "  if (typeSelect.value === 'violation') {\n"
    "    const site = Number(document.getElementById('siteSpeedThreshold').value);\n"
    "    const outside = Number(document.getElementById('outsideSpeedThreshold').value);\n"
    "    if (!Number.isFinite(site) || site < 5 || site > 200) {\n"
    "      statusBox.className = 'status error';\n"
    "      statusBox.textContent = 'Порог на площадке должен быть от 5 до 200 км/ч.';\n"
    "      generateBtn.disabled = false;\n"
    "      return;\n"
    "    }\n"
    "    if (!Number.isFinite(outside) || outside < 20 || outside > 250) {\n"
    "      statusBox.className = 'status error';\n"
    "      statusBox.textContent = 'Порог вне площадки должен быть от 20 до 250 км/ч.';\n"
    "      generateBtn.disabled = false;\n"
    "      return;\n"
    "    }\n"
    "    if (outside < site) {\n"
    "      statusBox.className = 'status error';\n"
    "      statusBox.textContent = 'Порог вне площадки не может быть ниже порога на площадке.';\n"
    "      generateBtn.disabled = false;\n"
    "      return;\n"
    "    }\n"
    "  }\n"
    "  const data = new FormData(form);",
)
implementation.HTML = implementation.HTML.replace(
    "fetch('/api/generate', {method: 'POST', body: data})",
    "fetch('/api/generate-v2', {method: 'POST', body: data})",
)


def parse_threshold(value: str, label: str) -> float:
    text = value.strip().replace(",", ".")
    if not text:
        raise ValueError(f"Не заполнено поле «{label}»")
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"Поле «{label}» должно содержать число") from exc


def _header_map(sheet) -> dict[str, int]:
    first = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), tuple())
    return {str(value or "").strip(): index for index, value in enumerate(first)}


def _value(row: tuple[Any, ...], headers: dict[str, int], name: str) -> Any:
    index = headers.get(name)
    if index is None or index >= len(row):
        return None
    return row[index]


def _as_number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def violation_web_preview(path: Path) -> tuple[list[str], list[list[Any]], int]:
    """Build one detailed, plate-grouped table from all violation sheets."""
    columns = [
        "Госномер",
        "Тип нарушения",
        "Дата",
        "Начало",
        "Окончание",
        "Максимальная скорость, км/ч",
        "Порог, км/ч",
        "Адрес",
    ]
    records: list[tuple[str, datetime | None, list[Any]]] = []
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        for sheet_name in (TURN_SHEET_NAME, SITE_SHEET_NAME, OUTSIDE_SHEET_NAME):
            if sheet_name not in workbook.sheetnames:
                continue
            sheet = workbook[sheet_name]
            headers = _header_map(sheet)
            for row in sheet.iter_rows(min_row=2, values_only=True):
                plate = str(_value(row, headers, "Госномер") or "").strip()
                if not plate:
                    continue

                if sheet_name == TURN_SHEET_NAME:
                    start = _value(row, headers, "Начало прохода")
                    finish = _value(row, headers, "Окончание прохода")
                    start_speed = _as_number(_value(row, headers, "Скорость в начале"))
                    finish_speed = _as_number(_value(row, headers, "Скорость в конце"))
                    speeds = [value for value in (start_speed, finish_speed) if value is not None]
                    max_speed = max(speeds) if speeds else None
                    address_parts = [
                        str(_value(row, headers, "Адрес начала") or "").strip(),
                        str(_value(row, headers, "Адрес окончания") or "").strip(),
                    ]
                    address = " → ".join(part for part in address_parts if part)
                    threshold = None
                else:
                    start = _value(row, headers, "Начало нарушения")
                    finish = _value(row, headers, "Окончание нарушения")
                    max_speed = _as_number(_value(row, headers, "Максимальная скорость, км/ч"))
                    threshold = _as_number(_value(row, headers, "Порог фиксации, км/ч"))
                    address = str(_value(row, headers, "Адрес максимума") or "").strip()

                event_date = start.date() if isinstance(start, datetime) else _value(row, headers, "Дата")
                display = [
                    plate,
                    sheet_name,
                    implementation.json_cell(event_date),
                    implementation.json_cell(start),
                    implementation.json_cell(finish),
                    implementation.json_cell(max_speed),
                    implementation.json_cell(threshold),
                    address,
                ]
                records.append((plate, start if isinstance(start, datetime) else None, display))
    finally:
        workbook.close()

    records.sort(key=lambda item: (item[0], item[1] or datetime.min, item[2][1]))
    rows = [item[2] for item in records]
    return columns, rows, len(rows)


def generate_report_with_thresholds(
    report_type: str,
    report_day: date,
    report_end_day: date | None,
    roster_bytes: bytes | None,
    roster_filename: str,
    roster_suffix: str,
    grade_from: str,
    grade_to: str,
    time_from: str,
    time_to: str,
    consider_previous_exits: bool,
    site_speed_threshold: float,
    outside_speed_threshold: float,
) -> dict[str, Any]:
    if report_type != "violation":
        return _original_generate_report(
            report_type,
            report_day,
            report_end_day,
            roster_bytes,
            roster_filename,
            roster_suffix,
            grade_from,
            grade_to,
            time_from,
            time_to,
            consider_previous_exits,
        )

    implementation.validate_canonical_scripts()
    site_threshold, outside_threshold = validate_speed_thresholds(
        site_speed_threshold,
        outside_speed_threshold,
    )

    with tempfile.TemporaryDirectory(prefix="arvento_report_portal_") as temp_name:
        temp_dir = Path(temp_name)
        csv_path = temp_dir / f"gps_{report_day.isoformat()}.csv"
        gps_count = implementation.export_period_to_csv(report_day, report_day, csv_path)
        if gps_count == 0:
            raise ValueError("За выбранную дату GPS-точки отсутствуют")

        filename = f"Нарушения_{report_day.isoformat()}.xlsx"
        output_path = temp_dir / filename
        log = implementation.run_command([
            implementation.sys.executable,
            str(implementation.CANONICAL_REPORT_SCRIPTS[report_type]),
            str(csv_path),
            str(output_path),
            "--site-speed-threshold",
            f"{site_threshold:g}",
            "--outside-speed-threshold",
            f"{outside_threshold:g}",
        ])
        if not output_path.exists():
            raise RuntimeError("Построитель не создал Excel-файл")

        columns, rows, total_rows = violation_web_preview(output_path)
        plate_count = len({str(row[0]) for row in rows if row and row[0]})
        return {
            "filename": filename,
            "columns": columns,
            "rows": rows,
            "preview_truncated": False,
            "excel_base64": base64.b64encode(output_path.read_bytes()).decode("ascii"),
            "summary": {
                "Отчёт": "Нарушения",
                "Период": report_day.strftime("%d.%m.%Y"),
                "Порог на площадке": f"{site_threshold:g} км/ч",
                "Порог вне площадки": f"{outside_threshold:g} км/ч",
                "Госномеров": plate_count,
                "Нарушений": total_rows,
                "GPS-точек": gps_count,
            },
            "log": log,
        }


app = implementation.app


@app.post("/api/generate-v2")
async def api_generate_v2(
    report_type: str = Form(...),
    report_date: str = Form(...),
    report_end_date: str = Form(default=""),
    roster: UploadFile | None = File(default=None),
    grade_from: str = Form(default=""),
    grade_to: str = Form(default=""),
    time_from: str = Form(default=""),
    time_to: str = Form(default=""),
    consider_previous_exits: bool = Form(default=False),
    site_speed_threshold: str = Form(default=str(DEFAULT_SITE_SPEED_THRESHOLD_KMH)),
    outside_speed_threshold: str = Form(default=str(DEFAULT_OUTSIDE_SPEED_THRESHOLD_KMH)),
) -> dict[str, Any]:
    try:
        day = implementation.parse_report_date(report_date)
        end_day = (
            implementation.parse_report_date(report_end_date)
            if report_end_date.strip()
            else None
        )

        site_threshold = parse_threshold(site_speed_threshold, "Порог на площадке")
        outside_threshold = parse_threshold(outside_speed_threshold, "Порог вне площадки")
        if report_type == "violation":
            site_threshold, outside_threshold = validate_speed_thresholds(
                site_threshold,
                outside_threshold,
            )

        roster_bytes: bytes | None = None
        roster_filename = "roster.xlsx"
        roster_suffix = ".xlsx"
        if roster is not None and roster.filename:
            roster_filename = Path(roster.filename).name
            roster_suffix = Path(roster_filename).suffix.lower()
            if roster_suffix not in {".xlsx", ".xlsm"}:
                raise ValueError("Разнарядка должна быть в формате XLSX или XLSM")
            roster_bytes = await roster.read(implementation.MAX_ROSTER_BYTES + 1)
            if len(roster_bytes) > implementation.MAX_ROSTER_BYTES:
                raise ValueError("Размер разнарядки превышает 25 МБ")

        return await run_in_threadpool(
            generate_report_with_thresholds,
            report_type,
            day,
            end_day,
            roster_bytes,
            roster_filename,
            roster_suffix,
            grade_from,
            grade_to,
            time_from,
            time_to,
            consider_previous_exits,
            site_threshold,
            outside_threshold,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


__all__ = ["app"]
