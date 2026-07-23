#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from tkinter import Tk, filedialog

from arvento_reports import save_daily_book, save_summary_book
from geozone_registry import load_registry
from sqlite_analysis import analyze_sqlite
from sqlite_store import import_source_to_sqlite


def choose_file() -> Path:
    import sys

    if len(sys.argv) > 1:
        return Path(sys.argv[1]).expanduser().resolve()

    root = Tk()
    root.withdraw()
    selected = filedialog.askopenfilename(
        title="Выберите выгрузку Arvento",
        filetypes=[("Excel / CSV", "*.xlsx *.xlsm *.csv"), ("Все файлы", "*.*")],
    )
    root.destroy()
    if not selected:
        raise SystemExit("Файл не выбран")
    return Path(selected).resolve()


def main() -> None:
    source = choose_file()
    if not source.exists():
        raise SystemExit(f"Файл не найден: {source}")

    config = source.parent / "geozones.json"
    if not config.exists():
        config = Path(__file__).resolve().parent / "geozones.json"

    registry = load_registry(config)
    temp_dir = Path(tempfile.mkdtemp(prefix="arvento_kpp_"))
    db_path = temp_dir / "arvento_points.sqlite3"

    try:
        print(f"Исходный файл: {source}")
        print(f"Временная SQLite-база: {db_path}")
        print("Импорт данных в SQLite...")
        stats = import_source_to_sqlite(source, db_path)

        print("Анализ данных по датам и автомобилям...")
        daily, stops = analyze_sqlite(db_path, registry)

        daily_path = source.with_name(source.stem + "_по_дням.xlsx")
        summary_path = source.with_name(source.stem + "_итоговая_сводка.xlsx")

        print("Формирование дневной книги...")
        save_daily_book(daily_path, daily)
        print("Формирование итоговой сводки...")
        save_summary_book(summary_path, daily, stops)

        print(f"Готово: {daily_path}")
        print(f"Готово: {summary_path}")
        print(
            f"Дат: {len(daily)}; "
            f"точек: {stats['loaded']}; "
            f"пропущено: {stats['skipped']}"
        )
    finally:
        print("Удаление временной SQLite-базы...")
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
