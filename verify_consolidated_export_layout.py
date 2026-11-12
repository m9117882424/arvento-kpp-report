#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline verification for consolidated export and roster-field selection."""
from __future__ import annotations

import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.utils.datetime import CALENDAR_MAC_1904

from consolidated_export_layout import (
    EXPORT_HEADERS,
    GRADE_HEADER,
    GRADE_HEADERS,
    PLATE_DATE_HEADER,
    REPORT_SHEET,
    RosterExportDetails,
    finalize_consolidated_workbook,
    load_roster_export_details,
)
from responsible_roster_fields import _load_responsible_values


def verify_responsible_source(temp_dir: Path) -> None:
    roster_path = temp_dir / "28.07.2026 roster.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Список легкового автотранспорта"
    sheet.append(["служебная строка"])
    sheet.append([])
    sheet.append([
        "Гос рег знак / PLAKA",
        "Ответственный по подразделению / Birim sorumlusu",
        "Ответственный / Sorumlu",
    ])
    sheet.append([
        "33 ATB 635",
        "Виктория Лодыгина / Victoria Lodygina",
        "Евгений Яценко / IATCENKO EVGENII",
    ])
    workbook.save(roster_path)
    workbook.close()

    values = _load_responsible_values(roster_path)
    assert values["33ATB635"] == "Виктория Лодыгина / Victoria Lodygina"
    assert "Евгений Яценко" not in values["33ATB635"]


REPORT_DAY = date(2026, 9, 2)
MODEL = "FORD TRANSIT 2.2 TDCi / 16+1"
FORBIDDEN_HEADERS = (
    "Нарушение геозоны / Sınır ihlali", "Нарушение геозоны",
    "Имя водителя", "Проверка пробега",
)
ROSTER_DETAILS = {
    (REPORT_DAY, "34ABC123"): RosterExportDetails(
        driver_name="Актуальный водитель",
        grade="9b",
        model=MODEL,
        position="Начальник участка / Şef",
        directorate="Дирекция",
        responsible="Ответственный",
    ),
    (REPORT_DAY, "34EMPTY1"): RosterExportDetails(),
}


def make_report(workbook):
    """Use interleaved legacy columns so shifts can corrupt retained metrics."""
    report = workbook.active
    report.title = REPORT_SHEET
    headers = [
        "Дата", "Компания или фирма", "Госномер / Plaka",
        "ПОЛЬЗОВАТЕЛЬ / KULLANICI", "Грейд / SCALA", "Пробег общий, км",
        "Нарушение геозоны / Sınır ihlali", "Заправка, л", "Время въезда",
        "Время работы", "Доля пробега", "Грейд", "Имя водителя",
        "Проверка пробега", "SCALA", "Grade", "Нарушение геозоны", "Грейд",
    ]
    report.append(headers)
    for plate in ("34 ABC 123", "34 EMPTY 1", "34 MISSING 2"):
        report.append([
            REPORT_DAY, "TSM", plate, "Пользователь из отчёта", "7a", 123.4,
            "Да", 12.3, time(8, 15), timedelta(hours=26, minutes=30), 0.45,
            "7b", "Лишний водитель", "Проверить", "7c", "7d", "Да", "7e",
        ])
    for index, cell in enumerate(report[1], 1):
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
        report.column_dimensions[get_column_letter(index)].width = 15 + index
    for row in report.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            cell.border = Border(bottom=Side(style="thin", color="123456"))
        for index, number_format in {
            0: "dd.mm.yyyy", 5: "0.0", 7: "0.0", 8: "hh:mm",
            9: "[h]:mm", 10: "0.0%",
        }.items():
            row[index].number_format = number_format
    report.column_dimensions["I"].hidden = True
    report.column_dimensions["I"].outlineLevel = 1
    report.row_dimensions[1].height = 48
    report.freeze_panes = "F2"
    report.auto_filter.ref = report.dimensions
    report.auto_filter.add_filter_column(7, ["12.3"])
    report.auto_filter.add_filter_column(4, ["7a"])
    workbook.create_sheet("Диагностика")
    parameters = workbook.create_sheet("Параметры")
    parameters.append(["Параметр", "Значение"])
    return report


