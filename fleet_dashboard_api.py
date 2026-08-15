#!/usr/bin/env python3
"""Read-only JSON API for the passenger-fleet dashboard.

The API joins prepared Arvento daily rows with Fuel Monitor transactions by
calendar day and normalized plate. It deliberately avoids recalculating GPS
tracks or generating Excel workbooks on dashboard requests.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

import psycopg
import psycopg.rows
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response

from roster_registry import normalize_plate


TZ = ZoneInfo("Europe/Istanbul")
LOGGER = logging.getLogger(__name__)
DEFAULT_MAX_PERIOD_DAYS = 93
DEFAULT_OFFLINE_MINUTES = 180
DEFAULT_MOVING_SPEED_KMH = 3.0

DatabaseUrlFactory = Callable[[], str]


def _integer_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(max(value, minimum), maximum)


def _float_setting(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(max(value, minimum), maximum)


def _statement_options() -> str:
    timeout = _integer_setting("FLEET_API_STATEMENT_TIMEOUT_MS", 15_000, 1_000, 120_000)
    return f"-c statement_timeout={timeout}"


def validate_period(start_day: date, end_day: date) -> int:
    """Validate an inclusive dashboard period and return its number of days."""
    if end_day < start_day:
        raise ValueError("date_to не может быть раньше date_from")
    days = (end_day - start_day).days + 1
    maximum = _integer_setting(
        "FLEET_API_MAX_PERIOD_DAYS",
        DEFAULT_MAX_PERIOD_DAYS,
        1,
        366,
    )
    if days > maximum:
        raise ValueError(f"Период ограничен {maximum} календарными днями")
    return days


def authorization_matches(authorization: str | None, expected_token: str) -> bool:
    """Compare a Bearer header without leaking token timing information."""
    if not authorization or not expected_token:
        return False
    scheme, separator, supplied = authorization.partition(" ")
    return (
        bool(separator)
        and scheme.lower() == "bearer"
        and bool(supplied)
        and secrets.compare_digest(supplied, expected_token)
    )


def authorization_matches_sha256(
    authorization: str | None,
    expected_sha256: str,
) -> bool:
    """Compare a Bearer token with a configured SHA-256 digest."""
    if not authorization:
        return False
    scheme, separator, supplied = authorization.partition(" ")
    digest = expected_sha256.strip().lower()
    valid_digest = len(digest) == 64 and all(
        character in "0123456789abcdef" for character in digest
    )
    if not separator or scheme.lower() != "bearer" or not supplied or not valid_digest:
        return False
    supplied_digest = hashlib.sha256(supplied.encode("utf-8")).hexdigest()
    return secrets.compare_digest(supplied_digest, digest)


def require_fleet_token(authorization: str | None = Header(default=None)) -> None:
    expected = os.environ.get("FLEET_API_TOKEN", "").strip()
    expected_sha256 = os.environ.get("FLEET_API_TOKEN_SHA256", "").strip()
    if not expected and not expected_sha256:
        raise HTTPException(status_code=503, detail="Fleet API is not configured")
    if not (
        authorization_matches(authorization, expected)
        or authorization_matches_sha256(authorization, expected_sha256)
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid fleet API token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def prevent_fleet_caching(response: Response) -> None:
    """Keep live vehicle positions out of browser and intermediary caches."""
    response.headers["Cache-Control"] = "private, no-store"


def _as_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _rounded(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(float(value), digits)


def _iso(value: Any) -> str | None:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return None if value is None else str(value)


def _day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value:
        try:
            return date.fromisoformat(str(value))
        except ValueError:
            return None
    return None


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=TZ)
    return value


def classify_vehicle_status(
    event_time: datetime | None,
    speed_kmh: float | None,
    now: datetime,
    *,
    offline_minutes: int,
    moving_speed_kmh: float,
) -> tuple[str, float | None]:
    """Return moving/parked/offline and the non-negative data age."""
    event_time = _aware(event_time)
    now = _aware(now) or datetime.now(TZ)
    if event_time is None:
        return "offline", None
    age_minutes = max(0.0, (now - event_time).total_seconds() / 60.0)
    if age_minutes > offline_minutes:
        return "offline", age_minutes
    if _as_float(speed_kmh) >= moving_speed_kmh:
        return "moving", age_minutes
    return "parked", age_minutes


def load_arvento_snapshot(
    database_url: str,
    start_day: date,
    end_day: date,
) -> dict[str, Any]:
    """Load prepared daily metrics and a bounded latest-position snapshot."""
    with psycopg.connect(
        database_url,
        row_factory=psycopg.rows.dict_row,
        connect_timeout=10,
        options=_statement_options(),
    ) as connection:
        daily_rows = list(
            connection.execute(
                """
                SELECT
                    report_day, normalized_plate, plate, company, user_name, grade,
                    max_speed,
                    total_km, inside_km, outside_km, worked_hours,
                    boundary_violation, personal_use, weekend_work, night_work,
                    fuel_liters
                FROM consolidated_report_cache
                WHERE report_day BETWEEN %s AND %s
                ORDER BY report_day, normalized_plate
                """,
                (start_day, end_day),
            ).fetchall()
        )
        cache_row = connection.execute(
            """
            SELECT
                MAX(refreshed_at) AS refreshed_at,
                COUNT(*) FILTER (WHERE status = 'SUCCESS') AS success_days
            FROM consolidated_cache_days
            WHERE report_day BETWEEN %s AND %s
            """,
            (start_day, end_day),
        ).fetchone()
        current_rows = list(
            connection.execute(
                """
                WITH active_vehicles AS (
                    SELECT DISTINCT ON (normalized_plate)
                        normalized_plate, plate, driver, group_name, last_seen_at,
                        updated_at, id
                    FROM vehicles
                    WHERE is_active
                      AND normalized_plate <> ''
                    ORDER BY normalized_plate, updated_at DESC, id DESC
                ), latest AS (
                    SELECT DISTINCT ON (normalized_plate)
                        normalized_plate, plate, driver, event_time,
                        speed_kmh, latitude, longitude
                    FROM gps_points
                    WHERE event_time >= now() - interval '3 days'
                    ORDER BY normalized_plate, event_time DESC
                )
                SELECT
                    v.normalized_plate,
                    COALESCE(NULLIF(latest.plate, ''), v.plate) AS plate,
                    COALESCE(NULLIF(latest.driver, ''), v.driver) AS driver,
                    v.group_name,
                    COALESCE(latest.event_time, v.last_seen_at) AS last_seen_at,
                    latest.speed_kmh,
                    latest.latitude,
                    latest.longitude
                FROM active_vehicles v
                LEFT JOIN latest USING (normalized_plate)
                ORDER BY v.normalized_plate
                """
            ).fetchall()
        )

    expected_days = (end_day - start_day).days + 1
    success_days = _as_int((cache_row or {}).get("success_days"))
    return {
        "daily_rows": daily_rows,
        "current_rows": current_rows,
        "cache_refreshed_at": (cache_row or {}).get("refreshed_at"),
        "cache_complete": success_days == expected_days,
        "cache_success_days": success_days,
        "cache_expected_days": expected_days,
    }


def load_fuel_snapshot(
    database_url: str,
    start_day: date,
    end_day: date,
) -> dict[str, Any]:
    """Aggregate Fuel Monitor transactions without exposing transaction secrets."""
    if not database_url.strip():
        return {"status": "unconfigured", "rows": [], "latest_event_at": None}
    start_at = datetime.combine(start_day, time.min)
    finish_at = datetime.combine(end_day + timedelta(days=1), time.min)
    with psycopg.connect(
        database_url,
        row_factory=psycopg.rows.dict_row,
        connect_timeout=10,
        options=_statement_options(),
    ) as connection:
        rows = list(
            connection.execute(
                """
                SELECT
                    event_dt::date AS event_day,
                    plate,
                    source,
                    COALESCE(SUM(liters), 0) AS liters,
                    COALESCE(SUM(amount_try), 0) AS amount_try,
                    COUNT(*) AS transaction_count,
                    MAX(event_dt) AS latest_event_at
                FROM public.fuel_events
                WHERE event_dt >= %s
                  AND event_dt < %s
                GROUP BY event_dt::date, plate, source
                ORDER BY event_day, plate, source
                """,
                (start_at, finish_at),
            ).fetchall()
        )
    latest = max(
        (row.get("latest_event_at") for row in rows if row.get("latest_event_at")),
        default=None,
    )
    return {"status": "ok", "rows": rows, "latest_event_at": latest}


def load_fuel_events(
    database_url: str,
    normalized: str,
    start_day: date,
    end_day: date,
    *,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Load bounded transaction detail for one normalized plate."""
    if not database_url.strip():
        return []
    start_at = datetime.combine(start_day, time.min)
    finish_at = datetime.combine(end_day + timedelta(days=1), time.min)
    with psycopg.connect(
        database_url,
        row_factory=psycopg.rows.dict_row,
        connect_timeout=10,
        options=_statement_options(),
    ) as connection:
        return list(
            connection.execute(
                """
                SELECT
                    event_dt, plate, source, fuel_type_norm,
                    liters, amount_try, station_name, station_city
                FROM public.fuel_events
                WHERE event_dt >= %s
                  AND event_dt < %s
                  AND regexp_replace(upper(plate), '[^[:alnum:]]', '', 'g') = %s
                ORDER BY event_dt DESC
                LIMIT %s
                """,
                (start_at, finish_at, normalized, limit),
            ).fetchall()
        )


