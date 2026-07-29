#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Portal integration for the persistent consolidated-report cache."""
from __future__ import annotations

import base64
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

import psycopg.rows

import consolidated_portal as portal
from consolidated_cache import (
    TZ,
    cache_complete,
    save_roster_uploads,
    upsert_cache_from_workbook,
    write_cached_workbook,
)

_ORIGINAL_GENERATE = portal.generate_consolidated_web
_ORIGINAL_READ_UPLOADS = portal.read_roster_uploads
_PATCHED = False


async def read_roster_uploads_optional(files):
    """Allow cache-only history requests without repeatedly uploading rosters."""
    uploads = [item for item in (files or []) if item.filename]
    if not uploads:
        return []
    return await _ORIGINAL_READ_UPLOADS(files)


def cached_generate_consolidated_web(
    start_day: date,
    end_day: date,
    uploads: list[tuple[str, bytes]],
) -> dict[str, Any]:
    database_url = portal.implementation.db_url()

    # New uploads are authoritative: archive them, calculate live and replace cache.
    if uploads:
        saved_rosters = save_roster_uploads(database_url, uploads)
        result = _ORIGINAL_GENERATE(start_day, end_day, uploads)
        try:
            with tempfile.TemporaryDirectory(prefix="arvento_cache_live_") as temp_name:
                workbook_path = Path(temp_name) / result["filename"]
                workbook_path.write_bytes(base64.b64decode(result["excel_base64"]))
                cache_stats = upsert_cache_from_workbook(
                    database_url,
                    workbook_path,
                    start_day,
                    end_day,
                    trigger_name="portal-live",
                )
            result.setdefault("summary", {})["Источник"] = "Расчёт GPS с сохранением в базу"
            result["summary"]["Сохранено разнарядок"] = saved_rosters
            result["summary"]["Строк записано в историю"] = cache_stats["rows"]
        except Exception as exc:
            result.setdefault("summary", {})["История"] = f"не сохранена: {exc}"
        return result

    # No uploads: serve the ready-made historical rows without GPS recalculation.
    if not cache_complete(database_url, start_day, end_day):
        raise ValueError(
            "За выбранный период нет полного набора готовых данных. "
            "Загрузите разнарядку для первичного расчёта или выполните ручную дозагрузку кэша."
        )

    with tempfile.TemporaryDirectory(prefix="arvento_cache_export_") as temp_name:
        filename = f"Сводный_отчет_история_{start_day.isoformat()}_{end_day.isoformat()}.xlsx"
        output_path = Path(temp_name) / filename
        stats = write_cached_workbook(database_url, output_path, start_day, end_day)
        columns, rows, total_rows = portal.implementation.workbook_preview(output_path)
        refreshed_at = stats.get("refreshed_at")
        refreshed_text = (
            refreshed_at.astimezone(TZ).strftime("%d.%m.%Y %H:%M:%S")
            if refreshed_at
            else "не определено"
        )
        return {
            "filename": filename,
            "columns": columns,
            "rows": rows,
            "preview_truncated": total_rows > len(rows),
            "excel_base64": base64.b64encode(output_path.read_bytes()).decode("ascii"),
            "summary": {
                "Отчёт": "Сводный отчёт — история",
                "Период": portal.period_text(start_day, end_day),
                "Источник": "База готовых данных",
                "Автомобилей-дней": stats["rows"],
                "Заправка, л": stats["fuel_liters"],
                "Обновлено": refreshed_text,
            },
            "log": "",
        }


def apply_cache_portal() -> None:
    global _PATCHED
    if _PATCHED:
        return

    portal.read_roster_uploads = read_roster_uploads_optional
    portal.generate_consolidated_web = cached_generate_consolidated_web

    portal.implementation.HTML = portal.implementation.HTML.replace(
        "consolidatedRosters.required = isConsolidated;",
        "consolidatedRosters.required = false;",
    )
    portal.implementation.HTML = portal.implementation.HTML.replace(
        "Можно выбрать несколько файлов. Дата каждой разнарядки определяется по имени файла или содержимому Excel.",
        "Для первичного расчёта загрузите разнарядки. Для готовой истории файлы можно не выбирать.",
    )
    _PATCHED = True


__all__ = ["apply_cache_portal", "cached_generate_consolidated_web"]
