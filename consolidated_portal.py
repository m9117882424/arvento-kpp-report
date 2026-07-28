#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Final ASGI layer adding the consolidated report to the web portal."""
from __future__ import annotations

import base64
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool

import portal_table_ui as ui
import run_report_portal as current
from consolidated_multi_report import generate_multi_roster_report
from speed_violation_report import (
    DEFAULT_OUTSIDE_SPEED_THRESHOLD_KMH,
    DEFAULT_SITE_SPEED_THRESHOLD_KMH,
)

implementation = ui.implementation
app = ui.app

MAX_ROSTER_FILES = 31
MAX_TOTAL_ROSTER_BYTES = 100 * 1024 * 1024


def replace_once(old: str, new: str, label: str) -> None:
    if old not in implementation.HTML:
        raise RuntimeError(f"Не найден HTML-блок для изменения: {label}")
    implementation.HTML = implementation.HTML.replace(old, new, 1)


# Report selector and dedicated multi-file roster field.
replace_once(
    '<option value="efficiency">Эффективность легкового транспорта</option>',
    '<option value="efficiency">Эффективность легкового транспорта</option>\n'
    '            <option value="consolidated">Сводный отчёт</option>',
    "пункт Сводный отчёт",
)
replace_once(
    '''        <div id="rosterBox" style="grid-column: span 2">
          <label for="roster">Разнарядка XLSX/XLSM</label>
          <input id="roster" name="roster" type="file" accept=".xlsx,.xlsm">
        </div>''',
    '''        <div id="rosterBox" style="grid-column: span 2">
          <label for="roster">Разнарядка XLSX/XLSM</label>
          <input id="roster" name="roster" type="file" accept=".xlsx,.xlsm">
        </div>
        <div id="consolidatedRosterBox" class="consolidated-only hidden" style="grid-column: span 2">
          <label for="consolidatedRosters">Разнарядки XLSX/XLSM</label>
          <input id="consolidatedRosters" name="rosters" type="file"
                 accept=".xlsx,.xlsm" multiple>
          <div id="consolidatedRosterInfo" class="note">Можно выбрать несколько файлов. Дата каждой разнарядки определяется по имени файла или содержимому Excel.</div>
        </div>''',
    "множественная загрузка разнарядок",
)

# Bind the new controls.
replace_once(
    "const rosterBox = document.getElementById('rosterBox');",
    "const rosterBox = document.getElementById('rosterBox');\n"
    "const consolidatedRosterBox = document.getElementById('consolidatedRosterBox');\n"
    "const consolidatedRosters = document.getElementById('consolidatedRosters');\n"
    "const consolidatedRosterInfo = document.getElementById('consolidatedRosterInfo');",
    "JS-поля разнарядок",
)
replace_once(
    "  const isEfficiency = type === 'efficiency';\n  const isViolation = type === 'violation';",
    "  const isEfficiency = type === 'efficiency';\n"
    "  const isConsolidated = type === 'consolidated';\n"
    "  const isViolation = type === 'violation';",
    "признак сводного отчёта",
)
replace_once(
    "  roster.required = needsRoster;",
    "  roster.required = needsRoster;\n"
    "  consolidatedRosterBox.classList.toggle('hidden', !isConsolidated);\n"
    "  consolidatedRosters.required = isConsolidated;",
    "обязательность нескольких разнарядок",
)
replace_once(
    "  document.querySelectorAll('.efficiency-only').forEach(x => x.classList.toggle('hidden', !isEfficiency));",
    "  document.querySelectorAll('.efficiency-only').forEach(x => x.classList.toggle('hidden', !(isEfficiency || isConsolidated)));",
    "диапазон дат сводного отчёта",
)
replace_once(
    "  dateLabel.textContent = isEfficiency ? 'Дата от' : 'Дата';\n"
    "  endDateInput.required = isEfficiency;\n"
    "  if (isEfficiency && !endDateInput.value) endDateInput.value = dateInput.value;",
    "  dateLabel.textContent = (isEfficiency || isConsolidated) ? 'Дата от' : 'Дата';\n"
    "  endDateInput.required = isEfficiency || isConsolidated;\n"
    "  if ((isEfficiency || isConsolidated) && !endDateInput.value) endDateInput.value = dateInput.value;",
    "правила диапазона дат",
)
replace_once(
    "  if (typeSelect.value === 'efficiency' && (!endDateInput.value || endDateInput.value < dateInput.value)) endDateInput.value = dateInput.value;",
    "  if ((typeSelect.value === 'efficiency' || typeSelect.value === 'consolidated') && (!endDateInput.value || endDateInput.value < dateInput.value)) endDateInput.value = dateInput.value;",
    "синхронизация окончания периода",
)
replace_once(
    "  const targetDate = typeSelect.value === 'efficiency' && endDateInput.value ? endDateInput.value : dateInput.value;",
    "  const targetDate = (typeSelect.value === 'efficiency' || typeSelect.value === 'consolidated') && endDateInput.value ? endDateInput.value : dateInput.value;",
    "статус полноты диапазона",
)

# Let the existing plate search work with the bilingual consolidated header.
implementation.HTML = implementation.HTML.replace(
    "const plateIndex = tableColumns.indexOf('Госномер');",
    "const plateIndex = tableColumns.findIndex(value => ['Госномер', 'Госномер / Plaka', 'Номерной знак'].includes(value));",
)
implementation.HTML = implementation.HTML.replace(
    "const plateIndex = columns.indexOf('Госномер');",
    "const plateIndex = columns.findIndex(value => ['Госномер', 'Госномер / Plaka', 'Номерной знак'].includes(value));",
)

