#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Asynchronous efficiency dashboard routes for the existing report portal."""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response

from efficiency_report import (
    EfficiencySettings,
    MAX_PERIOD_DAYS,
    filter_options,
    generate_efficiency_report,
    workbook_from_report,
)


LOGGER = logging.getLogger(__name__)
_JOBS: dict[str, "EfficiencyJob"] = {}
_LIMIT_RUNNING = asyncio.Semaphore(1)
_JOB_TTL_SECONDS = 1800
_MAX_JOBS = 10
_LOCAL_TZ = ZoneInfo("Europe/Istanbul")
_WEB_PAGE = Path(__file__).resolve().parent / "web" / "efficiency_report.html"


@dataclass(slots=True)
class EfficiencyJob:
    job_id: str
    settings: EfficiencySettings
    created_at: float
    state: str = "queued"
    updated_at: float = field(default_factory=time.monotonic)
    result: dict[str, Any] | None = None
    detail: str = ""
    task: asyncio.Task[None] | None = None


def _cleanup() -> None:
    now = time.monotonic()
    for key, job in list(_JOBS.items()):
        if job.state in {"succeeded", "failed"} and now - job.updated_at > _JOB_TTL_SECONDS:
            _JOBS.pop(key, None)


def _get_job(job_id: str) -> EfficiencyJob:
    _cleanup()
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Отчёт не найден или срок хранения истёк")
    return job


async def _run(job: EfficiencyJob, database_url: Callable[[], str]) -> None:
    async with _LIMIT_RUNNING:
        job.state = "running"
        job.updated_at = time.monotonic()
        try:
            job.result = await run_in_threadpool(
                generate_efficiency_report, database_url(), job.settings
            )
        except ValueError as exc:
            job.detail = str(exc)
            job.state = "failed"
        except Exception:
            LOGGER.exception("Ошибка анализа эффективности ТС (задача %s)", job.job_id)
            job.detail = (
                "Ошибка расчёта. Проверьте полноту сводного кэша и геозон; "
                "подробности записаны в журнал портала."
            )
            job.state = "failed"
        else:
            job.state = "succeeded"
        finally:
            job.updated_at = time.monotonic()


def apply_efficiency_portal(app: FastAPI, database_url: Callable[[], str]) -> None:
    """Register a dedicated report without altering the legacy efficiency export."""
    if getattr(app.state, "efficiency_portal_registered", False):
        return
    app.state.efficiency_portal_registered = True

    @app.get("/efficiency", include_in_schema=False)
    def efficiency_page() -> FileResponse:
        return FileResponse(_WEB_PAGE, media_type="text/html")

    @app.get("/api/efficiency/options")
    def efficiency_options(
        date_from: date = Query(...),
        date_to: date = Query(...),
    ) -> dict[str, Any]:
        try:
            if (date_to - date_from).days + 1 > MAX_PERIOD_DAYS or date_to < date_from:
                raise ValueError(f"Период должен составлять от 1 до {MAX_PERIOD_DAYS} дней")
            return filter_options(database_url(), date_from, date_to)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception:
            LOGGER.exception("Не удалось получить параметры отчёта эффективности")
            raise HTTPException(
                status_code=503, detail="Недоступны данные геозон или разнарядок"
            ) from None

    @app.post("/api/efficiency/jobs", status_code=202)
    async def start_efficiency(settings: EfficiencySettings) -> dict[str, str]:
        _cleanup()
        # Identical historical requests should reuse an existing result or
        # running job. Current-day results are never reused after completion.
        historical = settings.date_to < datetime.now(_LOCAL_TZ).date()
        for existing in _JOBS.values():
            if existing.settings == settings and (
                existing.state in {"queued", "running"}
                or (historical and existing.state == "succeeded")
            ):
                return {"job_id": existing.job_id, "status": existing.state}
        active = sum(job.state in {"queued", "running"} for job in _JOBS.values())
        if active >= 2:
            raise HTTPException(
                status_code=429,
                detail="Уже выполняются расчёты. Дождитесь их окончания.",
            )
        # Bound memory without blocking new work because old completed jobs
        # have not yet reached their 30-minute retention deadline.
        while len(_JOBS) >= _MAX_JOBS:
            completed = [job for job in _JOBS.values() if job.state in {"succeeded", "failed"}]
            if not completed:
                raise HTTPException(status_code=429, detail="Очередь отчётов заполнена")
            oldest = min(completed, key=lambda job: job.updated_at)
            _JOBS.pop(oldest.job_id, None)
        job_id = secrets.token_urlsafe(24)
        job = EfficiencyJob(job_id, settings, time.monotonic())
        _JOBS[job_id] = job
        job.task = asyncio.create_task(_run(job, database_url))
        return {"job_id": job_id, "status": job.state}

    @app.get("/api/efficiency/jobs/{job_id}")
    async def efficiency_job_status(job_id: str) -> dict[str, Any]:
        job = _get_job(job_id)
        return {
            "job_id": job.job_id,
            "status": job.state,
            "detail": job.detail if job.state == "failed" else "",
        }

    @app.get("/api/efficiency/jobs/{job_id}/result")
    async def efficiency_job_result(job_id: str) -> dict[str, Any]:
        job = _get_job(job_id)
        if job.state != "succeeded" or job.result is None:
            raise HTTPException(status_code=409, detail="Отчёт ещё не готов")
        return job.result

    @app.get("/api/efficiency/jobs/{job_id}/excel")
    async def efficiency_job_excel(job_id: str) -> Response:
        job = _get_job(job_id)
        if job.state != "succeeded" or job.result is None:
            raise HTTPException(status_code=409, detail="Отчёт ещё не готов")
        body = await run_in_threadpool(workbook_from_report, job.result)
        filename = (
            f"Arvento_efficiency_{job.settings.date_from.isoformat()}"
            f"_{job.settings.date_to.isoformat()}.xlsx"
        )
        return Response(
            body,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "private, no-store",
            },
        )

    # The existing portal HTML is composed through compatibility patches.
    # Add only a navigational link; never replace the existing report route.
    try:
        import consolidated_portal as portal

        anchor = "<h1>Отчёты Arvento</h1>"
        if anchor in portal.implementation.HTML and 'href="/efficiency"' not in portal.implementation.HTML:
            portal.implementation.HTML = portal.implementation.HTML.replace(
                anchor,
                anchor +
                '\n    <a href="/efficiency" style="display:inline-block;margin:12px 0 8px;'
                'padding:10px 14px;background:#1663d6;color:white;border-radius:9px;'
                'text-decoration:none;font-size:13px;font-weight:700">'
                'Открыть рейтинг эффективности ТС →</a>',
                1,
            )
    except ImportError:
        LOGGER.warning("Основной портал недоступен: ссылка на эффективность не добавлена")


__all__ = ["apply_efficiency_portal"]