def retained_snapshot(sheet):
    return {
        cell.value: (
            tuple((sheet.cell(row, cell.column).value, sheet.cell(row, cell.column)._style)
                  for row in range(1, sheet.max_row + 1)),
            sheet.column_dimensions[cell.column_letter].width,
            sheet.column_dimensions[cell.column_letter].hidden,
            sheet.column_dimensions[cell.column_letter].outlineLevel,
        )
        for cell in sheet[1]
        if cell.value not in FORBIDDEN_HEADERS and cell.value not in EXPORT_HEADERS
        and cell.value not in GRADE_HEADERS
        and cell.value != PLATE_DATE_HEADER
    }


def assert_export(sheet, original):
    headers = [cell.value for cell in sheet[1]]
    assert headers[0] == PLATE_DATE_HEADER
    assert headers.count(GRADE_HEADER) == 1
    assert headers.index(GRADE_HEADER) == headers.index("ПОЛЬЗОВАТЕЛЬ / KULLANICI") + 1
    assert not (set(GRADE_HEADERS) - {GRADE_HEADER}).intersection(headers)
    assert not set(FORBIDDEN_HEADERS).intersection(headers), headers
    assert tuple(headers[-4:]) == ("Марка", "Должность", "Дирекция", "Ответственный")
    assert tuple(headers[-4:]) == EXPORT_HEADERS
    assert retained_snapshot(sheet) == original
    assert sheet.freeze_panes == "G2"
    assert len(sheet.sheet_view.selection) == 3
    assert sheet.row_dimensions[1].height == 48
    assert sheet.auto_filter.ref == sheet.dimensions
    filters = sheet.auto_filter.filterColumn
    assert len(filters) == 2
    by_column = {item.colId: item.filters.filter for item in filters}
    assert by_column[headers.index("Заправка, л")] == ["12.3"]
    assert by_column[headers.index(GRADE_HEADER)] == ["7a"]
    assert [sheet.cell(row, headers.index(GRADE_HEADER) + 1).value for row in (2, 3, 4)] == [
        "9b", "7a", "7a",
    ]
    assert [sheet.cell(row, 1).value for row in (2, 3, 4)] == [
        "34ABC12346267", "34EMPTY146267", "34MISSING246267",
    ]
    assert sheet.cell(2, 1).number_format == "@"
    assert [sheet.cell(2, headers.index(name) + 1).value for name in EXPORT_HEADERS] == [
        MODEL, "Начальник участка / Şef", "Дирекция", "Ответственный",
    ]
    # Both an empty roster record and a missing plate leave the new fields blank.
    for row in (3, 4):
        assert all(sheet.cell(row, headers.index(name) + 1).value in (None, "")
                   for name in EXPORT_HEADERS)
    for name in EXPORT_HEADERS:
        header = sheet.cell(1, headers.index(name) + 1)
        assert header.font.bold and header.fill.fgColor.rgb == "001F4E78"


def verify_roster_loader() -> None:
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (REPORT_DAY,)
    cursor.fetchall.return_value = [
        ("34ABC123", "Актуальный водитель", "9b", "Дирекция", "Ответственный",
         MODEL, "Начальник участка / Şef"),
        ("34EMPTY1", None, None, None, None, None, None),
    ]
    next_day = REPORT_DAY + timedelta(days=1)
    with patch("consolidated_export_layout.psycopg.connect") as connect, patch(
        "consolidated_export_layout.ensure_schema"
    ):
        connect.return_value.__enter__.return_value = connection
        assert load_roster_export_details("offline", []) == {}
        connect.assert_not_called()
        details = load_roster_export_details("offline", [REPORT_DAY, next_day, REPORT_DAY])
    assert details == {
        **ROSTER_DETAILS,
        **{(next_day, plate): value for (_, plate), value in ROSTER_DETAILS.items()},
    }
    cursor.fetchall.assert_called_once()
    entry_queries = [call.args[0] for call in cursor.execute.call_args_list
                     if "FROM consolidated_roster_entries" in call.args[0]]
    assert len(entry_queries) == 1
    selected = entry_queries[0].split("SELECT", 1)[1].split("FROM", 1)[0]
    assert [name.strip() for name in selected.split(",")] == [
        "normalized_plate", "user_name", "grade", "directorate", "responsible",
        "model", "position",
    ]


