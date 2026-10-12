#!/usr/bin/env python3
"""Regression: cache a three-day workbook without random read-only access."""
from __future__ import annotations

import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from openpyxl import Workbook
from openpyxl.worksheet._read_only import ReadOnlyWorksheet

import consolidated_cache as cache
import consolidated_report as core
from cache_freshness import SourceWatermark
from fuel_enriched_consolidated_report import FUEL_HEADER


def main() -> None:
    start_day = date(2026, 10, 9)
    end_day = start_day + timedelta(days=2)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Сводный отчет"
    headers = [*core.HEADERS, FUEL_HEADER]
    sheet.append(headers)
    diagnostics = workbook.create_sheet("Диагностика")
    diagnostics.append([
        "Дата", "Госномер", "В разнарядке", "Исходных GPS-точек",
        "Точек после удаления координатных выбросов",
        "Точек для расчёта максимальной скорости",
        "Максимальное удаление от АЭС, км", "Дата разнарядки",
        "Файл разнарядки",
    ])
    for offset in range(3):
        report_day = start_day + timedelta(days=offset)
        for index in range(205):
            plate = f"34A{index:04d}"
            values = {
                "Дата": datetime.combine(report_day, datetime.min.time()),
                "Компания или фирма": "TSM",
                "Госномер / Plaka": plate,
                "ПОЛЬЗОВАТЕЛЬ / KULLANICI": "Водитель",
                "Грейд / SCALA": "5",
                "Максимальная скорость / Maximum Hız": 80.5,
                "Пробег общий, км": 10.5,
                "Пробег внутри АЭС АККУЮ, км": 4.0,
                "Пробег вне площадки, км": 6.5,
                "Прибыл / Giriş": datetime.min.time(),
                "Всего отработано часов": None if index == 0 else 0,
                FUEL_HEADER: "2.5",
            }
            sheet.append([values.get(header) for header in headers])
            if index < 204:
                diagnostics.append([
                    report_day, plate, "да", "10", 9, 8, "1.5",
                    report_day, "central.xlsx",
                ])
    # Out-of-period and empty-plate rows must not replace daily cache metrics.
    for report_day, plate in ((start_day - timedelta(days=1), "34A0000"),
                              (start_day, "")):
        values = {"Дата": report_day, "Госномер / Plaka": plate}
        sheet.append([values.get(header) for header in headers])

    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.return_value = (73,)
    connection.cursor.return_value = cursor
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    watermark = SourceWatermark(now, now, now, now, now, 199)

    with tempfile.TemporaryDirectory(prefix="arvento_cache_workbook_test_") as temp:
        path = Path(temp) / "three-days.xlsx"
        workbook.save(path)
        workbook.close()
        with (
            patch.object(cache.psycopg, "connect", return_value=connection),
            patch.object(cache, "ensure_schema"),
            patch.object(cache, "load_source_watermark", return_value=watermark),
            patch.object(ReadOnlyWorksheet, "cell", side_effect=AssertionError(
                "Random read-only access reparses the entire worksheet"
            )),
        ):
            result = cache.upsert_cache_from_workbook(
                "unused", path, start_day, end_day, trigger_name="regression"
            )

    records = [
        call.args[1]
        for call in cursor.execute.call_args_list
        if call.args[0].lstrip().startswith("INSERT INTO consolidated_report_cache(")
    ]
    assert result == {"run_id": 73, "rows": 615, "days": 3}, result
    assert len(records) == 615
    assert len({(row[0], row[1]) for row in records}) == 615
    first = records[0]
    assert first[:7] == (start_day, "34A0000", "TSM", "34A0000", "Водитель", "5", 80.5)
    assert first[9:12] == (10.5, 4.0, 6.5)
    assert first[19] == datetime.min.time()
    assert first[21] is None
    assert records[1][21] == 0.0
    assert first[26:35] == (2.5, True, 10, 9, 8, 1.5, start_day, "central.xlsx", 73)
    assert records[204][27:34] == (True, 0, 0, 0, 0.0, None, "")
    print("OK: 615 cache rows streamed once with metrics, nulls and diagnostics preserved")


if __name__ == "__main__":
    main()