def _fuel_state(status: str) -> float | None:
    return 0.0 if status == "ok" else None


def merge_dashboard_payload(
    arvento: Mapping[str, Any],
    fuel: Mapping[str, Any],
    start_day: date,
    end_day: date,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Merge database rows into the stable dashboard response contract."""
    now = _aware(now) or datetime.now(TZ)
    fuel_status = str(fuel.get("status") or "unavailable")
    offline_minutes = _integer_setting(
        "FLEET_API_OFFLINE_MINUTES",
        DEFAULT_OFFLINE_MINUTES,
        5,
        10_080,
    )
    moving_speed = _float_setting(
        "FLEET_API_MOVING_SPEED_KMH",
        DEFAULT_MOVING_SPEED_KMH,
        0.1,
        30.0,
    )

    vehicles: dict[str, dict[str, Any]] = {}
    fleet_day_mileage: defaultdict[date, float] = defaultdict(float)
    cached_fuel_liters = 0.0

    for row in arvento.get("daily_rows", []):
        normalized = normalize_plate(row.get("normalized_plate") or row.get("plate"))
        report_day = _day(row.get("report_day"))
        if not normalized or report_day is None:
            continue
        vehicle = vehicles.setdefault(
            normalized,
            {
                "normalized_plate": normalized,
                "plate": str(row.get("plate") or normalized),
                "driver": "",
                "company": "",
                "group_name": "",
                "grade": "",
                "mileage_km": 0.0,
                "inside_km": 0.0,
                "outside_km": 0.0,
                "worked_hours": 0.0,
                "max_speed_kmh": None,
                "boundary_violations": 0,
                "personal_use_days": 0,
                "weekend_work_days": 0,
                "night_work_days": 0,
            },
        )
        vehicle["plate"] = str(row.get("plate") or vehicle["plate"])
        vehicle["driver"] = str(row.get("user_name") or vehicle["driver"])
        vehicle["company"] = str(row.get("company") or vehicle["company"])
        vehicle["grade"] = str(row.get("grade") or vehicle["grade"])
        total_km = _as_float(row.get("total_km"))
        inside_km = _as_float(row.get("inside_km"))
        outside_km = _as_float(row.get("outside_km"))
        vehicle["mileage_km"] += total_km
        vehicle["inside_km"] += inside_km
        vehicle["outside_km"] += outside_km
        vehicle["worked_hours"] += _as_float(row.get("worked_hours"))
        speed = row.get("max_speed")
        if speed is not None:
            vehicle["max_speed_kmh"] = max(
                _as_float(vehicle.get("max_speed_kmh")),
                _as_float(speed),
            )
        vehicle["boundary_violations"] += _as_int(row.get("boundary_violation"))
        vehicle["personal_use_days"] += _as_int(row.get("personal_use"))
        vehicle["weekend_work_days"] += _as_int(row.get("weekend_work"))
        vehicle["night_work_days"] += _as_int(row.get("night_work"))
        cached_fuel_liters += _as_float(row.get("fuel_liters"))
        fleet_day_mileage[report_day] += total_km
    current_by_plate: dict[str, Mapping[str, Any]] = {}
    for row in arvento.get("current_rows", []):
        normalized = normalize_plate(row.get("normalized_plate") or row.get("plate"))
        if not normalized:
            continue
        current_by_plate[normalized] = row
        vehicle = vehicles.setdefault(
            normalized,
            {
                "normalized_plate": normalized,
                "plate": str(row.get("plate") or normalized),
                "driver": "",
                "company": "",
                "group_name": "",
                "grade": "",
                "mileage_km": 0.0,
                "inside_km": 0.0,
                "outside_km": 0.0,
                "worked_hours": 0.0,
                "max_speed_kmh": None,
                "boundary_violations": 0,
                "personal_use_days": 0,
                "weekend_work_days": 0,
                "night_work_days": 0,
            },
        )
        vehicle["plate"] = str(row.get("plate") or vehicle["plate"])
        vehicle["driver"] = str(row.get("driver") or vehicle["driver"])
        vehicle["group_name"] = str(row.get("group_name") or vehicle["group_name"])

    fuel_by_vehicle: defaultdict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "liters": 0.0,
            "amount_try": 0.0,
            "transaction_count": 0,
            "sources": defaultdict(lambda: {"liters": 0.0, "amount_try": 0.0, "transaction_count": 0}),
        }
    )
    fuel_by_day: defaultdict[date, dict[str, float]] = defaultdict(
        lambda: {"liters": 0.0, "amount_try": 0.0, "transaction_count": 0.0}
    )
    source_totals: defaultdict[str, dict[str, float]] = defaultdict(
        lambda: {"liters": 0.0, "amount_try": 0.0, "transaction_count": 0.0}
    )
    unmatched_liters = 0.0
    unmatched_transactions = 0

    if fuel_status == "ok":
        for row in fuel.get("rows", []):
            normalized = normalize_plate(row.get("plate"))
            event_day = _day(row.get("event_day"))
            liters = _as_float(row.get("liters"))
            amount = _as_float(row.get("amount_try"))
            count = _as_int(row.get("transaction_count"))
            if not normalized or event_day is None or normalized not in vehicles:
                unmatched_liters += liters
                unmatched_transactions += count
                continue
            source = str(row.get("source") or "unknown").strip().upper() or "UNKNOWN"
            bucket = fuel_by_vehicle[normalized]
            bucket["liters"] += liters
            bucket["amount_try"] += amount
            bucket["transaction_count"] += count
            bucket["sources"][source]["liters"] += liters
            bucket["sources"][source]["amount_try"] += amount
            bucket["sources"][source]["transaction_count"] += count
            fuel_by_day[event_day]["liters"] += liters
            fuel_by_day[event_day]["amount_try"] += amount
            fuel_by_day[event_day]["transaction_count"] += count
            source_totals[source]["liters"] += liters
            source_totals[source]["amount_try"] += amount
            source_totals[source]["transaction_count"] += count

    vehicle_rows: list[dict[str, Any]] = []
    status_counts = {"moving": 0, "parked": 0, "offline": 0}
    for normalized, vehicle in sorted(vehicles.items(), key=lambda item: item[1]["plate"]):
        current = current_by_plate.get(normalized, {})
        last_seen_at = current.get("last_seen_at")
        status, age_minutes = classify_vehicle_status(
            last_seen_at,
            current.get("speed_kmh"),
            now,
            offline_minutes=offline_minutes,
            moving_speed_kmh=moving_speed,
        )
        status_counts[status] += 1
        fuel_bucket = fuel_by_vehicle.get(normalized)
        fuel_liters = (
            _rounded(_as_float(fuel_bucket.get("liters")), 3)
            if fuel_status == "ok" and fuel_bucket is not None
            else _fuel_state(fuel_status)
        )
        fuel_amount = (
            _rounded(_as_float(fuel_bucket.get("amount_try")), 2)
            if fuel_status == "ok" and fuel_bucket is not None
            else _fuel_state(fuel_status)
        )
        transaction_count = (
            _as_int(fuel_bucket.get("transaction_count"))
            if fuel_status == "ok" and fuel_bucket is not None
            else (0 if fuel_status == "ok" else None)
        )
        mileage = _as_float(vehicle["mileage_km"])
        consumption = (
            fuel_liters / mileage * 100.0
            if fuel_liters is not None and mileage > 0
            else None
        )
        sources = []
        if fuel_bucket is not None:
            sources = [
                {
                    "source": source,
                    "liters": _rounded(values["liters"], 3),
                    "amount_try": _rounded(values["amount_try"], 2),
                    "transaction_count": _as_int(values["transaction_count"]),
                }
                for source, values in sorted(fuel_bucket["sources"].items())
            ]
        vehicle_rows.append(
            {
                **vehicle,
                "mileage_km": _rounded(mileage, 1),
                "inside_km": _rounded(_as_float(vehicle["inside_km"]), 1),
                "outside_km": _rounded(_as_float(vehicle["outside_km"]), 1),
                "worked_hours": _rounded(_as_float(vehicle["worked_hours"]), 1),
                "max_speed_kmh": _rounded(vehicle.get("max_speed_kmh"), 1),
                "fuel_liters": fuel_liters,
                "fuel_amount_try": fuel_amount,
                "fuel_transactions": transaction_count,
                "liters_per_100km": _rounded(consumption, 2),
                "fuel_sources": sources,
                "status": status,
                "last_seen_at": _iso(last_seen_at),
                "data_age_minutes": _rounded(age_minutes, 1),
                "speed_kmh": _rounded(_as_float(current.get("speed_kmh")), 1),
                "latitude": _rounded(current.get("latitude"), 6),
                "longitude": _rounded(current.get("longitude"), 6),
            }
        )

    daily_rows: list[dict[str, Any]] = []
    cursor_day = start_day
    while cursor_day <= end_day:
        mileage = fleet_day_mileage[cursor_day]
        fuel_day = fuel_by_day.get(cursor_day)
        liters = (
            _as_float(fuel_day.get("liters"))
            if fuel_status == "ok" and fuel_day is not None
            else _fuel_state(fuel_status)
        )
        amount = (
            _as_float(fuel_day.get("amount_try"))
            if fuel_status == "ok" and fuel_day is not None
            else _fuel_state(fuel_status)
        )
        daily_rows.append(
            {
                "date": cursor_day.isoformat(),
                "mileage_km": _rounded(mileage, 1),
                "fuel_liters": _rounded(liters, 3),
                "fuel_amount_try": _rounded(amount, 2),
                "liters_per_100km": _rounded(liters / mileage * 100.0, 2)
                if liters is not None and mileage > 0
                else None,
            }
        )
        cursor_day += timedelta(days=1)

    total_mileage = sum(_as_float(vehicle["mileage_km"]) for vehicle in vehicle_rows)
    total_fuel = (
        sum(_as_float(vehicle["fuel_liters"]) for vehicle in vehicle_rows)
        if fuel_status == "ok"
        else None
    )
    total_amount = (
        sum(_as_float(vehicle["fuel_amount_try"]) for vehicle in vehicle_rows)
        if fuel_status == "ok"
        else None
    )
    fleet_consumption = (
        total_fuel / total_mileage * 100.0
        if total_fuel is not None and total_mileage > 0
        else None
    )
    fleet_max_speed = max(
        (
            _as_float(vehicle["max_speed_kmh"])
            for vehicle in vehicle_rows
            if vehicle.get("max_speed_kmh") is not None
        ),
        default=None,
    )

    return {
        "meta": {
            "date_from": start_day.isoformat(),
            "date_to": end_day.isoformat(),
            "generated_at": now.isoformat(),
            "timezone": str(TZ),
            "cache_complete": bool(arvento.get("cache_complete")),
            "cache_success_days": _as_int(arvento.get("cache_success_days")),
            "cache_expected_days": _as_int(arvento.get("cache_expected_days")),
            "cache_refreshed_at": _iso(arvento.get("cache_refreshed_at")),
            "fuel_status": fuel_status,
            "fuel_latest_event_at": _iso(fuel.get("latest_event_at")),
            "cached_fuel_liters": _rounded(cached_fuel_liters, 3),
            "live_vs_cached_fuel_difference_liters": _rounded(
                total_fuel - cached_fuel_liters,
                3,
            )
            if total_fuel is not None
            else None,
            "unmatched_fuel_liters": _rounded(unmatched_liters, 3),
            "unmatched_fuel_transactions": unmatched_transactions,
        },
        "summary": {
            "vehicles_total": len(vehicle_rows),
            "moving": status_counts["moving"],
            "parked": status_counts["parked"],
            "offline": status_counts["offline"],
            "vehicles_without_mileage": sum(
                1 for vehicle in vehicle_rows if _as_float(vehicle["mileage_km"]) <= 0
            ),
            "total_mileage_km": _rounded(total_mileage, 1),
            "inside_km": _rounded(
                sum(_as_float(vehicle["inside_km"]) for vehicle in vehicle_rows),
                1,
            ),
            "outside_km": _rounded(
                sum(_as_float(vehicle["outside_km"]) for vehicle in vehicle_rows),
                1,
            ),
            "worked_hours": _rounded(
                sum(_as_float(vehicle["worked_hours"]) for vehicle in vehicle_rows),
                1,
            ),
            "max_speed_kmh": _rounded(fleet_max_speed, 1),
            "boundary_violations": sum(
                _as_int(vehicle["boundary_violations"]) for vehicle in vehicle_rows
            ),
            "personal_use_days": sum(
                _as_int(vehicle["personal_use_days"]) for vehicle in vehicle_rows
            ),
            "weekend_work_days": sum(
                _as_int(vehicle["weekend_work_days"]) for vehicle in vehicle_rows
            ),
            "night_work_days": sum(
                _as_int(vehicle["night_work_days"]) for vehicle in vehicle_rows
            ),
            "average_mileage_km": _rounded(
                total_mileage / len(vehicle_rows) if vehicle_rows else 0.0,
                1,
            ),
            "fuel_liters": _rounded(total_fuel, 3),
            "fuel_amount_try": _rounded(total_amount, 2),
            "liters_per_100km": _rounded(fleet_consumption, 2),
        },
        "daily": daily_rows,
        "fuel_by_source": [
            {
                "source": source,
                "liters": _rounded(values["liters"], 3),
                "amount_try": _rounded(values["amount_try"], 2),
                "transaction_count": _as_int(values["transaction_count"]),
            }
            for source, values in sorted(source_totals.items())
        ],
        "vehicles": vehicle_rows,
    }


def vehicle_detail_payload(
    dashboard: Mapping[str, Any],
    arvento: Mapping[str, Any],
    fuel: Mapping[str, Any],
    normalized: str,
    events: Sequence[Mapping[str, Any]],
    start_day: date,
    end_day: date,
) -> dict[str, Any] | None:
    vehicle = next(
        (
            item
            for item in dashboard.get("vehicles", [])
            if item.get("normalized_plate") == normalized
        ),
        None,
    )
    if vehicle is None:
        return None

    arvento_by_day: defaultdict[date, dict[str, float]] = defaultdict(
        lambda: {
            "mileage_km": 0.0,
            "inside_km": 0.0,
            "outside_km": 0.0,
            "worked_hours": 0.0,
        }
    )
    for row in arvento.get("daily_rows", []):
        row_plate = normalize_plate(row.get("normalized_plate") or row.get("plate"))
        report_day = _day(row.get("report_day"))
        if row_plate != normalized or report_day is None:
            continue
        arvento_by_day[report_day]["mileage_km"] += _as_float(row.get("total_km"))
        arvento_by_day[report_day]["inside_km"] += _as_float(row.get("inside_km"))
        arvento_by_day[report_day]["outside_km"] += _as_float(row.get("outside_km"))
        arvento_by_day[report_day]["worked_hours"] += _as_float(row.get("worked_hours"))

    fuel_by_day: defaultdict[date, dict[str, float]] = defaultdict(
        lambda: {"liters": 0.0, "amount_try": 0.0}
    )
    if fuel.get("status") == "ok":
        for row in fuel.get("rows", []):
            row_plate = normalize_plate(row.get("plate"))
            event_day = _day(row.get("event_day"))
            if row_plate != normalized or event_day is None:
                continue
            fuel_by_day[event_day]["liters"] += _as_float(row.get("liters"))
            fuel_by_day[event_day]["amount_try"] += _as_float(row.get("amount_try"))

    daily: list[dict[str, Any]] = []
    cursor_day = start_day
    while cursor_day <= end_day:
        arvento_day = arvento_by_day[cursor_day]
        fuel_day = fuel_by_day.get(cursor_day)
        mileage = arvento_day["mileage_km"]
        liters = (
            _as_float(fuel_day.get("liters"))
            if fuel.get("status") == "ok" and fuel_day is not None
            else _fuel_state(str(fuel.get("status")))
        )
        daily.append(
            {
                "date": cursor_day.isoformat(),
                "mileage_km": _rounded(mileage, 1),
                "inside_km": _rounded(arvento_day["inside_km"], 1),
                "outside_km": _rounded(arvento_day["outside_km"], 1),
                "worked_hours": _rounded(arvento_day["worked_hours"], 1),
                "fuel_liters": _rounded(liters, 3),
                "fuel_amount_try": _rounded(
                    _as_float(fuel_day.get("amount_try"))
                    if fuel_day is not None
                    else _fuel_state(str(fuel.get("status"))),
                    2,
                ),
                "liters_per_100km": _rounded(liters / mileage * 100.0, 2)
                if liters is not None and mileage > 0
                else None,
            }
        )
        cursor_day += timedelta(days=1)

    return {
        "meta": dashboard["meta"],
        "vehicle": vehicle,
        "daily": daily,
        "fuel_events": [
            {
                "event_at": _iso(event.get("event_dt")),
                "plate": event.get("plate"),
                "source": event.get("source"),
                "fuel_type": event.get("fuel_type_norm"),
                "liters": _rounded(_as_float(event.get("liters")), 3),
                "amount_try": _rounded(_as_float(event.get("amount_try")), 2),
                "station_name": event.get("station_name"),
                "station_city": event.get("station_city"),
            }
            for event in events
        ],
    }


def _degraded_fuel_snapshot(status: str) -> dict[str, Any]:
    return {"status": status, "rows": [], "latest_event_at": None}


def apply_fleet_dashboard_api(
    app: FastAPI,
    database_url_factory: DatabaseUrlFactory,
) -> None:
    """Attach fleet routes exactly once to the production portal app."""
    if getattr(app.state, "fleet_dashboard_api_applied", False):
        return

    dependencies = [Depends(require_fleet_token), Depends(prevent_fleet_caching)]

    @app.get("/api/v1/fleet/health", dependencies=dependencies)
    def fleet_health() -> dict[str, Any]:
        return {
            "ok": True,
            "app": "Arvento fleet dashboard API",
            "fuel_configured": bool(os.environ.get("FUEL_DATABASE_URL", "").strip()),
            "time": datetime.now(TZ).isoformat(),
        }

    @app.get("/api/v1/fleet/dashboard", dependencies=dependencies)
    def fleet_dashboard(
        date_from: date = Query(...),
        date_to: date = Query(...),
    ) -> dict[str, Any]:
        try:
            validate_period(date_from, date_to)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            arvento = load_arvento_snapshot(database_url_factory(), date_from, date_to)
        except (psycopg.Error, RuntimeError, ValueError) as exc:
            LOGGER.exception("Fleet API could not load Arvento dashboard data")
            raise HTTPException(status_code=503, detail="Arvento data is unavailable") from exc

        fuel_url = os.environ.get("FUEL_DATABASE_URL", "").strip()
        try:
            fuel = load_fuel_snapshot(fuel_url, date_from, date_to)
        except (psycopg.Error, RuntimeError, ValueError):
            LOGGER.exception("Fleet API could not load Fuel Monitor dashboard data")
            fuel = _degraded_fuel_snapshot("unavailable")
        return merge_dashboard_payload(arvento, fuel, date_from, date_to)

    @app.get("/api/v1/fleet/vehicles/{plate}", dependencies=dependencies)
    def fleet_vehicle_detail(
        plate: str,
        date_from: date = Query(...),
        date_to: date = Query(...),
    ) -> dict[str, Any]:
        try:
            validate_period(date_from, date_to)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        normalized = normalize_plate(plate)
        if not normalized:
            raise HTTPException(status_code=400, detail="Некорректный госномер")
        try:
            arvento = load_arvento_snapshot(database_url_factory(), date_from, date_to)
        except (psycopg.Error, RuntimeError, ValueError) as exc:
            LOGGER.exception("Fleet API could not load Arvento vehicle data")
            raise HTTPException(status_code=503, detail="Arvento data is unavailable") from exc

        fuel_url = os.environ.get("FUEL_DATABASE_URL", "").strip()
        try:
            fuel = load_fuel_snapshot(fuel_url, date_from, date_to)
            events = load_fuel_events(fuel_url, normalized, date_from, date_to)
        except (psycopg.Error, RuntimeError, ValueError):
            LOGGER.exception("Fleet API could not load Fuel Monitor vehicle data")
            fuel = _degraded_fuel_snapshot("unavailable")
            events = []
        dashboard = merge_dashboard_payload(arvento, fuel, date_from, date_to)
        detail = vehicle_detail_payload(
            dashboard,
            arvento,
            fuel,
            normalized,
            events,
            date_from,
            date_to,
        )
        if detail is None:
            raise HTTPException(status_code=404, detail="Автомобиль не найден")
        return detail

    app.state.fleet_dashboard_api_applied = True


__all__ = [
    "apply_fleet_dashboard_api",
    "authorization_matches",
    "authorization_matches_sha256",
    "classify_vehicle_status",
    "load_arvento_snapshot",
    "load_fuel_events",
    "load_fuel_snapshot",
    "merge_dashboard_payload",
    "validate_period",
    "vehicle_detail_payload",
]
