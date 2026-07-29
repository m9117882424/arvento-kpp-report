#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dedicated web page for managing dated roster snapshots."""
from __future__ import annotations

import io
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import psycopg
import psycopg.rows
from fastapi import File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, StreamingResponse
from openpyxl import Workbook

import consolidated_portal as portal
from consolidated_cache import TZ, ensure_schema, save_roster_uploads
from consolidated_cache_worker import refresh as refresh_cache

MAX_FILES = 31
MAX_TOTAL_BYTES = 100 * 1024 * 1024
_PATCHED = False

ROSTERS_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Разнарядки — Arvento</title>
  <style>
    :root { color-scheme: light; font-family: Inter, Segoe UI, Arial, sans-serif; }
    * { box-sizing: border-box; }
    body { margin: 0; background: #f3f5f8; color: #172033; }
    .wrap { max-width: 1250px; margin: 0 auto; padding: 24px; }
    .topbar { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; margin-bottom:18px; }
    h1 { margin:0 0 6px; font-size:28px; }
    .muted { color:#667085; font-size:14px; }
    .nav { display:flex; gap:10px; flex-wrap:wrap; }
    .nav a { text-decoration:none; color:#172033; background:#fff; border:1px solid #dfe4ec; border-radius:9px; padding:9px 12px; font-weight:700; }
    .card { background:#fff; border:1px solid #dfe4ec; border-radius:14px; box-shadow:0 4px 18px rgba(31,45,61,.06); padding:20px; margin-bottom:18px; }
    .grid { display:grid; grid-template-columns:2fr 1fr; gap:18px; }
    .stats { display:grid; grid-template-columns:repeat(3,minmax(150px,1fr)); gap:12px; margin-bottom:18px; }
    .stat { border:1px solid #e2e7ef; background:#f8fafc; border-radius:10px; padding:13px; }
    .stat .k { color:#667085; font-size:12px; margin-bottom:5px; }
    .stat .v { font-size:22px; font-weight:760; }
    label { display:block; font-size:13px; font-weight:700; margin-bottom:7px; }
    input, button { width:100%; border-radius:9px; border:1px solid #cfd6e1; padding:10px 11px; font:inherit; background:#fff; }
    button { cursor:pointer; border:0; font-weight:750; }
    button:disabled { opacity:.55; cursor:wait; }
    .primary { background:#1663d6; color:#fff; }
    .secondary { background:#e9eef6; color:#172033; }
    .actions { display:flex; gap:10px; margin-top:14px; }
    .status { min-height:24px; margin-top:12px; white-space:pre-wrap; font-size:14px; }
    .ok { color:#067647; }
    .error { color:#b42318; }
    .note { color:#667085; font-size:13px; line-height:1.45; margin-top:9px; }
    .schedule { border-left:4px solid #1663d6; background:#f5f8ff; border-radius:8px; padding:13px 14px; line-height:1.55; }
    .table-wrap { overflow:auto; border:1px solid #dfe4ec; border-radius:10px; }
    table { border-collapse:separate; border-spacing:0; width:100%; font-size:13px; }
    th, td { padding:10px 12px; border-right:1px solid #e5e9f0; border-bottom:1px solid #e5e9f0; white-space:nowrap; text-align:left; }
    th { position:sticky; top:0; background:#eef3f9; font-weight:750; }
    td:last-child, th:last-child { border-right:0; }
    tr:last-child td { border-bottom:0; }
    .download { display:inline-block; text-decoration:none; color:#1663d6; font-weight:700; }
    .empty { padding:28px; text-align:center; color:#667085; }
    @media (max-width:850px) { .grid,.stats { grid-template-columns:1fr; } .topbar { flex-direction:column; } .actions { flex-direction:column; } }
  </style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div>
      <h1>Разнарядки</h1>
      <div class="muted">Загрузка, замена и контроль датированных разнарядок для сводного отчёта.</div>
    </div>
    <div class="nav"><a href="/">← Отчёты</a></div>
  </div>

  <div id="stats" class="stats"></div>

  <div class="grid">
    <div class="card">
      <h2 style="margin-top:0">Загрузить разнарядки</h2>
      <form id="uploadForm">
        <label for="files">Файлы XLSX/XLSM</label>
        <input id="files" name="files" type="file" accept=".xlsx,.xlsm" multiple required>
        <div id="fileInfo" class="note">Можно выбрать несколько файлов. Дата определяется по имени файла или содержимому Excel.</div>
        <div class="actions"><button id="uploadBtn" class="primary" type="submit">Загрузить в базу</button></div>
        <div id="uploadStatus" class="status"></div>
      </form>
      <div class="note">Если в базе уже есть разнарядка с такой датой, она полностью заменяется новой версией. Старые даты не удаляются.</div>
    </div>

    <div class="card">
      <h2 style="margin-top:0">Обновление истории</h2>
      <div class="schedule">
        Автоматический перерасчёт выполняется ежедневно:<br>
        <strong>08:00 · 12:30 · 20:00</strong><br>
        Пересчитываются вчера и сегодня, поэтому поздние GPS-точки и заправки дозагружаются.
      </div>
      <div class="actions"><button id="refreshBtn" class="secondary" type="button">Пересчитать вчера и сегодня сейчас</button></div>
      <div id="refreshStatus" class="status"></div>
    </div>
  </div>

  <div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px">
      <div><h2 style="margin:0 0 4px">Сохранённые разнарядки</h2><div class="muted">От новых к старым</div></div>
      <button id="reloadBtn" class="secondary" style="width:auto" type="button">Обновить список</button>
    </div>
    <div class="table-wrap"><table id="rosterTable"></table></div>
  </div>
</div>
<script>
const table = document.getElementById('rosterTable');
const stats = document.getElementById('stats');
const files = document.getElementById('files');
const fileInfo = document.getElementById('fileInfo');
const uploadForm = document.getElementById('uploadForm');
const uploadBtn = document.getElementById('uploadBtn');
const uploadStatus = document.getElementById('uploadStatus');
const refreshBtn = document.getElementById('refreshBtn');
const refreshStatus = document.getElementById('refreshStatus');
const reloadBtn = document.getElementById('reloadBtn');

function esc(value) {
  return String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
}
function render(data) {
  const items = data.rosters || [];
  stats.innerHTML = [
    ['Сохранено дат', data.total_rosters || 0],
    ['Последняя дата', data.latest_roster_day || '—'],
    ['Всего записей ТС', data.total_entries || 0],
  ].map(([k,v]) => `<div class="stat"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`).join('');
  if (!items.length) {
    table.innerHTML = '<tbody><tr><td class="empty">Разнарядки ещё не загружены</td></tr></tbody>';
    return;
  }
  table.innerHTML = `<thead><tr><th>Дата</th><th>Файл</th><th>ТС</th><th>Загружено</th><th></th></tr></thead><tbody>${items.map(item => `
    <tr>
      <td><strong>${esc(item.roster_day)}</strong></td>
      <td>${esc(item.source_filename)}</td>
      <td>${esc(item.entry_count)}</td>
      <td>${esc(item.loaded_at)}</td>
      <td><a class="download" href="/api/rosters/${encodeURIComponent(item.roster_day_iso)}/download">Скачать</a></td>
    </tr>`).join('')}</tbody>`;
}
async function loadRosters() {
  reloadBtn.disabled = true;
  try {
    const response = await fetch('/api/rosters');
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Не удалось загрузить список');
    render(data);
  } catch (error) {
    table.innerHTML = `<tbody><tr><td class="empty">${esc(error.message)}</td></tr></tbody>`;
  } finally { reloadBtn.disabled = false; }
}
files.addEventListener('change', () => {
  const count = files.files.length;
  fileInfo.textContent = count ? `Выбрано файлов: ${count}` : 'Можно выбрать несколько файлов. Дата определяется по имени файла или содержимому Excel.';
});
uploadForm.addEventListener('submit', async event => {
  event.preventDefault();
  uploadBtn.disabled = true;
  uploadStatus.className = 'status';
  uploadStatus.textContent = 'Проверка и загрузка файлов…';
  try {
    const response = await fetch('/api/rosters/upload', {method:'POST', body:new FormData(uploadForm)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Ошибка загрузки');
    uploadStatus.className = 'status ok';
    uploadStatus.textContent = `Сохранено разнарядок: ${data.saved}. Следующий автоматический запуск применит новые данные.`;
    uploadForm.reset();
    fileInfo.textContent = 'Можно выбрать несколько файлов. Дата определяется по имени файла или содержимому Excel.';
    render(data.inventory);
  } catch (error) {
    uploadStatus.className = 'status error';
    uploadStatus.textContent = error.message;
  } finally { uploadBtn.disabled = false; }
});
refreshBtn.addEventListener('click', async () => {
  refreshBtn.disabled = true;
  refreshStatus.className = 'status';
  refreshStatus.textContent = 'Перерасчёт запущен. Это может занять несколько минут…';
  try {
    const data = new FormData(); data.append('days_back','1');
    const response = await fetch('/api/rosters/refresh', {method:'POST', body:data});
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || 'Ошибка перерасчёта');
    refreshStatus.className = 'status ok';
    refreshStatus.textContent = payload.status === 'SKIPPED'
      ? 'Другой процесс обновления уже работает.'
      : `Готово. Период: ${payload.period}; строк: ${payload.cached_rows}.`;
  } catch (error) {
    refreshStatus.className = 'status error';
    refreshStatus.textContent = error.message;
  } finally { refreshBtn.disabled = false; }
});
reloadBtn.addEventListener('click', loadRosters);
loadRosters();
</script>
</body>
</html>"""


def roster_inventory() -> dict:
    url = portal.implementation.db_url()
    with psycopg.connect(url, row_factory=psycopg.rows.dict_row) as connection:
        ensure_schema(connection)
        connection.commit()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT roster_day, source_filename, entry_count, loaded_at
                FROM consolidated_roster_snapshots
                ORDER BY roster_day DESC
                """
            )
            rows = list(cursor.fetchall())

    rosters = []
    total_entries = 0
    for item in rows:
        loaded_at = item["loaded_at"]
        if loaded_at is not None:
            loaded_at = loaded_at.astimezone(TZ).strftime("%d.%m.%Y %H:%M:%S")
        total_entries += int(item["entry_count"] or 0)
        rosters.append({
            "roster_day": item["roster_day"].strftime("%d.%m.%Y"),
            "roster_day_iso": item["roster_day"].isoformat(),
            "source_filename": item["source_filename"],
            "entry_count": int(item["entry_count"] or 0),
            "loaded_at": loaded_at or "",
        })
    return {
        "rosters": rosters,
        "total_rosters": len(rosters),
        "latest_roster_day": rosters[0]["roster_day"] if rosters else "",
        "total_entries": total_entries,
    }


async def read_uploads(files: list[UploadFile] | None) -> list[tuple[str, bytes]]:
    uploads = [item for item in (files or []) if item.filename]
    if not uploads:
        raise ValueError("Выберите минимум один файл XLSX/XLSM")
    if len(uploads) > MAX_FILES:
        raise ValueError(f"За один раз можно загрузить не более {MAX_FILES} файлов")

    result: list[tuple[str, bytes]] = []
    total = 0
    for upload in uploads:
        filename = Path(upload.filename or "roster.xlsx").name
        if Path(filename).suffix.lower() not in {".xlsx", ".xlsm"}:
            raise ValueError(f"Файл «{filename}» должен быть XLSX или XLSM")
        content = await upload.read(portal.implementation.MAX_ROSTER_BYTES + 1)
        if len(content) > portal.implementation.MAX_ROSTER_BYTES:
            raise ValueError(f"Размер файла «{filename}» превышает 25 МБ")
        total += len(content)
        if total > MAX_TOTAL_BYTES:
            raise ValueError("Общий размер файлов превышает 100 МБ")
        result.append((filename, content))
    return result


def build_roster_download(roster_day: date) -> tuple[bytes, str]:
    url = portal.implementation.db_url()
    with psycopg.connect(url, row_factory=psycopg.rows.dict_row) as connection:
        ensure_schema(connection)
        connection.commit()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT source_filename
                FROM consolidated_roster_snapshots
                WHERE roster_day=%s
                """,
                (roster_day,),
            )
            snapshot = cursor.fetchone()
            if snapshot is None:
                raise ValueError("Разнарядка за выбранную дату не найдена")
            cursor.execute(
                """
                SELECT plate, company, user_name, grade
                FROM consolidated_roster_entries
                WHERE roster_day=%s
                ORDER BY normalized_plate
                """,
                (roster_day,),
            )
            entries = list(cursor.fetchall())

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Разнарядка"
    sheet.append(["Гос рег знак", "Компания или фирма", "ПОЛЬЗОВАТЕЛЬ", "Грейд"])
    for item in entries:
        sheet.append([item["plate"], item["company"], item["user_name"], item["grade"]])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for column, width in {"A": 18, "B": 28, "C": 38, "D": 14}.items():
        sheet.column_dimensions[column].width = width
    stream = io.BytesIO()
    workbook.save(stream)
    workbook.close()
    filename = f"Разнарядка_{roster_day.isoformat()}.xlsx"
    return stream.getvalue(), filename


def apply_roster_management_portal() -> None:
    global _PATCHED
    if _PATCHED:
        return

    app = portal.app

    @app.get("/rosters", response_class=HTMLResponse)
    def roster_page() -> HTMLResponse:
        return HTMLResponse(ROSTERS_HTML)

    @app.get("/api/rosters")
    def api_rosters() -> dict:
        try:
            return roster_inventory()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/rosters/upload")
    async def api_rosters_upload(files: list[UploadFile] | None = File(default=None)) -> dict:
        try:
            uploads = await read_uploads(files)
            saved = await run_in_threadpool(
                save_roster_uploads,
                portal.implementation.db_url(),
                uploads,
            )
            inventory = await run_in_threadpool(roster_inventory)
            return {"saved": saved, "inventory": inventory}
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/rosters/refresh")
    async def api_rosters_refresh(days_back: int = Form(default=1)) -> dict:
        try:
            if days_back < 0 or days_back > 30:
                raise ValueError("days_back должен быть от 0 до 30")
            today = datetime.now(TZ).date()
            return await run_in_threadpool(
                refresh_cache,
                today - timedelta(days=days_back),
                today,
                "rosters-page",
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/rosters/{roster_day}/download")
    def api_roster_download(roster_day: date) -> StreamingResponse:
        try:
            content, filename = build_roster_download(roster_day)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        encoded = quote(filename)
        return StreamingResponse(
            io.BytesIO(content),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"},
        )

    portal.implementation.HTML = portal.implementation.HTML.replace(
        '<div class="muted">Данные берутся напрямую из PostgreSQL. Временные CSV и Excel после формирования удаляются.</div>',
        '<div class="muted">Данные берутся напрямую из PostgreSQL. Временные CSV и Excel после формирования удаляются.</div>'
        '<div style="margin-top:12px"><a href="/rosters" style="display:inline-block;text-decoration:none;color:#1663d6;font-weight:750">Разнарядки →</a></div>',
        1,
    )
    _PATCHED = True


__all__ = ["apply_roster_management_portal", "roster_inventory"]
