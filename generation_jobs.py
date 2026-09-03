#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Resilient background jobs for long-running consolidated reports."""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import JSONResponse

import consolidated_portal as portal
from business_rules import (
    DEFAULT_OUTSIDE_SPEED_THRESHOLD_KMH,
    DEFAULT_SITE_SPEED_THRESHOLD_KMH,
)
from runtime_settings import report_runtime_settings


LOGGER = logging.getLogger(__name__)
_SETTINGS = report_runtime_settings()
JOB_TTL_SECONDS = _SETTINGS.generation_job_ttl_seconds
JOB_MAX_ENTRIES = _SETTINGS.generation_job_max_entries


@dataclass(slots=True)
class GenerationJob:
    job_id: str
    created_at: float
    status: str = "queued"
    updated_at: float = field(default_factory=time.time)
    result: dict[str, Any] | None = None
    detail: str = ""
    http_status: int = 500
    task: asyncio.Task[None] | None = None


_JOBS: dict[str, GenerationJob] = {}


def _cleanup_jobs(now: float) -> None:
    expired = [
        job_id
        for job_id, job in _JOBS.items()
        if job.status in {"succeeded", "failed"}
        and job.updated_at + JOB_TTL_SECONDS <= now
    ]
    for job_id in expired:
        _JOBS.pop(job_id, None)

    overflow = max(0, len(_JOBS) - JOB_MAX_ENTRIES + 1)
    if not overflow:
        return
    completed = sorted(
        (
            job
            for job in _JOBS.values()
            if job.status in {"succeeded", "failed"}
        ),
        key=lambda item: item.updated_at,
    )
    for job in completed[:overflow]:
        _JOBS.pop(job.job_id, None)


async def _execute_job(job: GenerationJob, arguments: dict[str, Any]) -> None:
    job.status = "running"
    job.updated_at = time.time()
    try:
        job.result = await portal.api_generate_v3(**arguments)
    except HTTPException as exc:
        job.status = "failed"
        job.detail = str(exc.detail)
        job.http_status = int(exc.status_code)
    except Exception:
        LOGGER.exception("Не удалось выполнить фоновую задачу %s", job.job_id)
        job.status = "failed"
        job.detail = (
            "Внутренняя ошибка формирования отчёта. "
            "Подробности сохранены в журнале."
        )
        job.http_status = 500
    else:
        job.status = "succeeded"
    finally:
        job.updated_at = time.time()


async def start_generation_job(
    report_type: str = Form(...),
    report_date: str = Form(...),
    report_end_date: str = Form(default=""),
    grade_from: str = Form(default=""),
    grade_to: str = Form(default=""),
    time_from: str = Form(default=""),
    time_to: str = Form(default=""),
    consider_previous_exits: bool = Form(default=False),
    site_speed_threshold: str = Form(
        default=str(DEFAULT_SITE_SPEED_THRESHOLD_KMH)
    ),
    outside_speed_threshold: str = Form(
        default=str(DEFAULT_OUTSIDE_SPEED_THRESHOLD_KMH)
    ),
) -> JSONResponse:
    """Start a consolidated report without holding the browser connection."""
    if report_type != "consolidated":
        raise HTTPException(
            status_code=400,
            detail="Фоновый режим поддерживается только для сводного отчёта",
        )

    now = time.time()
    _cleanup_jobs(now)
    if len(_JOBS) >= JOB_MAX_ENTRIES:
        raise HTTPException(
            status_code=503,
            detail="Очередь формирования отчётов заполнена. Повторите запрос позже.",
        )

    job_id = secrets.token_urlsafe(24)
    job = GenerationJob(job_id=job_id, created_at=now, updated_at=now)
    _JOBS[job_id] = job
    arguments = {
        "report_type": report_type,
        "report_date": report_date,
        "report_end_date": report_end_date,
        "roster": None,
        "rosters": None,
        "grade_from": grade_from,
        "grade_to": grade_to,
        "time_from": time_from,
        "time_to": time_to,
        "consider_previous_exits": consider_previous_exits,
        "site_speed_threshold": site_speed_threshold,
        "outside_speed_threshold": outside_speed_threshold,
    }
    job.task = asyncio.create_task(_execute_job(job, arguments))
    return JSONResponse(
        status_code=202,
        content={"job_id": job_id, "status": job.status},
        headers={"Cache-Control": "no-store"},
    )


async def generation_job_status(job_id: str) -> JSONResponse:
    """Return current state and the completed report payload when available."""
    _cleanup_jobs(time.time())
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail="Задача не найдена или срок её хранения истёк",
        )

    payload: dict[str, Any] = {
        "job_id": job.job_id,
        "status": job.status,
    }
    if job.status == "succeeded":
        payload["result"] = job.result or {}
    elif job.status == "failed":
        payload["detail"] = job.detail
        payload["http_status"] = job.http_status
    return JSONResponse(
        content=payload,
        headers={"Cache-Control": "no-store"},
    )


