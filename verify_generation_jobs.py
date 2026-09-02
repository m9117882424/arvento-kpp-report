#!/usr/bin/env python3
"""Regression checks for resilient browser report generation jobs."""
from __future__ import annotations

import asyncio
import json

from fastapi import HTTPException

import generation_jobs


def payload(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def arguments() -> dict:
    return {
        "report_type": "consolidated",
        "report_date": "2026-09-01",
        "report_end_date": "2026-09-01",
        "grade_from": "",
        "grade_to": "",
        "time_from": "",
        "time_to": "",
        "consider_previous_exits": False,
        "site_speed_threshold": "50.0",
        "outside_speed_threshold": "103.0",
    }


async def check_success() -> None:
    original = generation_jobs.portal.api_generate_v3
    generation_jobs._JOBS.clear()
    release = asyncio.Event()

    async def fake_generate(**kwargs):
        assert kwargs["report_type"] == "consolidated"
        assert kwargs["rosters"] is None
        await release.wait()
        return {"filename": "ready.xlsx", "download_url": "/api/download/test"}

    generation_jobs.portal.api_generate_v3 = fake_generate
    try:
        started = payload(await generation_jobs.start_generation_job(**arguments()))
        assert started["status"] == "queued"
        job = generation_jobs._JOBS[started["job_id"]]
        assert job.task is not None
        await asyncio.sleep(0)
        running = payload(await generation_jobs.generation_job_status(job.job_id))
        assert running["status"] == "running"
        release.set()
        await job.task

        finished_response = await generation_jobs.generation_job_status(job.job_id)
        finished = payload(finished_response)
        assert finished["status"] == "succeeded"
        assert finished["result"]["download_url"] == "/api/download/test"
        assert finished_response.headers["cache-control"] == "no-store"
    finally:
        generation_jobs.portal.api_generate_v3 = original
        generation_jobs._JOBS.clear()


async def check_failure() -> None:
    original = generation_jobs.portal.api_generate_v3
    generation_jobs._JOBS.clear()

    async def fake_generate(**_kwargs):
        raise HTTPException(status_code=400, detail="Некорректный период")

    generation_jobs.portal.api_generate_v3 = fake_generate
    try:
        started = payload(await generation_jobs.start_generation_job(**arguments()))
        job = generation_jobs._JOBS[started["job_id"]]
        assert job.task is not None
        await job.task

        failed = payload(await generation_jobs.generation_job_status(job.job_id))
        assert failed["status"] == "failed"
        assert failed["http_status"] == 400
        assert failed["detail"] == "Некорректный период"
    finally:
        generation_jobs.portal.api_generate_v3 = original
        generation_jobs._JOBS.clear()


def main() -> None:
    asyncio.run(check_success())
    asyncio.run(check_failure())
    print("OK: background generation jobs survive the initial HTTP response")


if __name__ == "__main__":
    main()