def verify_plate_date_keys(temp_dir: Path) -> None:
    for use_mac_epoch in (False, True):
        output_path = temp_dir / f"keys_{use_mac_epoch}.xlsx"
        workbook = Workbook()
        if use_mac_epoch:
            workbook.epoch = CALENDAR_MAC_1904
        report = workbook.active
        report.title = REPORT_SHEET
        report.append(["Дата", "Госномер / Plaka"])
        for day, plate in (
            (date(2026, 11, 8), "46 AJP 556"),
            (datetime(2026, 11, 9, 13, 15), "46ajp556"),
            ("08.11.2026", "46AJP556"),
            (44872 if use_mac_epoch else 46334, "46AJP556"),
            (None, "46AJP556"),
            (date(2026, 11, 8), None),
        ):
            report.append([day, plate])
        workbook.save(output_path)
        workbook.close()
        for _ in range(2):
            finalize_consolidated_workbook(output_path, {})
            result = load_workbook(output_path, data_only=True)
            try:
                sheet = result[REPORT_SHEET]
                expected = "46AJP55644872" if use_mac_epoch else "46AJP55646334"
                next_day = "46AJP55644873" if use_mac_epoch else "46AJP55646335"
                assert [sheet.cell(row, 1).value for row in range(2, 8)] == [
                    expected, next_day, expected, expected, None, None,
                ]
                assert [cell.value for cell in sheet[1]].count(PLATE_DATE_HEADER) == 1
                assert sheet.cell(1, 1).value == PLATE_DATE_HEADER
                assert sheet.cell(2, 1).data_type == "s"
            finally:
                result.close()


def verify_grade_restored_without_source_column(temp_dir: Path) -> None:
    output_path = temp_dir / "restore_grade.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = REPORT_SHEET
    sheet.append([PLATE_DATE_HEADER, "Дата", "Госномер / Plaka", "Пользователь", "Пробег общий, км"])
    sheet.append(["old key", REPORT_DAY, "34 ABC 123", "Пользователь 1", 123.4])
    sheet.append(["old key", REPORT_DAY + timedelta(days=1), "34 ABC 123", "Пользователь 2", 50.0])
    sheet.append(["old key", REPORT_DAY, "34 UNKNOWN 1", "Неизвестный", 0.0])
    sheet.column_dimensions["E"].width = 25
    sheet.freeze_panes = "E2"
    sheet.auto_filter.ref = sheet.dimensions
    sheet.auto_filter.add_filter_column(4, ["123.4"])
    workbook.save(output_path)
    workbook.close()
    details = {
        (REPORT_DAY, "34ABC123"): RosterExportDetails(grade="9b"),
        (REPORT_DAY + timedelta(days=1), "34ABC123"): RosterExportDetails(grade="10a"),
    }
    for _ in range(2):
        finalize_consolidated_workbook(output_path, details)
        result = load_workbook(output_path)
        try:
            sheet = result[REPORT_SHEET]
            headers = [cell.value for cell in sheet[1]]
            assert headers[:6] == [PLATE_DATE_HEADER, "Дата", "Госномер / Plaka", "Пользователь", GRADE_HEADER, "Пробег общий, км"]
            assert [sheet.cell(row, 5).value for row in (2, 3, 4)] == ["9b", "10a", None]
            assert headers.count(GRADE_HEADER) == 1
            assert sheet.column_dimensions["F"].width == 25
            assert sheet.freeze_panes == "F2"
            assert sheet.auto_filter.filterColumn[0].colId == 5
            assert sheet.cell(2, 6).value == 123.4
            assert sheet.cell(2, 1).value == "34ABC12346267"
        finally:
            result.close()


def main() -> int:
    verify_roster_loader()
    with tempfile.TemporaryDirectory(prefix="verify_consolidated_export_") as temp_name:
        temp_dir = Path(temp_name)
        verify_plate_date_keys(temp_dir)
        verify_grade_restored_without_source_column(temp_dir)
        verify_responsible_source(temp_dir)
        output_path = temp_dir / "report.xlsx"
        workbook = Workbook()
        make_report(workbook)
        workbook.save(output_path)
        workbook.close()
        workbook = load_workbook(output_path)
        original = retained_snapshot(workbook[REPORT_SHEET])
        workbook.close()

        # Finalizing an already-finalized export must not duplicate fields or
        # shift retained values, styles, widths, filters or frozen columns again.
        for _ in range(2):
            stats = finalize_consolidated_workbook(output_path, ROSTER_DETAILS)
            result = load_workbook(output_path)
            try:
                assert result.sheetnames == [REPORT_SHEET], result.sheetnames
                assert_export(result[REPORT_SHEET], original)
                assert stats == {
                    "sheets": 1, "rows": 3, "enriched_rows": 2,
                    "columns": result[REPORT_SHEET].max_column,
                }
            finally:
                result.close()

    print("OK: roster model/position, removed aliases, retained values/formatting and idempotence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
