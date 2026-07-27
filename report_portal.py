#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import base64
import csv
import os
import subprocess
import sys
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from openpyxl import load_workbook

APP_DIR = Path(__file__).resolve().parent
TZ = ZoneInfo("Europe/Istanbul")
MAX_ROSTER_BYTES = 25 * 1024 * 1024
PREVIEW_ROWS = 300

app = FastAPI(title="Arvento Report Portal")


HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ежедневные отчёты Arvento</title>
  <style>
    :root { color-scheme: light; font-family: Inter, Segoe UI, Arial, sans-serif; }
    body { margin: 0; background: #f3f5f8; color: #172033; }
    .wrap { max-width: 1500px; margin: 0 auto; padding: 24px; }
    .card { background: #fff; border: 1px solid #dfe4ec; border-radius: 14px; box-shadow: 0 4px 18px rgba(31,45,61,.06); padding: 20px; margin-bottom: 18px; }
    h1 { margin: 0 0 6px; font-size: 26px; }
    .muted { color: #667085; font-size: 14px; }
    .grid { display: grid; grid-template-columns: repeat(4, minmax(180px, 1fr)); gap: 14px; margin-top: 18px; }
    label { display: block; font-size: 13px; font-weight: 650; margin-bottom: 6px; }
    input, select, button { width: 100%; box-sizing: border-box; border-radius: 9px; border: 1px solid #cfd6e1; padding: 10px 11px; font: inherit; background: #fff; }
    input[type=checkbox] { width: auto; margin-right: 8px; }
    .check { display: flex; align-items: center; min-height: 42px; font-weight: 500; }
    .actions { display: flex; gap: 12px; margin-top: 18px; }
    button { cursor: pointer; border: 0; font-weight: 700; }
    .primary { background: #1663d6; color: #fff; max-width: 280px; }
    .secondary { background: #e9eef6; color: #172033; max-width: 280px; }
    button:disabled { opacity: .55; cursor: wait; }
    .status { margin-top: 14px; min-height: 22px; font-size: 14px; white-space: pre-wrap; }
    .error { color: #b42318; }
    .ok { color: #067647; }
    .stats { display: grid; grid-template-columns: repeat(4, minmax(150px, 1fr)); gap: 12px; margin-bottom: 16px; }
    .stat { border: 1px solid #e2e7ef; background: #f8fafc; border-radius: 10px; padding: 12px; }
    .stat .k { color: #667085; font-size: 12px; margin-bottom: 5px; }
    .stat .v { font-size: 20px; font-weight: 750; }
    .table-wrap { overflow: auto; max-height: 62vh; border: 1px solid #dfe4ec; border-radius: 10px; }
    table { border-collapse: separate; border-spacing: 0; width: 100%; font-size: 13px; }
    th, td { padding: 8px 10px; border-right: 1px solid #e5e9f0; border-bottom: 1px solid #e5e9f0; white-space: nowrap; text-align: left; }
    th { position: sticky; top: 0; z-index: 1; background: #eef3f9; font-weight: 750; }
    tr:last-child td { border-bottom: 0; }
    .hidden { display: none !important; }
    .note { margin-top: 10px; font-size: 13px; color: #667085; }
    @media (max-width: 900px) { .grid, .stats { grid-template-columns: 1fr 1fr; } }
    @media (max-width: 560px) { .grid, .stats { grid-template-columns: 1fr; } .actions { flex-direction: column; } .primary, .secondary { max-width: none; } }
  </style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <h1>Ежедневные отчёты Arvento</h1>
    <div class="muted">Данные берутся напрямую из PostgreSQL. CSV и Excel на сервере не сохраняются.</div>

    <form id="reportForm">
      <div class="grid">
        <div>
          <label for="reportType">Отчёт</label>
          <select id="reportType" name="report_type">
            <option value="kpp">Первый въезд через КПП</option>
            <option value="violation">Запрещённый поворот</option>
          </select>
        </div>
        <div>
          <label for="reportDate">Дата</label>
          <input id="reportDate" name="report_date" type="date" required>
        </div>
        <div id="rosterBox" style="grid-column: span 2">
          <label for="roster">Разнарядка XLSX/XLSM</label>
          <input id="roster" name="roster" type="file" accept=".xlsx,.xlsm">
        </div>

        <div class="kpp-only">
          <label for="gradeFrom">Грейд от</label>
          <input id="gradeFrom" name="grade_from" value="7" placeholder="без фильтра">
        </div>
        <div class="kpp-only">
          <label for="gradeTo">Грейд до</label>
          <input id="gradeTo" name="grade_to" value="14" placeholder="без фильтра">
        </div>
        <div class="kpp-only">
          <label for="timeFrom">Время от</label>
          <input id="timeFrom" name="time_from" type="time" value="07:00">
        </div>
        <div class="kpp-only">
          <label for="timeTo">Время до</label>
          <input id="timeTo" name="time_to" type="time" value="09:00">
        </div>
        <div class="kpp-only" style="grid-column: span 2">
          <label class="check"><input name="consider_previous_exits" type="checkbox" value="true" checked>Учитывать предыдущие выезды</label>
        </div>
      </div>

      <div class="actions">
        <button id="generateBtn" class="primary" type="submit">Сформировать отчёт</button>
        <button id="downloadBtn" class="secondary hidden" type="button">Выгрузить Excel</button>
      </div>
      <div id="status" class="status"></div>
    </form>
  </div>

  <div id="resultCard" class="card hidden">
    <div id="stats" class="stats"></div>
    <div class="table-wrap"><table id="resultTable"></table></div>
    <div id="previewNote" class="note"></div>
  </div>
</div>

<script>
const form = document.getElementById('reportForm');
const typeSelect = document.getElementById('reportType');
const dateInput = document.getElementById('reportDate');
const roster = document.getElementById('roster');
const rosterBox = document.getElementById('rosterBox');
const generateBtn = document.getElementById('generateBtn');
const downloadBtn = document.getElementById('downloadBtn');
const statusBox = document.getElementById('status');
const resultCard = document.getElementById('resultCard');
const statsBox = document.getElementById('stats');
const table = document.getElementById('resultTable');
const previewNote = document.getElementById('previewNote');
let excelBase64 = '';
let excelFilename = '';

function esc(value) {
  return String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
}

function toggleType() {
  const isKpp = typeSelect.value === 'kpp';
  rosterBox.classList.toggle('hidden', !isKpp);
  document.querySelectorAll('.kpp-only').forEach(x => x.classList.toggle('hidden', !isKpp));
  roster.required = isKpp;
}

typeSelect.addEventListener('change', toggleType);
toggleType();

async function loadDates() {
  try {
    const res = await fetch('/api/dates');
    const data = await res.json();
    if (data.dates && data.dates.length) {
      dateInput.value = data.dates[0];
      dateInput.min = data.dates[data.dates.length - 1];
      dateInput.max = data.dates[0];
    }
  } catch (_) {}
}
loadDates();

function renderStats(summary) {
  statsBox.innerHTML = Object.entries(summary).map(([k, v]) =>
    `<div class="stat"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`
  ).join('');
}

function renderTable(columns, rows) {
  const head = `<thead><tr>${columns.map(c => `<th>${esc(c)}</th>`).join('')}</tr></thead>`;
  const body = `<tbody>${rows.map(row => `<tr>${row.map(v => `<td>${esc(v)}</td>`).join('')}</tr>`).join('')}</tbody>`;
  table.innerHTML = head + body;
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  excelBase64 = '';
  excelFilename = '';
  downloadBtn.classList.add('hidden');
  resultCard.classList.add('hidden');
  statusBox.className = 'status';
  statusBox.textContent = 'Формирование отчёта. Это может занять несколько минут…';
  generateBtn.disabled = true;

  const data = new FormData(form);
  if (!data.has('consider_previous_exits')) data.append('consider_previous_exits', 'false');

  try {
    const res = await fetch('/api/generate', { method: 'POST', body: data });
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.detail || 'Ошибка формирования отчёта');

    excelBase64 = payload.excel_base64;
    excelFilename = payload.filename;
    renderStats(payload.summary);
    renderTable(payload.columns, payload.rows);
    previewNote.textContent = payload.preview_truncated
      ? `Показаны первые ${payload.rows.length} строк. Полный результат находится в Excel.`
      : `Показано строк: ${payload.rows.length}.`;
    resultCard.classList.remove('hidden');
    downloadBtn.classList.remove('hidden');
    statusBox.className = 'status ok';
    statusBox.textContent = 'Отчёт сформирован.';
  } catch (error) {
    statusBox.className = 'status error';
    statusBox.textContent = error.message;
  } finally {
    generateBtn.disabled = false;
  }
});

downloadBtn.addEventListener('click', () => {
  if (!excelBase64) return;
  const raw = atob(excelBase64);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  const blob = new Blob([bytes], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = excelFilename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
});
</script>
</body>
</html>"""


def db_url() -> str:
    value = os.environ.get("DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError("DATABASE_URL is not configured")
    return value


def parse_report_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Некорректная дата") from exc


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=TZ)
    return start, start + timedelta(days=1)


def export_day_to_csv(day: date, path: Path) -> int:
    start, end = day_bounds(day)
    query = """
        SELECT
            COALESCE(NULLIF(plate, ''), normalized_plate) AS plate,
            event_time AT TIME ZONE 'Europe/Istanbul' AS local_time,
            latitude,
            longitude,
            speed_kmh,
            distance_km,
            COALESCE(region_name, ''),
            COALESCE(address, '')
        FROM gps_points
        WHERE event_time >= %s
          AND event_time < %s
        ORDER BY normalized_plate, event_time
    """
    count = 0
    with psycopg.connect(db_url()) as connection:
        with connection.cursor(name="report_portal_export") as cursor:
            cursor.itersize = 10_000
            cursor.execute(query, (start, end))
            with path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.writer(stream, delimiter=";")
                writer.writerow([
                    "Номерной знак",
                    "Дата / время",
                    "Latitudine",
                    "Долгота",
                    "Скорость",
                    "Расстояние (км)",
                    "Область",
                    "Адрес",
                ])
                for row in cursor:
                    local_time = row[1]
                    writer.writerow([
                        row[0] or "",
                        local_time.strftime("%Y-%m-%d %H:%M:%S") if local_time else "",
                        row[2],
                        row[3],
                        row[4],
                        row[5],
                        row[6],
                        row[7],
                    ])
                    count += 1
    return count


def run_command(command: list[str]) -> str:
    completed = subprocess.run(
        command,
        cwd=APP_DIR,
        capture_output=True,
        text=True,
        timeout=15 * 60,
        check=False,
    )
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    if completed.returncode != 0:
        raise RuntimeError(output[-6000:] or f"Команда завершилась с кодом {completed.returncode}")
    return output


def json_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, time):
        return value.strftime("%H:%M:%S")
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, float):
        return round(value, 6)
    return value


def workbook_preview(path: Path) -> tuple[list[str], list[list[Any]], int]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.worksheets[0]
        iterator = sheet.iter_rows(values_only=True)
        first = next(iterator, tuple())
        columns = [str(value or "") for value in first]
        rows: list[list[Any]] = []
        total = 0
        for row in iterator:
            if not any(value not in (None, "") for value in row):
                continue
            total += 1
            if len(rows) < PREVIEW_ROWS:
                rows.append([json_cell(value) for value in row])
        return columns, rows, total
    finally:
        workbook.close()


def generate_report(
    report_type: str,
    report_day: date,
    roster_bytes: bytes | None,
    roster_suffix: str,
    grade_from: str,
    grade_to: str,
    time_from: str,
    time_to: str,
    consider_previous_exits: bool,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="arvento_report_portal_") as temp_name:
        temp_dir = Path(temp_name)
        csv_path = temp_dir / f"gps_{report_day.isoformat()}.csv"
        gps_count = export_day_to_csv(report_day, csv_path)
        if gps_count == 0:
            raise ValueError("За выбранную дату GPS-точки отсутствуют")

        if report_type == "violation":
            filename = f"Запрещенный_поворот_{report_day.isoformat()}.xlsx"
            output_path = temp_dir / filename
            log = run_command([
                sys.executable,
                str(APP_DIR / "generate_prohibited_left_turn_report.py"),
                str(csv_path),
                str(output_path),
            ])
            report_label = "Запрещённый поворот"
        elif report_type == "kpp":
            if not roster_bytes:
                raise ValueError("Для отчёта по КПП необходимо загрузить разнарядку")
            roster_path = temp_dir / f"roster{roster_suffix}"
            roster_path.write_bytes(roster_bytes)
            filename = f"Первый_въезд_{report_day.isoformat()}.xlsx"
            output_path = temp_dir / filename
            command = [
                sys.executable,
                str(APP_DIR / "generate_first_entry_report.py"),
                str(csv_path),
                str(roster_path),
                str(output_path),
                "--no-filter-dialog",
            ]
            if grade_from.strip():
                command += ["--grade-from", grade_from.strip()]
            if grade_to.strip():
                command += ["--grade-to", grade_to.strip()]
            if time_from.strip():
                command += ["--time-from", time_from.strip()]
            if time_to.strip():
                command += ["--time-to", time_to.strip()]
            command.append("--consider-previous-exits" if consider_previous_exits else "--no-consider-previous-exits")
            log = run_command(command)
            report_label = "Первый въезд через КПП"
        else:
            raise ValueError("Неизвестный тип отчёта")

        if not output_path.exists():
            raise RuntimeError("Построитель не создал Excel-файл")

        columns, rows, total_rows = workbook_preview(output_path)
        excel_bytes = output_path.read_bytes()
        return {
            "filename": filename,
            "columns": columns,
            "rows": rows,
            "preview_truncated": total_rows > len(rows),
            "excel_base64": base64.b64encode(excel_bytes).decode("ascii"),
            "summary": {
                "Отчёт": report_label,
                "Дата": report_day.strftime("%d.%m.%Y"),
                "GPS-точек": gps_count,
                "Строк результата": total_rows,
            },
            "log": log,
        }


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return HTML


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/dates")
def available_dates() -> dict[str, list[str]]:
    query = """
        SELECT DISTINCT (event_time AT TIME ZONE 'Europe/Istanbul')::date AS day
        FROM gps_points
        ORDER BY day DESC
        LIMIT 31
    """
    with psycopg.connect(db_url()) as connection:
        rows = connection.execute(query).fetchall()
    return {"dates": [row[0].isoformat() for row in rows if row[0] is not None]}


@app.post("/api/generate")
async def api_generate(
    report_type: str = Form(...),
    report_date: str = Form(...),
    roster: UploadFile | None = File(default=None),
    grade_from: str = Form(default=""),
    grade_to: str = Form(default=""),
    time_from: str = Form(default=""),
    time_to: str = Form(default=""),
    consider_previous_exits: bool = Form(default=False),
) -> dict[str, Any]:
    try:
        day = parse_report_date(report_date)
        roster_bytes: bytes | None = None
        roster_suffix = ".xlsx"
        if roster is not None and roster.filename:
            roster_suffix = Path(roster.filename).suffix.lower()
            if roster_suffix not in {".xlsx", ".xlsm"}:
                raise ValueError("Разнарядка должна быть в формате XLSX или XLSM")
            roster_bytes = await roster.read(MAX_ROSTER_BYTES + 1)
            if len(roster_bytes) > MAX_ROSTER_BYTES:
                raise ValueError("Размер разнарядки превышает 25 МБ")

        return await run_in_threadpool(
            generate_report,
            report_type,
            day,
            roster_bytes,
            roster_suffix,
            grade_from,
            grade_to,
            time_from,
            time_to,
            consider_previous_exits,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
