#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Инкрементальная загрузка GeneralReportWithDistance из Arvento API.

Сутки загружаются небольшими временными чанками. Повторные проходы повторно
запрашивают тот же период и добавляют только новые сообщения, которые могли
позже поступить от трекеров. Итоговый CSV совместим с существующими отчётами
проекта (prohibited_left_turn_report.py и другими скриптами на sqlite_store.py).

Пример:
    python arvento_api_sync.py --date 2026-07-24 --group TSM --passes 3 --interval 1800

Учётные данные читаются из ARVENTO_USER / ARVENTO_PIN1 / ARVENTO_PIN2 либо
запрашиваются в консоли. В файлы и журнал они не записываются.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import os
import sqlite3
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

ENDPOINT = "https://ws.arvento.com/v1/report.asmx/GeneralReportWithDistance"
DATE_FORMAT = "%Y%m%d%H%M%S"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/xml,application/xml,*/*",
    "Referer": "https://ws.arvento.com/v1/report.asmx?op=GeneralReportWithDistance",
    "Origin": "https://ws.arvento.com",
}

CSV_HEADERS = [
    "License Plate",
    "Date / Time",
    "Latitude",
    "Longitude",
    "Speed",
    "Distance",
    "Address",
    "Type",
    "Device No",
    "Driver",
    "Pause Duration",
    "Idling Duration",
    "Ignition On Duration",
]


@dataclass(frozen=True)
class ApiRow:
    plate: str
    timestamp: datetime
    latitude: float
    longitude: float
    speed: float | None
    distance: float | None
    address: str
    event_type: str
    device_no: str
    driver: str
    pause_duration: str
    idling_duration: str
    ignition_duration: str


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def text_of(element: ET.Element, name: str) -> str:
    for child in element:
        if local_name(child.tag) == name:
            return (child.text or "").strip()
    return ""


def parse_float(value: str) -> float | None:
    value = value.strip().replace(" ", "").replace(",", ".")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_datetime(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=None)
    except ValueError:
        return None


def extract_xml(response_text: str) -> str:
    start = response_text.find("<?xml")
    if start < 0:
        start = response_text.find("<DataSet")
    if start < 0:
        raise ValueError("Ответ не содержит XML DataSet")
    return response_text[start:]


def parse_rows(response_text: str) -> list[ApiRow]:
    xml_text = extract_xml(response_text)
    root = ET.fromstring(xml_text)
    rows: list[ApiRow] = []

    for element in root.iter():
        if local_name(element.tag) != "General_x0020_Report":
            continue

        plate = text_of(element, "License_x0020_Plate")
        timestamp = parse_datetime(text_of(element, "Date_x002F_Time"))
        latitude = parse_float(text_of(element, "Latitude"))
        longitude = parse_float(text_of(element, "Longitude"))
        if not plate or timestamp is None or latitude is None or longitude is None:
            continue

        rows.append(
            ApiRow(
                plate=plate,
                timestamp=timestamp,
                latitude=latitude,
                longitude=longitude,
                speed=parse_float(text_of(element, "Speed_x0020_km_x002F_h")),
                distance=parse_float(text_of(element, "Distance")),
                address=text_of(element, "Address"),
                event_type=text_of(element, "Type"),
                device_no=text_of(element, "Device_x0020_No"),
                driver=text_of(element, "Driver"),
                pause_duration=text_of(element, "Pause_x0020_Duration"),
                idling_duration=text_of(element, "Idling_x0020_Duration"),
                ignition_duration=text_of(element, "Ignition_x0020_On_x0020_Duration"),
            )
        )
    return rows


def build_params(
    username: str,
    pin1: str,
    pin2: str,
    group: str,
    node: str,
    start_dt: datetime,
    end_dt: datetime,
    minute_dif: int,
) -> dict[str, str]:
    return {
        "Username": username,
        "PIN1": pin1,
        "PIN2": pin2,
        "StartDate": start_dt.strftime(DATE_FORMAT),
        "EndDate": end_dt.strftime(DATE_FORMAT),
        "Node": node,
        "Group": group,
        "Compress": "",
        "chkLocation": "1",
        "chkSpeed": "",
        "chkPause": "",
        "chkMotion": "",
        "chkRegion": "",
        "txtSpeedMin": "",
        "txtSpeedMax": "",
        "chkTemperatureSensor1": "",
        "chkTemperatureSensorPer1": "",
        "chkTemperatureSensorAlm1": "",
        "chkTemperatureSensor2": "",
        "chkTemperatureSensorPer2": "",
        "chkTemperatureSensorAlm2": "",
        "chkTemperatureSensor3": "",
        "chkTemperatureSensorPer3": "",
        "chkTemperatureSensorAlm3": "",
        "chkTemperatureSensor4": "",
        "chkTemperatureSensorPer4": "",
        "chkTemperatureSensorAlm4": "",
        "txtTemperatureMin": "",
        "txtTemperatureMax": "",
        "chkEmergency": "",
        "chkDoor": "",
        "chkPauseTime": "",
        "chkContactAlarm": "1",
        "chkIdlingTime": "1",
        "chkIdlingAlarm": "",
        "chkFuelLevel": "",
        "chkPower": "",
        "chkDriverIdentification": "",
        "chkHumiditySensor1": "",
        "chkHumiditySensor2": "",
        "chkHumiditySensor3": "",
        "chkHumiditySensor4": "",
        "chkPossibleAccident": "",
        "chkAcceleration": "",
        "chkVehicleMovedWithoutDriverCard": "",
        "MinuteDif": str(minute_dif),
        "Language": "1",
    }


def create_cache(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS api_points (
            plate TEXT NOT NULL,
            ts TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            speed REAL,
            distance REAL,
            address TEXT,
            event_type TEXT,
            device_no TEXT,
            driver TEXT,
            pause_duration TEXT,
            idling_duration TEXT,
            ignition_duration TEXT,
            UNIQUE(device_no, plate, ts, latitude, longitude, event_type)
        )
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_api_points_ts_plate ON api_points(ts, plate)")
    connection.commit()
    return connection


def insert_rows(connection: sqlite3.Connection, rows: list[ApiRow]) -> int:
    before = connection.total_changes
    connection.executemany(
        """
        INSERT OR IGNORE INTO api_points VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                row.plate,
                row.timestamp.isoformat(sep=" "),
                row.latitude,
                row.longitude,
                row.speed,
                row.distance,
                row.address,
                row.event_type,
                row.device_no,
                row.driver,
                row.pause_duration,
                row.idling_duration,
                row.ignition_duration,
            )
            for row in rows
        ],
    )
    connection.commit()
    return connection.total_changes - before


def export_csv(connection: sqlite3.Connection, target: Path, day: date) -> int:
    start = datetime.combine(day, datetime.min.time()).isoformat(sep=" ")
    end = datetime.combine(day + timedelta(days=1), datetime.min.time()).isoformat(sep=" ")
    rows = connection.execute(
        """
        SELECT plate, ts, latitude, longitude, speed, distance, address, event_type,
               device_no, driver, pause_duration, idling_duration, ignition_duration
        FROM api_points
        WHERE ts >= ? AND ts < ?
        ORDER BY plate, ts
        """,
        (start, end),
    ).fetchall()

    with target.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(CSV_HEADERS)
        writer.writerows(rows)
    return len(rows)


def fetch_chunk(
    session: requests.Session,
    params: dict[str, str],
    timeout: int,
    retries: int,
) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = session.post(ENDPOINT, data=params, timeout=timeout)
            response.raise_for_status()
            if "<Error>" in response.text or "<e>" in response.text:
                raise RuntimeError(response.text[:500])
            return response
        except (requests.RequestException, RuntimeError) as error:
            last_error = error
            if attempt < retries:
                time.sleep(min(10 * attempt, 30))
    raise RuntimeError(f"Не удалось загрузить чанк после {retries} попыток: {last_error}")


def sync_pass(
    connection: sqlite3.Connection,
    session: requests.Session,
    username: str,
    pin1: str,
    pin2: str,
    group: str,
    node: str,
    day: date,
    chunk_minutes: int,
    minute_dif: int,
    timeout: int,
    retries: int,
    raw_dir: Path,
    pass_number: int,
) -> tuple[int, int, int]:
    day_start = datetime.combine(day, datetime.min.time())
    day_end = day_start + timedelta(days=1)
    chunk_start = day_start
    chunk_count = 0
    received = 0
    inserted = 0

    while chunk_start < day_end:
        chunk_count += 1
        chunk_end = min(chunk_start + timedelta(minutes=chunk_minutes), day_end)
        label = f"{chunk_start:%H:%M}-{chunk_end:%H:%M}"
        print(f"  [{chunk_count:02d}] {label} ...", end=" ", flush=True)

        params = build_params(
            username, pin1, pin2, group, node, chunk_start, chunk_end, minute_dif
        )
        response = fetch_chunk(session, params, timeout, retries)
        rows = parse_rows(response.text)
        new_count = insert_rows(connection, rows)
        received += len(rows)
        inserted += new_count

        raw_path = raw_dir / (
            f"pass_{pass_number:02d}_{chunk_start:%H%M}_{chunk_end:%H%M}.xml"
        )
        raw_path.write_text(extract_xml(response.text), encoding="utf-8")
        print(f"получено {len(rows)}, новых {new_count}")
        chunk_start = chunk_end

    return chunk_count, received, inserted


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Инкрементальная загрузка Arvento API")
    parser.add_argument("--date", required=True, help="Дата отчёта: YYYY-MM-DD")
    parser.add_argument("--group", default=os.environ.get("ARVENTO_GROUP", "TSM"))
    parser.add_argument("--node", default=os.environ.get("ARVENTO_NODE", ""))
    parser.add_argument("--chunk-minutes", type=int, default=120)
    parser.add_argument("--passes", type=int, default=1, help="Количество повторных загрузок")
    parser.add_argument("--interval", type=int, default=0, help="Пауза между проходами, секунд")
    parser.add_argument("--minute-dif", type=int, default=180)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--output-dir", type=Path, default=Path("arvento_api_data"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    target_day = date.fromisoformat(args.date)
    if args.chunk_minutes <= 0 or args.passes <= 0:
        raise SystemExit("chunk-minutes и passes должны быть больше нуля")

    username = os.environ.get("ARVENTO_USER") or input("Username: ").strip()
    pin1 = os.environ.get("ARVENTO_PIN1") or getpass.getpass("PIN1: ")
    pin2 = os.environ.get("ARVENTO_PIN2") or getpass.getpass("PIN2: ")

    day_dir = args.output_dir / target_day.isoformat()
    raw_dir = day_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    cache_path = day_dir / "arvento_cache.sqlite"
    csv_path = day_dir / f"arvento_{target_day.isoformat()}.csv"

    connection = create_cache(cache_path)
    session = requests.Session()
    session.headers.update(HEADERS)

    try:
        for pass_number in range(1, args.passes + 1):
            print(f"\nПроход {pass_number}/{args.passes}: {target_day}, Group={args.group}")
            chunks, received, inserted = sync_pass(
                connection=connection,
                session=session,
                username=username,
                pin1=pin1,
                pin2=pin2,
                group=args.group,
                node=args.node,
                day=target_day,
                chunk_minutes=args.chunk_minutes,
                minute_dif=args.minute_dif,
                timeout=args.timeout,
                retries=args.retries,
                raw_dir=raw_dir,
                pass_number=pass_number,
            )
            total = export_csv(connection, csv_path, target_day)
            print(
                f"Проход завершён: чанков {chunks}, получено {received}, "
                f"добавлено {inserted}, всего в CSV {total}"
            )
            print(f"Итоговый файл: {csv_path.resolve()}")

            if pass_number < args.passes and args.interval > 0:
                print(f"Ожидание {args.interval} сек. перед повторной загрузкой...")
                time.sleep(args.interval)
    finally:
        session.close()
        connection.close()


if __name__ == "__main__":
    main()