def _patch_browser_workflow() -> None:
    html = portal.implementation.HTML
    if "waitForGenerationJob" in html:
        return

    helper_anchor = "form.addEventListener('submit', async event => {"
    helper = """const generationJobStorageKey = 'arvento-active-generation-job';
async function waitForGenerationJob(jobId) {
  const startedAt = Date.now();
  while (true) {
    await new Promise(resolve => setTimeout(resolve, 2000));
    let response;
    try {
      response = await fetch(`/api/generation-jobs/${encodeURIComponent(jobId)}`, {cache: 'no-store'});
    } catch (_) {
      statusBox.className = 'status';
      statusBox.textContent = 'Связь временно потеряна. Восстанавливаем статус отчёта…';
      continue;
    }
    const state = await readResponsePayload(response);
    if (!response.ok) throw new Error(state.detail || 'Не удалось получить статус отчёта');
    if (state.status === 'succeeded') return state.result || {};
    if (state.status === 'failed') throw new Error(state.detail || 'Ошибка формирования отчёта');
    const elapsed = Math.max(1, Math.round((Date.now() - startedAt) / 1000));
    statusBox.className = 'status';
    statusBox.textContent = `Формирование отчёта: ${elapsed} сек. Страница получает статус автоматически…`;
  }
}
function showGeneratedReport(payload) {
  excelBase64 = payload.excel_base64 || '';
  excelFilename = payload.filename;
  downloadUrl = payload.download_url || '';
  renderStats(payload.summary);
  renderTable(payload.columns, payload.rows);
  previewNote.textContent = payload.preview_truncated ? `Показаны первые ${payload.rows.length} строк. Полный результат находится в Excel.` : `Показано строк: ${payload.rows.length}.`;
  resultCard.classList.remove('hidden');
  downloadBtn.classList.remove('hidden');
  statusBox.className = 'status ok';
  statusBox.textContent = 'Отчёт сформирован.';
}
async function resumeGenerationJob() {
  const jobId = localStorage.getItem(generationJobStorageKey);
  if (!jobId) return;
  statusBox.className = 'status';
  statusBox.textContent = 'Восстанавливаем формирование ранее запущенного отчёта…';
  generateBtn.disabled = true;
  try {
    const payload = await waitForGenerationJob(jobId);
    showGeneratedReport(payload);
  } catch (error) {
    statusBox.className = 'status error';
    statusBox.textContent = error.message;
  } finally {
    localStorage.removeItem(generationJobStorageKey);
    generateBtn.disabled = false;
  }
}
"""
    if helper_anchor not in html:
        raise RuntimeError("Не найден обработчик запуска отчёта для фонового режима")
    html = html.replace(helper_anchor, helper + helper_anchor, 1)

    request_block = """    const response = await fetch('/api/generate-v3', {method: 'POST', body: data});
    const payload = await readResponsePayload(response);
    if (!response.ok) throw new Error(payload.detail || 'Ошибка формирования отчёта');"""
    replacement = """    let payload;
    if (typeSelect.value === 'consolidated') {
      const startResponse = await fetch('/api/generation-jobs', {method: 'POST', body: data});
      const startPayload = await readResponsePayload(startResponse);
      if (!startResponse.ok) throw new Error(startPayload.detail || 'Не удалось запустить формирование отчёта');
      localStorage.setItem(generationJobStorageKey, startPayload.job_id);
      try {
        payload = await waitForGenerationJob(startPayload.job_id);
      } finally {
        localStorage.removeItem(generationJobStorageKey);
      }
    } else {
      const response = await fetch('/api/generate-v3', {method: 'POST', body: data});
      payload = await readResponsePayload(response);
      if (!response.ok) throw new Error(payload.detail || 'Ошибка формирования отчёта');
    }"""
    if request_block not in html:
        raise RuntimeError("Не найден запрос generate-v3 для фонового режима")
    html = html.replace(request_block, replacement, 1)

    result_block = """    excelBase64 = payload.excel_base64 || '';
    excelFilename = payload.filename;
    downloadUrl = payload.download_url || '';
    renderStats(payload.summary);
    renderTable(payload.columns, payload.rows);
    previewNote.textContent = payload.preview_truncated ? `Показаны первые ${payload.rows.length} строк. Полный результат находится в Excel.` : `Показано строк: ${payload.rows.length}.`;
    resultCard.classList.remove('hidden');
    downloadBtn.classList.remove('hidden');
    statusBox.className = 'status ok';
    statusBox.textContent = 'Отчёт сформирован.';"""
    if result_block not in html:
        raise RuntimeError("Не найден вывод результата для фонового режима")
    html = html.replace(result_block, "    showGeneratedReport(payload);", 1)

    resume_anchor = """});
downloadBtn.addEventListener('click', () => {"""
    if resume_anchor not in html:
        raise RuntimeError("Не найден обработчик загрузки для восстановления задачи")
    portal.implementation.HTML = html.replace(
        resume_anchor,
        "});\nresumeGenerationJob();\ndownloadBtn.addEventListener('click', () => {",
        1,
    )


def apply_generation_jobs(app: FastAPI) -> None:
    if getattr(app.state, "generation_jobs_applied", False):
        return
    app.add_api_route(
        "/api/generation-jobs",
        start_generation_job,
        methods=["POST"],
        status_code=202,
        include_in_schema=False,
    )
    app.add_api_route(
        "/api/generation-jobs/{job_id}",
        generation_job_status,
        methods=["GET"],
        include_in_schema=False,
    )
    _patch_browser_workflow()
    app.state.generation_jobs_applied = True


__all__ = [
    "GenerationJob",
    "apply_generation_jobs",
    "generation_job_status",
    "start_generation_job",
]
