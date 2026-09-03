#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Roster-aware cache policy for the production portal.

Roster changes must not invalidate heavy GPS calculations. Cached vehicle-day
metrics are kept reusable while current roster attributes are overlaid when a
cached workbook is rendered. Identical roster uploads are ignored semantically
so their ``loaded_at`` timestamp is not needlessly advanced.
"""
from __future__ import annotations

import logging
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence

import psycopg
import psycopg.rows

from arvento_first_entry_report import load_roster as load_detailed_roster
from cache_freshness import load_cache_coverage
from consolidated_multi_report import DatedRoster, load_rosters
from responsible_roster_fields import _load_responsible_values


LOGGER = logging.getLogger(__name__)
_BASE_SAVE_ROSTERS = None
_PATCHED = False


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _desired_roster_state(roster: DatedRoster) -> tuple[tuple[str, ...], ...]:
    """Return the canonical DB state represented by one uploaded workbook."""
    detailed = load_detailed_roster(roster.path)
    responsible = _load_responsible_values(roster.path)
    rows: list[tuple[str, ...]] = []
    for normalized, vehicle in sorted(roster.vehicles.items()):
        details = detailed.get(normalized)
        driver = vehicle.user or (details.driver if details else "")
        rows.append(
            (
                normalized,
                _clean(vehicle.plate),
                _clean(vehicle.company),
                _clean(details.model if details else ""),
                _clean(vehicle.grade),
                _clean(driver),
                _clean(details.position if details else ""),
                _clean(details.directorate if details else ""),
                _clean(responsible.get(normalized, "")),
            )
        )
    return tuple(rows)


def _stored_roster_state(cursor, roster_day: date) -> tuple[tuple[str, ...], ...] | None:
    cursor.execute(
        "SELECT 1 FROM consolidated_roster_snapshots WHERE roster_day=%s",
        (roster_day,),
    )
    if cursor.fetchone() is None:
        return None
    cursor.execute(
        """
        SELECT
            normalized_plate, plate, company, model, grade,
            user_name, position, directorate, responsible
        FROM consolidated_roster_entries
        WHERE roster_day=%s
        ORDER BY normalized_plate
        """,
        (roster_day,),
    )
    return tuple(
        tuple(_clean(value) for value in row)
        for row in cursor.fetchall()
    )


def save_roster_uploads_if_changed(
    database_url: str,
    uploads: Sequence[tuple[str, bytes]],
) -> int:
    """Persist only roster dates whose parsed business content actually changed."""
    if not uploads:
        return 0
    if _BASE_SAVE_ROSTERS is None:
        raise RuntimeError("Политика кэша разнарядок не инициализирована")

    with tempfile.TemporaryDirectory(prefix="arvento_roster_compare_") as temp_name:
        temp_dir = Path(temp_name)
        upload_by_path: dict[Path, tuple[str, bytes]] = {}
        paths: list[Path] = []
        for index, (filename, content) in enumerate(uploads, start=1):
            target = temp_dir / f"{index:02d}_{Path(filename).name}"
            target.write_bytes(content)
            resolved = target.resolve()
            paths.append(target)
            upload_by_path[resolved] = (Path(filename).name, content)

        rosters = load_rosters(paths)
        desired = {
            roster.day: (_desired_roster_state(roster), upload_by_path[roster.path.resolve()])
            for roster in rosters
        }

        import responsible_roster_fields as responsible

        changed_uploads: list[tuple[str, bytes]] = []
        with psycopg.connect(database_url) as connection:
            responsible.ensure_schema(connection)
            connection.commit()
            with connection.cursor() as cursor:
                for roster_day, (wanted_state, original_upload) in desired.items():
                    if _stored_roster_state(cursor, roster_day) != wanted_state:
                        changed_uploads.append(original_upload)

        if not changed_uploads:
            LOGGER.info(
                "ROSTER UNCHANGED dates=%s; loaded_at preserved",
                ",".join(day.isoformat() for day in sorted(desired)),
            )
            return 0

        saved = _BASE_SAVE_ROSTERS(database_url, changed_uploads)
        LOGGER.info(
            "ROSTER UPDATED changed=%s dates=%s",
            saved,
            ",".join(
                day.isoformat()
                for day, (_state, original) in sorted(desired.items())
                if original in changed_uploads
            ),
        )
        return saved


def load_cached_rows_with_current_roster(
    database_url: str,
    start_day: date,
    end_day: date,
) -> tuple[list[dict[str, Any]], datetime | None]:
    """Load cached GPS metrics and overlay the roster effective for each day."""
    import consolidated_cache as cache

    with psycopg.connect(database_url, row_factory=psycopg.rows.dict_row) as connection:
        cache.ensure_schema(connection)
        connection.commit()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    c.*,
                    effective_roster.roster_day AS effective_roster_day,
                    effective_roster.source_filename AS effective_roster_filename,
                    COALESCE(e.company, '') AS effective_company,
                    COALESCE(e.user_name, '') AS effective_user_name,
                    COALESCE(e.grade, '') AS effective_grade,
                    (e.normalized_plate IS NOT NULL) AS effective_in_roster
                FROM consolidated_report_cache AS c
                LEFT JOIN LATERAL (
                    SELECT roster_day, source_filename
                    FROM consolidated_roster_snapshots
                    WHERE roster_day <= c.report_day
                    ORDER BY roster_day DESC
                    LIMIT 1
                ) AS effective_roster ON TRUE
                LEFT JOIN consolidated_roster_entries AS e
                  ON e.roster_day = effective_roster.roster_day
                 AND e.normalized_plate = c.normalized_plate
                WHERE c.report_day BETWEEN %s AND %s
                ORDER BY c.report_day, COALESCE(e.company, ''), c.normalized_plate
                """,
                (start_day, end_day),
            )
            rows = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT MAX(refreshed_at)
                FROM consolidated_cache_days
                WHERE report_day BETWEEN %s AND %s
                """,
                (start_day, end_day),
            )
            refreshed_at = cursor.fetchone()["max"]

    for item in rows:
        item["company"] = item.pop("effective_company")
        item["user_name"] = item.pop("effective_user_name")
        item["grade"] = item.pop("effective_grade")
        item["in_roster"] = bool(item.pop("effective_in_roster"))
        item["roster_day"] = item.pop("effective_roster_day")
        item["roster_filename"] = item.pop("effective_roster_filename") or ""
    return rows, refreshed_at


def cache_complete_with_logging(database_url: str, start_day: date, end_day: date) -> bool:
    """Return cache readiness and emit an actionable hit/miss reason."""
    import consolidated_cache as cache

    with psycopg.connect(database_url) as connection:
        cache.ensure_schema(connection)
        connection.commit()
        coverage = load_cache_coverage(connection, start_day, end_day)

    if coverage.complete:
        LOGGER.info(
            "CACHE HIT consolidated period=%s..%s ready=%s/%s",
            start_day,
            end_day,
            coverage.ready_days,
            coverage.expected_days,
        )
    else:
        reasons = "; ".join(
            f"{day.isoformat()}:{','.join(day_reasons)}"
            for day, day_reasons in coverage.stale_reasons
        )
        LOGGER.warning(
            "CACHE MISS consolidated period=%s..%s stale=%s reasons=%s",
            start_day,
            end_day,
            ",".join(day.isoformat() for day in coverage.stale_days),
            reasons or "unknown",
        )
    return coverage.complete


def apply_roster_cache_policy() -> None:
    """Install the roster-independent cache policy after roster field patches."""
    global _BASE_SAVE_ROSTERS, _PATCHED
    if _PATCHED:
        return

    import central_roster_reports as central
    import consolidated_cache as cache
    import consolidated_cache_portal as cache_portal
    import extended_roster_fields as extended
    import responsible_roster_fields as responsible
    import roster_management_portal as roster_portal

    _BASE_SAVE_ROSTERS = responsible.save_roster_uploads

    cache.load_cached_rows = load_cached_rows_with_current_roster
    cache.save_roster_uploads = save_roster_uploads_if_changed
    cache_portal.save_roster_uploads = save_roster_uploads_if_changed
    extended.save_roster_uploads = save_roster_uploads_if_changed
    responsible.save_roster_uploads = save_roster_uploads_if_changed
    roster_portal.save_roster_uploads = save_roster_uploads_if_changed

    # Central consolidated reports are the production entry path. Keep their
    # cache decision explicit in logs while the cached generator itself still
    # uses the same freshness policy underneath.
    central.cache_complete = cache_complete_with_logging
    _PATCHED = True


__all__ = [
    "apply_roster_cache_policy",
    "cache_complete_with_logging",
    "load_cached_rows_with_current_roster",
    "save_roster_uploads_if_changed",
]
