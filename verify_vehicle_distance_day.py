#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only verification of hybrid mileage selection for one report day."""
from __future__ import annotations

import argparse
import os
from collections import Counter
from datetime import date

import consolidated_report as core
import consolidated_mileage_logic as mileage


CONTROL_PLATES = {
    "34PAH724",
    "34PKY310",
    "34PKY311",
    "34GPS131",
    "34KDE822",
}


def normalize_plate(value: object) -> str:
    return "".join(
        character
        for character in str(value or "").upper()
        if character.isalnum()
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    args = parser.parse_args()

    database_url = os.environ["DATABASE_URL"]
    authoritative = mileage.load_authoritative_daily_distances(
        database_url,
        args.date,
        args.date,
    )

    rows: list[dict[str, object]] = []

    for report_day, display_plate, points in mileage._ORIGINAL_ITER_DATABASE_TRACKS(
        database_url,
        args.date,
        args.date,
    ):
        plate = normalize_plate(display_plate)
        track = core.sanitize_position_outliers(points)
        authoritative_km = authoritative.get((report_day, plate))
        coordinate_km = sum(mileage.coordinate_segment_distances(track))
        mode = mileage.normalize_vehicle_day_distances(track, authoritative_km)
        selected_km = sum(
            mileage.segment_distance_prefer_arvento(previous, current)
            for previous, current in zip(track, track[1:])
        )

        rows.append(
            {
                "plate": plate,
                "authoritative": authoritative_km,
                "coordinate": coordinate_km,
                "selected": selected_km,
                "mode": mode,
                "points": len(track),
            }
        )

    mode_counts = Counter(str(row["mode"]) for row in rows)
    authoritative_rows = [row for row in rows if row["authoritative"] is not None]
    fallback_rows = [row for row in rows if row["mode"] == "coordinate_fallback"]
    missing_rows = [row for row in rows if row["authoritative"] is None]

    print("========== ИТОГ ==========")
    print(f"GPS автомобилей:           {len(rows)}")
    print(f"С VehicleDistanceReport:   {len(authoritative_rows)}")
    print(f"Без VehicleDistanceReport: {len(missing_rows)}")
    print(f"Координатных fallback:     {len(fallback_rows)}")

    print("\nРежимы:")
    for mode, count in sorted(mode_counts.items()):
        print(f"  {mode:<36} {count}")

    authoritative_total = sum(float(row["authoritative"]) for row in authoritative_rows)
    selected_authoritative_total = sum(float(row["selected"]) for row in authoritative_rows)
    selected_all_total = sum(float(row["selected"]) for row in rows)

    print("\n========== СУММЫ ==========")
    print(f"VehicleDistanceReport:            {authoritative_total:.2f} км")
    print(f"Гибрид по тем же автомобилям:     {selected_authoritative_total:.2f} км")
    print(f"Гибрид со всеми GPS автомобилями: {selected_all_total:.2f} км")

    print("\n========== КООРДИНАТНЫЙ FALLBACK ==========")
    print(f"{'Госномер':<12}{'Одометр':>12}{'Координаты':>14}{'Выбрано':>12}{'Разница':>12}")
    for row in sorted(
        fallback_rows,
        key=lambda item: float(item["authoritative"]) - float(item["coordinate"]),
        reverse=True,
    ):
        authoritative_km = float(row["authoritative"])
        coordinate_km = float(row["coordinate"])
        selected_km = float(row["selected"])
        print(
            f"{row['plate']:<12}"
            f"{authoritative_km:>12.2f}"
            f"{coordinate_km:>14.2f}"
            f"{selected_km:>12.2f}"
            f"{authoritative_km - coordinate_km:>12.2f}"
        )

    print("\n========== КОНТРОЛЬНЫЕ АВТОМОБИЛИ ==========")
    for row in sorted(
        (item for item in rows if item["plate"] in CONTROL_PLATES),
        key=lambda item: str(item["plate"]),
    ):
        authoritative_value = row["authoritative"]
        authoritative_text = (
            f"{float(authoritative_value):.3f}"
            if authoritative_value is not None
            else "нет"
        )
        print(
            row["plate"],
            f"authoritative={authoritative_text}",
            f"coordinate={float(row['coordinate']):.3f}",
            f"selected={float(row['selected']):.3f}",
            f"mode={row['mode']}",
            f"points={row['points']}",
        )


if __name__ == "__main__":
    main()
