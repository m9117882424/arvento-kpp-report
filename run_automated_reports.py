#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Единый запуск: загрузка Arvento API и построение отчётов.

Примеры:
    python run_automated_reports.py --date 2026-07-24 --left-turn

    python run_automated_reports.py --date 2026-07-24 --first-entry \
        --roster "20.07.2026 SON GÜNCEL KİRALIK ARAÇ LİSTESİ.xlsx" \
        --grade-from 7 --grade-to 14 --time-from 07:00 --time-to 09:00

Учётные данные берутся из ARVENTO_USER / ARVENTO_PIN1 / ARVENTO_PIN2
или запрашиваются один раз в консоли.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date
from pathlib import Path


def run(command: list[str], env: dict[str, str]) -> None:
    print("\n> " + subprocess.list2cmdline(command))
    subprocess.run(command, check=True, env=env)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Автоматическая загрузка Arvento API и построение отчётов"
    )
    parser.add_argument("--date", default=date.today().isoformat(), help="YYYY-MM-DD")
    parser.add_argument("--group", default=os.environ.get("ARVENTO_GROUP", "TSM"))
    parser.add_argument("--node", default=os.environ.get("ARVENTO_NODE", ""))
    parser.add_argument("--output-dir", type=Path, default=Path("arvento_api_data"))
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    parser.add_argument("--chunk-minutes", type=int, default=120)
    parser.add_argument("--passes", type=int, default=1)
    parser.add_argument("--interval", type=int, default=0)
    parser.add_argument("--minute-dif", type=int, default=180)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--retries", type=int, default=3)

    report_group = parser.add_argument_group("Отчёты")
    report_group.add_argument("--left-turn", action="store_true", help="Запрещённый поворот")
    report_group.add_argument("--first-entry", action="store_true", help="Первый въезд")
    report_group.add_argument("--all", action="store_true", help="Построить оба отчёта")

    first = parser.add_argument_group("Параметры первого въезда")
    first.add_argument("--roster", type=Path, help="Файл разнарядки XLSX/XLSM")
    first.add_argument("--geo-events", type=Path)
    first.add_argument("--grade-from")
    first.add_argument("--grade-to")
    first.add_argument("--time-from")
    first.add_argument("--time-to")
    first.add_argument(
        "--consider-previous-exits",
        action=argparse.BooleanOptionalAction,
        default=False,
    )

    left = parser.add_argument_group("Параметры запрещённого поворота")
    left.add_argument("--width", type=float, default=20.0)
    left.add_argument("--max-minutes", type=float, default=3.0)
    left.add_argument("--control-minutes", type=float, default=3.0)
    left.add_argument("--cooldown-minutes", type=float, default=5.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    target_day = date.fromisoformat(args.date)
    make_left = args.left_turn or args.all
    make_first = args.first_entry or args.all
    if not make_left and not make_first:
        make_left = True

    script_dir = Path(__file__).resolve().parent
    env = os.environ.copy()

    sync_command = [
        sys.executable,
        str(script_dir / "arvento_api_sync.py"),
        "--date", target_day.isoformat(),
        "--group", args.group,
        "--node", args.node,
        "--chunk-minutes", str(args.chunk_minutes),
        "--passes", str(args.passes),
        "--interval", str(args.interval),
        "--minute-dif", str(args.minute_dif),
        "--timeout", str(args.timeout),
        "--retries", str(args.retries),
        "--output-dir", str(args.output_dir),
    ]
    run(sync_command, env)

    csv_path = (
        args.output_dir
        / target_day.isoformat()
        / f"arvento_{target_day.isoformat()}.csv"
    ).resolve()
    if not csv_path.exists():
        raise SystemExit(f"После синхронизации не найден CSV: {csv_path}")

    reports_day_dir = (args.reports_dir / target_day.isoformat()).resolve()
    reports_day_dir.mkdir(parents=True, exist_ok=True)

    if make_left:
        output = reports_day_dir / f"Запрещенный_поворот_{target_day.isoformat()}.xlsx"
        run(
            [
                sys.executable,
                str(script_dir / "prohibited_left_turn_report.py"),
                str(csv_path),
                str(output),
                "--width", str(args.width),
                "--max-minutes", str(args.max_minutes),
                "--control-minutes", str(args.control_minutes),
                "--cooldown-minutes", str(args.cooldown_minutes),
            ],
            env,
        )

    if make_first:
        if args.roster is None:
            raise SystemExit("Для отчёта первого въезда укажите --roster ПУТЬ_К_РАЗНАРЯДКЕ")
        roster = args.roster.expanduser().resolve()
        if not roster.exists():
            raise SystemExit(f"Разнарядка не найдена: {roster}")
        output = reports_day_dir / f"Первый_въезд_{target_day.isoformat()}.xlsx"
        command = [
            sys.executable,
            str(script_dir / "arvento_first_entry_report_fixed.py"),
            str(csv_path),
            str(roster),
            str(output),
            "--no-filter-dialog",
        ]
        if args.geo_events:
            command += ["--geo-events", str(args.geo_events.expanduser().resolve())]
        if args.grade_from is not None:
            command += ["--grade-from", args.grade_from]
        if args.grade_to is not None:
            command += ["--grade-to", args.grade_to]
        if args.time_from is not None:
            command += ["--time-from", args.time_from]
        if args.time_to is not None:
            command += ["--time-to", args.time_to]
        command.append(
            "--consider-previous-exits"
            if args.consider_previous_exits
            else "--no-consider-previous-exits"
        )
        run(command, env)

    print("\nАвтоматический цикл завершён.")
    print(f"Данные: {csv_path}")
    print(f"Отчёты: {reports_day_dir}")


if __name__ == "__main__":
    main()
