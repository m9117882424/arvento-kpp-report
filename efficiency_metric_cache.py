#!/usr/bin/env python3
"""Optional, multi-variant PostgreSQL store for completed-day efficiency GPS metrics.

Only derived daily metrics are stored, never the raw GPS track. Each distinct
source/settings version keeps its own key, so switching settings and switching
back does not cause another GPS scan. Provisional and stale days are not
eligible for persistence. Cache failures must not prevent report generation.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import date, datetime, time
from typing import Any, Mapping, Sequence

import psycopg
from psycopg.types.json import Jsonb


LOGGER = logging.getLogger(__name__)
METRIC_ALGORITHM_VERSION = "efficiency-gps-daily-v1"


def persistent_cache_enabled() -> bool:
    return os.environ.get("EFFICIENCY_PERSIST_METRIC_CACHE", "1").strip().lower() in {
        "1", "true", "yes", "on",
    }


def metric_digest(source_key: tuple[Any, ...]) -> str:
    """Stable identity for settings, geozone versions and source watermarks."""
    canonical = json.dumps(
        [METRIC_ALGORITHM_VERSION, source_key],
        default=lambda value: value.isoformat()
        if isinstance(value, (date, datetime, time)) else str(value),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class DailyMetricStore:
    """Fail-open database cache with one bounded connection per report run."""

    def __init__(self, database_url: str):
        self.database_url = database_url
        self.enabled = persistent_cache_enabled()
        self.connection: psycopg.Connection[Any] | None = None

    def __enter__(self) -> "DailyMetricStore":
        return self

    def __exit__(self, *_: object) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def _connection(self) -> psycopg.Connection[Any] | None:
        if not self.enabled:
            return None
        if self.connection is None:
            try:
                self.connection = psycopg.connect(
                    self.database_url, connect_timeout=5,
                    options="-c statement_timeout=15000",
                )
            except psycopg.Error as exc:
                self._disable(exc)
        return self.connection

    def _disable(self, exc: psycopg.Error) -> None:
        LOGGER.warning("Кэш эффективности PostgreSQL недоступен: %s", type(exc).__name__)
        self.enabled = False
        if self.connection is not None:
            try:
                self.connection.close()
            except psycopg.Error:
                pass
            self.connection = None

    def load(self, digests: Sequence[str]) -> dict[str, Mapping[str, Any]]:
        if not digests:
            return {}
        connection = self._connection()
        if connection is None:
            return {}
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT cache_key, metrics
                       FROM efficiency_gps_metric_variants
                       WHERE cache_key = ANY(%s::text[])""",
                    (list(digests),),
                )
                rows = cursor.fetchall()
            connection.commit()
        except psycopg.Error as exc:
            self._disable(exc)
            return {}
        return {
            key: value for key, value in rows
            if isinstance(key, str) and isinstance(value, dict)
        }

    def save(self, records: Sequence[tuple[str, date, str, Mapping[str, Any]]]) -> int:
        if not records:
            return 0
        connection = self._connection()
        if connection is None:
            return 0
        try:
            with connection.cursor() as cursor:
                cursor.executemany(
                    """INSERT INTO efficiency_gps_metric_variants
                           (cache_key, report_day, normalized_plate, metrics)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (cache_key) DO UPDATE SET
                           metrics = EXCLUDED.metrics, stored_at = now()""",
                    [
                        (digest, day, plate, Jsonb(dict(payload)))
                        for digest, day, plate, payload in records
                    ],
                )
            connection.commit()
        except psycopg.Error as exc:
            self._disable(exc)
            return 0
        return len(records)
