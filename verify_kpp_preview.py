#!/usr/bin/env python3
"""Runtime regression test for KPP date/time columns in the web preview."""
from __future__ import annotations

import tempfile
from datetime import datetime, time
from pathlib import Path

from openpyxl import Workbook

import portal_entrypoint


def main() -> None:
    implementation = portal_entrypoint.portal.implementation
    with tempfile.TemporaryDirectory(prefix="arvento_kpp_preview_") as temp_name:
        path = Path(temp_name) / "kpp_preview.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Первый въезд"
        sheet.append(["Отчёт по времени первого въезда"])
        sheet.append(["Тестовая строка заголовка"])
        sheet.append(["№", "Номерной знак", "Дата въезда", "Время въезда"])
        sheet.append([1, "TEST", datetime(2026, 7, 28, 0, 0, 0), time(7, 2, 28)])
        sheet["C4"].number_format = "dd.mm.yyyy"
        sheet["D4"].number_format = "hh:mm:ss"
        workbook.save(path)
        workbook.close()

        columns, rows, total = implementation.workbook_preview(path)
        assert columns[:4] == ["№", "Номерной знак", "Дата въезда", "Время въезда"]
        assert total == 1
        assert rows[0][2] == "28.07.2026", rows[0][2]
        assert rows[0][3] == "07:02:28", rows[0][3]

    print("OK: в веб-превью КПП дата въезда отображается без времени.")


if __name__ == "__main__":
    main()
