#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Persist the optional responsible person from centrally uploaded rosters.

The established roster store already keeps driver, grade, position and
Directorate. This patch adds one optional ``responsible`` field without changing
existing report calculations. Supported source headers are explicit variants of
``Ответственный``/``Responsible``/``Sorumlu``; when the source column is absent,
the stored value remains empty.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Sequence

import psycopg
from openpyxl import load_workbook

import consolidated_cache as cache
import extended_roster_fields as extended
from consolidated_multi_report import load_rosters
from roster_registry import normalize_plate

_BASE_ENSURE_SCHEMA = extended.ensure_schema
_BASE_SAVE_ROSTER_UPLOADS = extended.save_roster_uploads
_PATCHED = False

PLATE_ALIASES = (
    "Гос рег знак",
    "PLAKA",
    "Госномер",
    "Номерной знак",
    "License Plate",
)
RESPONSIBLE_ALIASES = (
    "Ответственный",
    "Ответственное лицо",
    "Responsible",
    "Responsible person",
    "Sorumlu",
    "Sorumlu kişi",
    "Sorumlu kisi",
)


def _clean(value: Any) -> str:
    return " ".join(str(value or "").replace("\n", " ").split()).strip()


def _normalized(value: Any) -> str:
    return _clean(value).casefold().replace("ё", "е").replace("ı", "i")


def _find_column(values: Sequence[Any], aliases: Sequence[str]) -> int | None:
    headers = [_normalized(value) for value in values]
    for alias in aliases:
        needle = _normalized(alias)
        for index, header in enumerate(headers):
            if header == needle or needle in header:
                return index
    return None


def _load_responsible_values(path: Path) -> dict[str, str]:
    """Read responsible persons keyed by normalized plate from one roster file."""
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        result: dict[str, str] = {}
        for sheet in workbook.worksheets:
            iterator = iter(sheet.iter_rows(values_only=True))
            plate_column: int | None = None
            responsible_column: int | None = None

            for _ in range(80):
                try:
                    row = next(iterator)
                except StopIteration:
                    break
                candidate_plate = _find_column(row, PLATE_ALIASES)
                candidate_responsible = _find_column(row, RESPONSIBLE_ALIASES)
                if candidate_plate is not None:
                    plate_column = candidate_plate
                    responsible_column = candidate_responsible
                    break

            if plate_column is None or responsible_column is None:
                continue

            for row in iterator:
                if plate_column >= len(row):
                    continue
                normalized_plate = normalize_plate(row[plate_column])
                if not normalized_plate:
                    continue
                value = (
                    _clean(row[responsible_column])
                    if responsible_column < len(row)
                    else ""
                )
                if value:
                    result[normalized_plate] = value
        return result
    finally:
        workbook.close()


def ensure_schema(connection: psycopg.Connection) -> None:
    """Create the existing central roster schema and add ``responsible``."""
    _BASE_ENSURE_SCHEMA(connection)
    with connection.cursor() as cursor:
        cursor.execute(
            "ALTER TABLE consolidated_roster_entries "
            "ADD COLUMN IF NOT EXISTS responsible TEXT NOT NULL DEFAULT ''"
        )


def save_roster_uploads(
    database_url: str,
    uploads: Sequence[tuple[str, bytes]],
) -> int:
    """Save the established roster fields and then persist responsible persons."""
    if not uploads:
        return 0

    saved = _BASE_SAVE_ROSTER_UPLOADS(database_url, uploads)

    with tempfile.TemporaryDirectory(prefix="arvento_responsible_roster_") as temp_name:
        temp_dir = Path(temp_name)
        paths: list[Path] = []
        responsible_by_path: dict[Path, dict[str, str]] = {}

        for index, (filename, content) in enumerate(uploads, start=1):
            path = temp_dir / f"{index:02d}_{Path(filename).name}"
            path.write_bytes(content)
            paths.append(path)
            responsible_by_path[path.resolve()] = _load_responsible_values(path)

        # load_rosters applies the same dated-roster and duplicate-date rules as
        # the established central store. The last upload for a duplicate date is
        # authoritative.
        rosters = load_rosters(paths)

        with psycopg.connect(database_url) as connection:
            ensure_schema(connection)
            connection.commit()
            with connection.cursor() as cursor:
                for roster in rosters:
                    values = responsible_by_path.get(roster.path.resolve(), {})
                    for normalized_plate, responsible in values.items():
                        cursor.execute(
                            """
                            UPDATE consolidated_roster_entries
                            SET responsible=%s
                            WHERE roster_day=%s
                              AND normalized_plate=%s
                            """,
                            (responsible, roster.day, normalized_plate),
                        )
            connection.commit()

    return saved


def apply_responsible_roster_fields() -> None:
    """Install the optional responsible field in all portal-facing roster paths."""
    global _PATCHED
    if _PATCHED:
        return

    extended.ensure_schema = ensure_schema
    extended.save_roster_uploads = save_roster_uploads
    cache.ensure_schema = ensure_schema
    cache.save_roster_uploads = save_roster_uploads

    try:
        import consolidated_cache_portal as cache_portal

        cache_portal.save_roster_uploads = save_roster_uploads
    except ImportError:
        pass

    try:
        import roster_management_portal as roster_portal

        roster_portal.ensure_schema = ensure_schema
        roster_portal.save_roster_uploads = save_roster_uploads
    except ImportError:
        pass

    _PATCHED = True


__all__ = [
    "RESPONSIBLE_ALIASES",
    "apply_responsible_roster_fields",
    "ensure_schema",
    "save_roster_uploads",
]