# Show selected-file count and switch the form to the v3 endpoint.
replace_once(
    "typeSelect.addEventListener('change', toggleType);",
    "typeSelect.addEventListener('change', toggleType);\n"
    "consolidatedRosters.addEventListener('change', () => {\n"
    "  const count = consolidatedRosters.files.length;\n"
    "  consolidatedRosterInfo.textContent = count\n"
    "    ? `Выбрано файлов: ${count}. Для каждой даты будет взята разнарядка с этой датой или последняя предыдущая.`\n"
    "    : 'Можно выбрать несколько файлов. Дата каждой разнарядки определяется по имени файла или содержимому Excel.';\n"
    "});",
    "счётчик файлов разнарядки",
)
replace_once(
    "fetch('/api/generate-v2', {method: 'POST', body: data})",
    "fetch('/api/generate-v3', {method: 'POST', body: data})",
    "маршрут генерации v3",
)


def period_text(start_day: date, end_day: date) -> str:
    if start_day == end_day:
        return start_day.strftime("%d.%m.%Y")
    return f"{start_day:%d.%m.%Y}–{end_day:%d.%m.%Y}"


async def read_roster_uploads(files: list[UploadFile] | None) -> list[tuple[str, bytes]]:
    uploads = [item for item in (files or []) if item.filename]
    if not uploads:
        raise ValueError("Для сводного отчёта загрузите минимум одну разнарядку")
    if len(uploads) > MAX_ROSTER_FILES:
        raise ValueError(f"Можно загрузить не более {MAX_ROSTER_FILES} разнарядок")

    result: list[tuple[str, bytes]] = []
    total = 0
    for upload in uploads:
        filename = Path(upload.filename or "roster.xlsx").name
        suffix = Path(filename).suffix.lower()
        if suffix not in {".xlsx", ".xlsm"}:
            raise ValueError(f"Файл «{filename}» должен быть в формате XLSX или XLSM")
        content = await upload.read(implementation.MAX_ROSTER_BYTES + 1)
        if len(content) > implementation.MAX_ROSTER_BYTES:
            raise ValueError(f"Размер файла «{filename}» превышает 25 МБ")
        total += len(content)
        if total > MAX_TOTAL_ROSTER_BYTES:
            raise ValueError("Общий размер загруженных разнарядок превышает 100 МБ")
        result.append((filename, content))
    return result


def generate_consolidated_web(
    start_day: date,
    end_day: date,
    uploads: list[tuple[str, bytes]],
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="arvento_consolidated_portal_") as temp_name:
        temp_dir = Path(temp_name)
        roster_paths: list[Path] = []
        for index, (filename, content) in enumerate(uploads, start=1):
            target = temp_dir / f"{index:02d}_{filename}"
            target.write_bytes(content)
            roster_paths.append(target)

        filename = f"Сводный_отчет_{start_day.isoformat()}_{end_day.isoformat()}.xlsx"
        output_path = temp_dir / filename
        stats = generate_multi_roster_report(
            start_day=start_day,
            end_day=end_day,
            roster_paths=roster_paths,
            output_path=output_path,
            database_url=implementation.db_url(),
        )
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
                "Отчёт": "Сводный отчёт",
                "Период": period_text(start_day, end_day),
                "Разнарядок": stats["rosters"],
                "Дней с данными": stats["report_days"],
                "Автомобилей-дней": stats["rows"],
                "Нет в разнарядке": stats["missing_roster_rows"],
            },
            "log": "",
        }


@app.post("/api/generate-v3")
async def api_generate_v3(
    report_type: str = Form(...),
    report_date: str = Form(...),
    report_end_date: str = Form(default=""),
    roster: UploadFile | None = File(default=None),
    rosters: list[UploadFile] | None = File(default=None),
    grade_from: str = Form(default=""),
    grade_to: str = Form(default=""),
    time_from: str = Form(default=""),
    time_to: str = Form(default=""),
    consider_previous_exits: bool = Form(default=False),
    site_speed_threshold: str = Form(default=str(DEFAULT_SITE_SPEED_THRESHOLD_KMH)),
    outside_speed_threshold: str = Form(default=str(DEFAULT_OUTSIDE_SPEED_THRESHOLD_KMH)),
) -> dict[str, Any]:
    try:
        if report_type != "consolidated":
            return await current.api_generate_v2(
                report_type=report_type,
                report_date=report_date,
                report_end_date=report_end_date,
                roster=roster,
                grade_from=grade_from,
                grade_to=grade_to,
                time_from=time_from,
                time_to=time_to,
                consider_previous_exits=consider_previous_exits,
                site_speed_threshold=site_speed_threshold,
                outside_speed_threshold=outside_speed_threshold,
            )

        start_day = implementation.parse_report_date(report_date)
        end_day = (
            implementation.parse_report_date(report_end_date)
            if report_end_date.strip()
            else start_day
        )
        if end_day < start_day:
            raise ValueError("Дата окончания раньше даты начала")
        if (end_day - start_day).days + 1 > implementation.MAX_REPORT_DAYS:
            raise ValueError(
                f"Период сводного отчёта не должен превышать {implementation.MAX_REPORT_DAYS} дней"
            )
        uploads = await read_roster_uploads(rosters)
        return await run_in_threadpool(
            generate_consolidated_web,
            start_day,
            end_day,
            uploads,
        )
    except HTTPException:
        raise
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


__all__ = ["app"]
