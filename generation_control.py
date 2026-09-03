#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bound concurrent report generation to protect the single portal worker."""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, TypeVar

from fastapi import HTTPException

from runtime_settings import report_runtime_settings


T = TypeVar("T")
_SETTINGS = report_runtime_settings()
MAX_CONCURRENT_GENERATIONS = _SETTINGS.max_concurrent_generations
GENERATION_QUEUE_TIMEOUT_SECONDS = _SETTINGS.generation_queue_timeout_seconds
_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_GENERATIONS)


async def run_with_generation_slot(operation: Callable[[], Awaitable[T]]) -> T:
    try:
        await asyncio.wait_for(
            _SEMAPHORE.acquire(),
            timeout=GENERATION_QUEUE_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=429,
            detail="Сервер уже формирует другой отчёт. Повторите запрос через несколько секунд.",
            headers={"Retry-After": "10"},
        ) from exc
    try:
        return await operation()
    finally:
        _SEMAPHORE.release()


__all__ = [
    "GENERATION_QUEUE_TIMEOUT_SECONDS",
    "MAX_CONCURRENT_GENERATIONS",
    "run_with_generation_slot",
]
