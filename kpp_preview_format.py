from __future__ import annotations

"""Column-aware formatting for the KPP browser preview."""

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

DATE_ONLY_HEADERS = {"Дата въезда"}
_DATE_WITH_OPTIONAL_MIDNIGHT = re.compile(
    r"^(\d{2}\.\d{2}\.\d{4})(?:\s+00:00(?::00)?)?$"
)


def date_only_value(value: Any) -> Any:
    """Return a date-only display value without changing non-date content."""
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, str):
        match = _DATE_WITH_OPTIONAL_MIDNIGHT.fullmatch(value.strip())
        if match:
            return match.group(1)
    return value


def apply_kpp_preview_format(implementation: Any) -> None:
    """Format only the dedicated KPP entry-date column as a plain date."""
    original_preview = implementation.workbook_preview
    if getattr(original_preview, "_kpp_date_only_installed", False):
        return

    def workbook_preview(path: Path):
        columns, rows, total = original_preview(path)
        date_indexes = {
            index
            for index, header in enumerate(columns)
            if str(header or "").strip() in DATE_ONLY_HEADERS
        }
        if not date_indexes:
            return columns, rows, total

        formatted_rows: list[list[Any]] = []
        for source_row in rows:
            row = list(source_row)
            for index in date_indexes:
                if index < len(row):
                    row[index] = date_only_value(row[index])
            formatted_rows.append(row)
        return columns, formatted_rows, total

    workbook_preview._kpp_date_only_installed = True
    implementation.workbook_preview = workbook_preview


__all__ = ["DATE_ONLY_HEADERS", "apply_kpp_preview_format", "date_only_value"]
