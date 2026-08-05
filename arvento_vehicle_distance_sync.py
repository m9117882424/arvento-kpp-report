#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Load authoritative vehicle-day mileage from VehicleDistanceReport."""
from __future__ import annotations

import argparse
import math
import os
import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import date, datetime, time
from typing import Any

import psycopg
import requests


ENDPOINT = "https://ws.arvento.com/v1/report.asmx/VehicleDistanceReport"


def normalize_key(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(
        character
        for character in text
        if not unicodedata.combining(character)
    )
    text = text.casefold().replace("ı", "i")
    return re.sub(r"[^a-z0-9]+", "", text)


def normalize_plate(value: Any) -> str:
    return "".join(
        character
        for character in str(value or "").upper()
        if character.isalnum()
    )


def decode_xml_name(value: str) -> str:
    value = value.rsplit("}", 1)[-1]

    def replace(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 16))

    return re.sub(r"_x([0-9A-Fa-f]{4})_", replace, value)


def parse_float(value: Any) -> float | None:
    text = str(value or "").strip().replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        result = float(text)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def value(row: dict[str, str], *candidates: str) -> str:
    for candidate in candidates:
        result = row.get(normalize_key(candidate))
        if result is not None:
            return result
    return ""


def parse_rows(content: bytes) -> list[dict[str, Any]]:
    root = ET.fromstring(content)
    result: list[dict[str, Any]] = []

    for element in root.iter():
        children = list(element)
        if not children or not all(not list(child) for child in children):
            continue

        row = {
            normalize_key(decode_xml_name(child.tag)): (child.text or "").strip()
            for child in children
        }
        plate_text = value(row, "License Plate", "License_Plate", "Plaka")
        normalized_plate = normalize_plate(plate_text)
        distance = parse_float(value(row, "Distance km", "Distance_km"))
        if not normalized_plate or distance is None or distance < 0:
            continue

        result.append(
            {
                "device_no": value(row, "Device", "Device No", "Device_No"),
                "plate": plate_text,
                "normalized_plate": normalized_plate,
                "distance_km": distance,
                "initial_odometer_km": parse_float(
                    value(row, "Initial Odometer Value km")
                ),
                "initial_odometer_time": parse_datetime(
                    value(row, "Initial Odometer Time")
                ),
                "last_odometer_km": parse_float(
                    value(row, "Last Odometer Value km")
                ),
                "last_odometer_time": parse_datetime(
                    value(row, "Last Odometer Time")
                ),
                "driver": value(row, "Driver"),
            }
        )

    return result


