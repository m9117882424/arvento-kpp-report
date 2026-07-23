#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from pathlib import Path
from tkinter import Tk, filedialog

from arvento_analysis import analyze_by_day
from arvento_io import load_points
from arvento_reports import save_daily_book, save_summary_book
from geozone_registry import load_registry


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

    print(f"Чтение: {source}")
    points, stats = load_points(source)
    registry = load_registry(config)
    daily, stops = analyze_by_day(points, registry)

    daily_path = source.with_name(source.stem + "_по_дням.xlsx")
    summary_path = source.with_name(source.stem + "_итоговая_сводка.xlsx")
    save_daily_book(daily_path, daily)
    save_summary_book(summary_path, daily, stops)

    print(f"Готово: {daily_path}")
    print(f"Готово: {summary_path}")
    print(f"Дат: {len(daily)}; точек: {stats['loaded']}; пропущено: {stats['skipped']}")


if __name__ == "__main__":
    main()
