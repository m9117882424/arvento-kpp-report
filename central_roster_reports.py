#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Use the dated roster store as the only roster source for portal reports."""
from __future__ import annotations

import base64
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Callable, Sequence

import psycopg

import consolidated_cache_portal as cache_portal
import consolidated_portal as portal
import extended_roster_fields as roster_store
import portal_runtime_patch as runtime_patch
from consolidated_cache import cache_complete, upsert_cache_from_workbook
from roster_selection import missing_roster_message

_BASE_REPORT_GENERATOR: Callable[..., dict[str, Any]] | None = None
_ORIGINAL_CONSOLIDATED_GENERATOR = cache_portal._ORIGINAL_GENERATE
_CACHED_CONSOLIDATED_GENERATOR = cache_portal.cached_generate_consolidated_web
_PATCHED = False


def _select_roster_day(database_url: str, target_day: date) -> date:
    """Select exact or latest previous roster; never use future data."""
    with psycopg.connect(database_url) as connection:
        roster_store.ensure_schema(connection)
        connection.commit()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT roster_day
                FROM consolidated_roster_snapshots
                WHERE roster_day <= %s
                ORDER BY roster_day DESC
                LIMIT 1
                """,
                (target_day,),
            )
            row = cursor.fetchone()
    if row is None:
        raise ValueError(missing_roster_message(target_day))
    return row[0]


def _load_roster_rows(
    database_url: str,
    roster_day: date,
) -> list[tuple[str, str, str, str, str, str, str]]:
    with psycopg.connect(database_url) as connection:
        roster_store.ensure_schema(connection)
        connection.commit()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT plate, company, model, grade,
                       user_name, position, directorate
                FROM consolidated_roster_entries
                WHERE roster_day=%s
                ORDER BY normalized_plate
                """,
                (roster_day,),
            )
            rows = [
                tuple(value or "" for value in row)
                for row in cursor.fetchall()
            ]
    if not rows:
        raise ValueError(f"Разнарядка за {roster_day:%d.%m.%Y} не содержит автомобилей")
    return rows


def export_roster_for_day(
    database_url: str,
    target_day: date,
    target_dir: Path,
) -> tuple[Path, date]:
    """Export the central roster applicable to ``target_day`` as XLSX."""
    selected_day = _select_roster_day(database_url, target_day)
    rows = _load_roster_rows(database_url, selected_day)
    path = target_dir / f"central_roster_{selected_day.isoformat()}.xlsx"
    path.write_bytes(roster_store._write_roster_workbook(rows))
    return path, selected_day


def generate_report_from_central_roster(
    report_type: str,
    report_day: date,
    report_end_day: date | None,
    roster_bytes: bytes | None,
    roster_filename: str,
    roster_suffix: str,
    grade_from: str,
    grade_to: str,
    time_from: str,
    time_to: str,
    consider_previous_exits: bool,
    site_speed_threshold: float,
    outside_speed_threshold: float,
) -> dict[str, Any]:
    """Run KPP and efficiency reports using a roster selected from PostgreSQL."""
    if _BASE_REPORT_GENERATOR is None:
        raise RuntimeError("Центральный источник разнарядок не инициализирован")

    if report_type not in {"kpp", "efficiency"}:
        return _BASE_REPORT_GENERATOR(
            report_type,
            report_day,
            report_end_day,
            roster_bytes,
            roster_filename,
            roster_suffix,
            grade_from,
            grade_to,
            time_from,
            time_to,
            consider_previous_exits,
            site_speed_threshold,
            outside_speed_threshold,
        )

    target_day = (
        report_end_day
        if report_type == "efficiency" and report_end_day is not None
        else report_day
    )
    database_url = portal.implementation.db_url()
    with tempfile.TemporaryDirectory(prefix="arvento_central_roster_") as temp_name:
        roster_path, selected_day = export_roster_for_day(
            database_url,
            target_day,
            Path(temp_name),
        )
        result = _BASE_REPORT_GENERATOR(
            report_type,
            report_day,
            report_end_day,
            roster_path.read_bytes(),
            roster_path.name,
            roster_path.suffix,
            grade_from,
            grade_to,
            time_from,
            time_to,
            consider_previous_exits,
            site_speed_threshold,
            outside_speed_threshold,
        )

    result.setdefault("summary", {})["Разнарядка"] = (
        f"Центральная база: {selected_day:%d.%m.%Y}"
    )
    return result


