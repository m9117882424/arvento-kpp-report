#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Canonical ASGI entrypoint for the report portal.

The compatibility implementation remains in ``report_portal.py``. This module
adds current user-facing names and validated speed-threshold controls without
changing the stable implementation entrypoints.
"""

from __future__ import annotations

import base64
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool

import report_portal as implementation
from speed_violation_report import (
    DEFAULT_OUTSIDE_SPEED_THRESHOLD_KMH,
    DEFAULT_SITE_SPEED_THRESHOLD_KMH,
    validate_speed_thresholds,
)


_original_generate_report = implementation.generate_report


# User-facing name and speed-threshold controls.
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
          <div class="note">По умолчанию: 33 км/ч на площадке и 104,5 км/ч вне площадки. Один случай считается валидным при наличии минимум двух последовательных GPS-точек.</div>
        </div>
        <div class="kpp-only"><label for="gradeFrom">Грейд от</label>''',
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

        columns, rows, total_rows = implementation.workbook_preview(output_path)
        return {
            "filename": filename,
            "columns": columns,
            "rows": rows,
            "preview_truncated": total_rows > len(rows),
            "excel_base64": base64.b64encode(output_path.read_bytes()).decode("ascii"),
            "summary": {
                "Отчёт": "Нарушения",
                "Период": report_day.strftime("%d.%m.%Y"),
                "Порог на площадке": f"{site_threshold:g} км/ч",
                "Порог вне площадки": f"{outside_threshold:g} км/ч",
                "GPS-точек": gps_count,
                "Строк первого листа": total_rows,
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
