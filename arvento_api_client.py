#!/usr/bin/env python3
from __future__ import annotations

import time
from datetime import datetime

import requests

ENDPOINT = "https://ws.arvento.com/v1/report.asmx/GeneralReportWithDistance"
DATE_FORMAT = "%Y%m%d%H%M%S"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/xml,application/xml,*/*",
    "Referer": "https://ws.arvento.com/v1/report.asmx?op=GeneralReportWithDistance",
    "Origin": "https://ws.arvento.com",
}


def build_general_report_params(
    username: str,
    pin1: str,
    pin2: str,
    group: str,
    node: str,
    start_dt: datetime,
    end_dt: datetime,
    minute_dif: int,
    include_regions: bool = True,
) -> dict[str, str]:
    """Build request parameters for GeneralReportWithDistance."""
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
        "chkRegion": "1" if include_regions else "",
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


def fetch_general_report_chunk(
    session: requests.Session,
    params: dict[str, str],
    timeout: int,
    retries: int,
) -> requests.Response:
    """Fetch one GeneralReportWithDistance time chunk with retries."""
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