def _stored_uploads(database_url: str, target_dir: Path) -> list[tuple[str, bytes]]:
    paths = roster_store.export_stored_rosters(database_url, target_dir)
    return [(path.name, path.read_bytes()) for path in paths]


def generate_consolidated_from_central_store(
    start_day: date,
    end_day: date,
    uploads: Sequence[tuple[str, bytes]],
) -> dict[str, Any]:
    """Read cached history or calculate it from centrally stored dated rosters."""
    database_url = portal.implementation.db_url()

    if cache_complete(database_url, start_day, end_day):
        result = _CACHED_CONSOLIDATED_GENERATOR(start_day, end_day, [])
        result.setdefault("summary", {})["Разнарядки"] = "Центральная база"
        return result

    with tempfile.TemporaryDirectory(prefix="arvento_central_consolidated_") as temp_name:
        temp_dir = Path(temp_name)
        central_uploads = _stored_uploads(database_url, temp_dir)
        result = _ORIGINAL_CONSOLIDATED_GENERATOR(
            start_day,
            end_day,
            central_uploads,
        )
        try:
            workbook_path = temp_dir / result["filename"]
            workbook_path.write_bytes(base64.b64decode(result["excel_base64"]))
            cache_stats = upsert_cache_from_workbook(
                database_url,
                workbook_path,
                start_day,
                end_day,
                trigger_name="portal-central-rosters",
            )
            summary = result.setdefault("summary", {})
            summary["Источник"] = "Центральные разнарядки + расчёт GPS"
            summary["Разнарядки"] = "Центральная база"
            summary["Строк записано в историю"] = cache_stats["rows"]
        except Exception as exc:
            result.setdefault("summary", {})["История"] = f"не сохранена: {exc}"
        return result


def _patch_report_page() -> None:
    html = portal.implementation.HTML
    css = (
        "\n    /* Разнарядки управляются только на отдельной странице. */\n"
        "    #rosterBox, #consolidatedRosterBox { display:none !important; }\n"
    )
    if "#rosterBox, #consolidatedRosterBox" not in html:
        html = html.replace("</style>", css + "  </style>", 1)

    html = html.replace("  roster.required = needsRoster;", "  roster.required = false;")
    html = html.replace(
        "  consolidatedRosters.required = isConsolidated;",
        "  consolidatedRosters.required = false;",
    )

    marker = '<form id="reportForm">'
    notice = (
        '<div class="note" style="margin-top:12px;margin-bottom:4px">'
        'Все отчёты используют разнарядки из центральной базы. '
        'Загрузка и замена файлов выполняется только в разделе «Разнарядки».'
        '</div>'
    )
    if notice not in html and marker in html:
        html = html.replace(marker, notice + marker, 1)

    portal.implementation.HTML = html


def apply_central_roster_reports() -> None:
    """Install central-roster report generators and remove upload controls."""
    global _BASE_REPORT_GENERATOR, _PATCHED
    if _PATCHED:
        return

    # Keep the public runtime wrapper in place. It still applies violation-summary
    # wording, while its internal generator is replaced by the central source.
    _BASE_REPORT_GENERATOR = runtime_patch._original_generate_report
    runtime_patch._original_generate_report = generate_report_from_central_roster
    portal.generate_consolidated_web = generate_consolidated_from_central_store
    _patch_report_page()
    _PATCHED = True


__all__ = [
    "apply_central_roster_reports",
    "export_roster_for_day",
    "generate_consolidated_from_central_store",
    "generate_report_from_central_roster",
]
