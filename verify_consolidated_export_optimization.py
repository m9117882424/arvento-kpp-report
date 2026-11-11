#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline regression checks for the final consolidated download and preview."""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook, load_workbook

import consolidated_cache_portal as cache_portal
from consolidated_export_layout import PLATE_DATE_HEADER, REPORT_SHEET
from consolidated_export_optimization import (
    _add_cached_fuel_and_parameters,
    _apply_mileage_review,
    _apply_roster_layout,
    apply_cached_workbook_optimization,
    finalize_consolidated_output_fast,
    write_cached_workbook_fast,
)
from excel_formatting import save_report_workbook
from fuel_enriched_consolidated_report import FUEL_HEADER
from mileage_review_policy import (
    MileageReviewCandidate,
    REVIEW_HEADER,
    REVIEW_SHEET,
    REVIEW_VALUE,
)
from verify_consolidated_export_layout import (
    FORBIDDEN_HEADERS,
    MODEL,
    REPORT_DAY,
    ROSTER_DETAILS,
    assert_export,
    make_report,
    retained_snapshot,
)


NORMALIZED = "34ABC123"
CANDIDATES = {
    (REPORT_DAY, NORMALIZED): MileageReviewCandidate(
        report_day=REPORT_DAY,
        normalized_plate=NORMALIZED,
        authoritative_km=123.4,
        coordinate_km=100.0,
        gap_km=23.4,
        gap_percent=23.4,
        reason="Тест",
    ),
}


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="verify_fast_export_") as temp_name:
        output_path = Path(temp_name) / "report.xlsx"
        workbook = Workbook()
        make_report(workbook)
        fuel_total, fuel_rows = _add_cached_fuel_and_parameters(
            workbook,
            {(REPORT_DAY, NORMALIZED): 12.3},
            datetime(2026, 9, 3, 0, 30, tzinfo=timezone.utc),
        )
        assert fuel_total == 12.3 and fuel_rows == 1
        save_report_workbook(workbook, output_path)
        workbook.close()

        workbook = load_workbook(output_path)
        original = retained_snapshot(workbook[REPORT_SHEET])
        # The diagnostic annotator must still work on its own and add a marker.
        _apply_roster_layout(workbook, ROSTER_DETAILS)
        review = _apply_mileage_review(workbook, CANDIDATES)
        assert review["flagged_rows"] == 1
        headers = [cell.value for cell in workbook[REPORT_SHEET][1]]
        assert headers[-1] == REVIEW_HEADER
        assert workbook[REPORT_SHEET].cell(2, len(headers)).value == REVIEW_VALUE
        assert workbook[REVIEW_SHEET].cell(2, 4).value == "Пользователь из отчёта"
        workbook.close()

        # Exercise the actual final boundary, including a second run, both
        # with and without review candidates. Never connect to a real database.
        for candidates in (CANDIDATES, CANDIDATES, {}):
            with patch(
                "consolidated_export_optimization.load_roster_export_details",
                return_value=ROSTER_DETAILS,
            ) as roster_loader, patch(
                "consolidated_export_optimization.load_mileage_review_candidates",
                return_value=candidates,
            ) as review_loader:
                stats = finalize_consolidated_output_fast(
                    output_path, "offline", REPORT_DAY, REPORT_DAY,
                )
            roster_loader.assert_called_once_with("offline", [REPORT_DAY])
            review_loader.assert_called_once_with("offline", REPORT_DAY, REPORT_DAY)
            columns, rows = stats["columns"], stats["rows"]
            assert columns[0] == PLATE_DATE_HEADER
            assert rows[0][0] == "34ABC12346267"
            assert not set(FORBIDDEN_HEADERS).intersection(columns), columns
            assert stats["total_rows"] == len(rows) == 3
            assert stats["layout"]["enriched_rows"] == 2
            assert stats["review"] == {
                "candidates": len(candidates), "flagged_rows": len(candidates), "sheets": 2,
            }
            assert rows[0][columns.index("Дата")] == "02.09.2026"
            assert rows[0][columns.index("Марка")] == MODEL
            assert rows[0][columns.index("Должность")] == "Начальник участка / Şef"
            assert rows[0][columns.index("ПОЛЬЗОВАТЕЛЬ / KULLANICI")] == "Пользователь из отчёта"
            assert rows[0][columns.index("Пробег общий, км")] == 123.4
            assert rows[0][columns.index(FUEL_HEADER)] == 12.3
            for row in rows[1:]:
                assert row[columns.index("Марка")] == row[columns.index("Должность")] == ""
            assert all(len(row) == len(columns) for row in rows)

            result = load_workbook(output_path)
            try:
                assert result.sheetnames == [REPORT_SHEET, REVIEW_SHEET], result.sheetnames
                sheet = result[REPORT_SHEET]
                assert [cell.value for cell in sheet[1]] == columns
                assert_export(sheet, original)
                review = result[REVIEW_SHEET]
                assert review.max_row == len(candidates) + 1
                assert review.freeze_panes == "A2"
                if candidates:
                    assert review.cell(2, 4).value == "Актуальный водитель"
                    assert review.cell(2, 5).value == 123.4
                    assert review.cell(2, 6).value == 100.0
                    assert review.cell(2, 9).value == REVIEW_VALUE
                    assert review.cell(2, 10).value == "Тест"
            finally:
                result.close()

    apply_cached_workbook_optimization()
    assert cache_portal.write_cached_workbook is write_cached_workbook_fast
    print("OK: final XLSX and preview exclude forbidden columns; fuel, roster and review sheet preserved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
