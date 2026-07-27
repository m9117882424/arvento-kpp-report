#!/usr/bin/env python3
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime


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
    region_name: str


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def text_of(element: ET.Element, *names: str) -> str:
    wanted = set(names)
    for child in element:
        if local_name(child.tag) in wanted:
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
        return datetime.fromisoformat(value).replace(tzinfo=None)
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
    root = ET.fromstring(extract_xml(response_text))
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
                region_name=text_of(
                    element,
                    "Geographic_x0020_Region",
                    "Geographical_x0020_Region",
                    "GeographicRegion",
                    "Region",
                    "Co_x011F_rafi_x0020_Bölge",
                ),
            )
        )
    return rows