def ensure_schema(connection: psycopg.Connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS vehicle_distance_daily (
                report_day DATE NOT NULL,
                device_no TEXT,
                plate TEXT NOT NULL,
                normalized_plate TEXT NOT NULL,
                distance_km DOUBLE PRECISION NOT NULL,
                initial_odometer_km DOUBLE PRECISION,
                initial_odometer_time TIMESTAMPTZ,
                last_odometer_km DOUBLE PRECISION,
                last_odometer_time TIMESTAMPTZ,
                driver TEXT,
                source TEXT NOT NULL DEFAULT 'VehicleDistanceReport',
                fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (report_day, normalized_plate)
            )
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_vehicle_distance_daily_plate_day
            ON vehicle_distance_daily (normalized_plate, report_day)
            """
        )


def fetch_day(session: requests.Session, report_day: date) -> list[dict[str, Any]]:
    start = datetime.combine(report_day, time.min)
    finish = datetime.combine(report_day, time(23, 59, 59))
    payload = {
        "Username": os.environ["ARVENTO_USER"],
        "PIN1": os.environ["ARVENTO_PIN1"],
        "PIN2": os.environ.get("ARVENTO_PIN2", ""),
        "StartDate": start.strftime("%m%d%Y%H%M%S"),
        "EndDate": finish.strftime("%m%d%Y%H%M%S"),
        "Node": "",
        "Group": os.environ.get("ARVENTO_GROUP", "TSM"),
        "Compress": "",
        "Locale": "tr",
        "Language": "1",
    }
    response = session.post(
        ENDPOINT,
        data=payload,
        timeout=int(os.environ.get("ARVENTO_HTTP_TIMEOUT", "300")),
    )
    response.raise_for_status()

    lowered = response.text.casefold()
    if "you are not authorized" in lowered or "yetkiniz yoktur" in lowered:
        raise PermissionError("Нет доступа к VehicleDistanceReport")

    rows = parse_rows(response.content)
    if not rows:
        raise RuntimeError("VehicleDistanceReport вернул пустой или нераспознанный ответ")
    return rows


def upsert_day(
    connection: psycopg.Connection,
    report_day: date,
    rows: list[dict[str, Any]],
) -> tuple[int, int]:
    written = 0
    queued = 0

    with connection.cursor() as cursor:
        for row in rows:
            cursor.execute(
                """
                INSERT INTO vehicle_distance_daily (
                    report_day, device_no, plate, normalized_plate,
                    distance_km, initial_odometer_km, initial_odometer_time,
                    last_odometer_km, last_odometer_time, driver, fetched_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                ON CONFLICT (report_day, normalized_plate)
                DO UPDATE SET
                    device_no=EXCLUDED.device_no,
                    plate=EXCLUDED.plate,
                    distance_km=EXCLUDED.distance_km,
                    initial_odometer_km=EXCLUDED.initial_odometer_km,
                    initial_odometer_time=EXCLUDED.initial_odometer_time,
                    last_odometer_km=EXCLUDED.last_odometer_km,
                    last_odometer_time=EXCLUDED.last_odometer_time,
                    driver=EXCLUDED.driver,
                    fetched_at=now()
                WHERE vehicle_distance_daily.device_no IS DISTINCT FROM EXCLUDED.device_no
                   OR vehicle_distance_daily.plate IS DISTINCT FROM EXCLUDED.plate
                   OR vehicle_distance_daily.distance_km IS DISTINCT FROM EXCLUDED.distance_km
                   OR vehicle_distance_daily.initial_odometer_km IS DISTINCT FROM EXCLUDED.initial_odometer_km
                   OR vehicle_distance_daily.initial_odometer_time IS DISTINCT FROM EXCLUDED.initial_odometer_time
                   OR vehicle_distance_daily.last_odometer_km IS DISTINCT FROM EXCLUDED.last_odometer_km
                   OR vehicle_distance_daily.last_odometer_time IS DISTINCT FROM EXCLUDED.last_odometer_time
                   OR vehicle_distance_daily.driver IS DISTINCT FROM EXCLUDED.driver
                RETURNING normalized_plate
                """,
                (
                    report_day,
                    row["device_no"],
                    row["plate"],
                    row["normalized_plate"],
                    row["distance_km"],
                    row["initial_odometer_km"],
                    row["initial_odometer_time"],
                    row["last_odometer_km"],
                    row["last_odometer_time"],
                    row["driver"],
                ),
            )
            changed = cursor.fetchone()
            if changed is None:
                continue

            written += 1
            cursor.execute(
                """
                INSERT INTO recalculation_queue (normalized_plate, day, reason)
                VALUES (%s,%s,'vehicle_distance_report')
                ON CONFLICT (normalized_plate, day)
                DO UPDATE SET
                    reason=EXCLUDED.reason,
                    created_at=now(),
                    completed_at=NULL
                """,
                (row["normalized_plate"], report_day),
            )
            queued += 1

    return written, queued


def sync_day(report_day: date) -> dict[str, Any]:
    with requests.Session() as session:
        rows = fetch_day(session, report_day)

    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        ensure_schema(connection)
        connection.commit()
        written, queued = upsert_day(connection, report_day, rows)
        connection.commit()

    result = {
        "status": "SUCCESS",
        "day": report_day.isoformat(),
        "rows_received": len(rows),
        "rows_written": written,
        "queue_updated": queued,
        "total_km": round(sum(float(row["distance_km"]) for row in rows), 2),
    }
    print(result, flush=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Загрузка суточного пробега VehicleDistanceReport"
    )
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    args = parser.parse_args()
    sync_day(args.date)


if __name__ == "__main__":
    main()
