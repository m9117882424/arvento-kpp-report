#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Store and restore all roster fields required by consolidated and KPP reports."""
from __future__ import annotations

import io
import tempfile
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Sequence

import psycopg
from openpyxl import Workbook

import consolidated_cache as cache
from arvento_first_entry_report import load_roster as load_first_entry_roster
from consolidated_multi_report import load_rosters

_BASE_ENSURE_SCHEMA = cache.ensure_schema
_PATCHED = False


def ensure_schema(connection: psycopg.Connection) -> None:
    """Create the base cache schema and add detailed roster fields idempotently."""
    _BASE_ENSURE_SCHEMA(connection)
    with connection.cursor() as cursor:
        cursor.execute(
            "ALTER TABLE consolidated_roster_entries "
            "ADD COLUMN IF NOT EXISTS model TEXT NOT NULL DEFAULT ''"
        )
        cursor.execute(
            "ALTER TABLE consolidated_roster_entries "
            "ADD COLUMN IF NOT EXISTS position TEXT NOT NULL DEFAULT ''"
        )
        cursor.execute(
            "ALTER TABLE consolidated_roster_entries "
            "ADD COLUMN IF NOT EXISTS directorate TEXT NOT NULL DEFAULT ''"
        )


def _write_roster_workbook(rows: Sequence[tuple[str, str, str, str, str, str, str]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Разнарядка"
    sheet.append([
        "Гос рег знак",
        "Компания или фирма",
        "Марка, модель",
        "Грейд",
        "ПОЛЬЗОВАТЕЛЬ",
        "Должность",
        "Дирекция",
    ])
    for row in rows:
        sheet.append(row)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for column, width in {
        "A": 18,
        "B": 28,
        "C": 26,
        "D": 14,
        "E": 38,
        "F": 38,
        "G": 52,
    }.items():
        sheet.column_dimensions[column].width = width
    stream = io.BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def save_roster_uploads(database_url: str, uploads: Sequence[tuple[str, bytes]]) -> int:
    """Archive roster snapshots including model, position and directorate."""
    if not uploads:
        return 0

    with tempfile.TemporaryDirectory(prefix="arvento_roster_archive_") as temp_name:
        temp_dir = Path(temp_name)
        paths: list[Path] = []
        original_names: dict[Path, str] = {}
        for index, (filename, content) in enumerate(uploads, start=1):
            target = temp_dir / f"{index:02d}_{Path(filename).name}"
            target.write_bytes(content)
            paths.append(target)
            original_names[target.resolve()] = Path(filename).name

        rosters = load_rosters(paths)
        detailed_by_path = {
            path.resolve(): load_first_entry_roster(path)
            for path in paths
        }

        with psycopg.connect(database_url) as connection:
            ensure_schema(connection)
            with connection.cursor() as cursor:
                for roster in rosters:
                    source_name = original_names.get(roster.path.resolve(), roster.path.name)
                    detailed = detailed_by_path.get(roster.path.resolve(), {})
                    cursor.execute(
                        """
                        INSERT INTO consolidated_roster_snapshots(
                            roster_day, source_filename, entry_count, loaded_at
                        ) VALUES (%s,%s,%s,now())
                        ON CONFLICT (roster_day) DO UPDATE SET
                            source_filename=EXCLUDED.source_filename,
                            entry_count=EXCLUDED.entry_count,
                            loaded_at=now()
                        """,
                        (roster.day, source_name, len(roster.vehicles)),
                    )
                    cursor.execute(
                        "DELETE FROM consolidated_roster_entries WHERE roster_day=%s",
                        (roster.day,),
                    )
                    for normalized, vehicle in roster.vehicles.items():
                        details = detailed.get(normalized)
                        driver = vehicle.user or (details.driver if details else "")
                        cursor.execute(
                            """
                            INSERT INTO consolidated_roster_entries(
                                roster_day, normalized_plate, plate, company,
                                user_name, grade, model, position, directorate
                            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            """,
                            (
                                roster.day,
                                normalized,
                                vehicle.plate,
                                vehicle.company,
                                driver,
                                vehicle.grade,
                                details.model if details else "",
                                details.position if details else "",
                                details.directorate if details else "",
                            ),
                        )
            connection.commit()
    return len(rosters)


def _load_all_rows(database_url: str) -> dict[date, list[tuple[str, str, str, str, str, str, str]]]:
    with psycopg.connect(database_url) as connection:
        ensure_schema(connection)
        connection.commit()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    roster_day, plate, company, model, grade,
                    user_name, position, directorate
                FROM consolidated_roster_entries
                ORDER BY roster_day, normalized_plate
                """
            )
            rows = cursor.fetchall()

    grouped: defaultdict[date, list[tuple[str, str, str, str, str, str, str]]] = defaultdict(list)
    for roster_day, plate, company, model, grade, user_name, position, directorate in rows:
        grouped[roster_day].append(
            (
                plate or "",
                company or "",
                model or "",
                grade or "",
                user_name or "",
                position or "",
                directorate or "",
            )
        )
    return grouped


def export_stored_rosters(database_url: str, target_dir: Path) -> list[Path]:
    """Export stored snapshots with all fields needed by every roster-based report."""
    grouped = _load_all_rows(database_url)
    if not grouped:
        raise ValueError(
            "В базе нет сохранённых разнарядок. Сначала загрузите разнарядку на странице «Разнарядки»."
        )

    result: list[Path] = []
    for roster_day in sorted(grouped):
        path = target_dir / f"roster_{roster_day.isoformat()}.xlsx"
        path.write_bytes(_write_roster_workbook(grouped[roster_day]))
        result.append(path)
    return result


def build_roster_download(roster_day: date) -> tuple[bytes, str]:
    """Build a complete XLSX snapshot suitable for both KPP and consolidated reports."""
    import consolidated_portal as portal

    database_url = portal.implementation.db_url()
    with psycopg.connect(database_url) as connection:
        ensure_schema(connection)
        connection.commit()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM consolidated_roster_snapshots WHERE roster_day=%s",
                (roster_day,),
            )
            if cursor.fetchone() is None:
                raise ValueError("Разнарядка за выбранную дату не найдена")
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

    return _write_roster_workbook(rows), f"Разнарядка_{roster_day.isoformat()}.xlsx"


def apply_extended_roster_fields() -> None:
    """Patch cache, worker-facing and portal-facing roster functions."""
    global _PATCHED

    # Reapply on every call: during circular imports roster_management_portal may
    # still be only partially initialized and later overwrite its globals.
    cache.ensure_schema = ensure_schema
    cache.save_roster_uploads = save_roster_uploads
    cache.export_stored_rosters = export_stored_rosters

    try:
        import consolidated_cache_portal as cache_portal
        cache_portal.save_roster_uploads = save_roster_uploads
    except ImportError:
        pass

    try:
        import roster_management_portal as roster_portal
        roster_portal.ensure_schema = ensure_schema
        roster_portal.save_roster_uploads = save_roster_uploads
        roster_portal.build_roster_download = build_roster_download
    except ImportError:
        pass

    _PATCHED = True


__all__ = [
    "apply_extended_roster_fields",
    "build_roster_download",
    "ensure_schema",
    "export_stored_rosters",
    "save_roster_uploads",
]
