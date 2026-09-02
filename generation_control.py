#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bound concurrent report generation to protect the single portal worker."""
from __future__ import annotations

import asyncio
import os
from typing import Awaitable, Callable, TypeVar

from fastapi import HTTPException


T = TypeVar("T")
MAX_CONCURRENT_GENERATIONS = max(
    1, int(os.environ.get("REPORT_MAX_CONCURRENT_GENERATIONS", "1"))
)
GENERATION_QUEUE_TIMEOUT_SECONDS = max(
    0.1, float(os.environ.get("REPORT_GENERATION_QUEUE_TIMEOUT_SECONDS", "5"))
)
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
