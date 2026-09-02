#!/usr/bin/env python3
"""Deterministic checks for the fleet dashboard API contract."""
from __future__ import annotations

import hashlib
import os
from datetime import date, datetime, timedelta

from fastapi import FastAPI

from fleet_dashboard_api import (
    FleetDashboardResponse,
    FleetVehicleDetailResponse,
    apply_fleet_dashboard_api,
    authorization_matches,
    authorization_matches_sha256,
    classify_vehicle_status,
    merge_dashboard_payload,
    validate_period,
    vehicle_detail_payload,
)


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check_authorization_and_period() -> None:
    expect(authorization_matches("Bearer top-secret", "top-secret"), "Bearer token must match")
    expect(authorization_matches("bearer top-secret", "top-secret"), "scheme is case-insensitive")
    expect(not authorization_matches("Basic top-secret", "top-secret"), "Basic must be rejected")
    expect(not authorization_matches("Bearer wrong", "top-secret"), "wrong token must be rejected")
    expect(not authorization_matches(None, "top-secret"), "missing header must be rejected")
    digest = hashlib.sha256(b"top-secret").hexdigest()
    expect(
        authorization_matches_sha256("Bearer top-secret", digest),
        "Bearer token must match its SHA-256 digest",
    )
    expect(
        authorization_matches_sha256("bearer top-secret", digest.upper()),
        "SHA-256 digest matching is case-insensitive",
    )
    expect(
        not authorization_matches_sha256("Bearer wrong", digest),
        "wrong token digest must be rejected",
    )
    expect(
        not authorization_matches_sha256("Bearer top-secret", "not-a-digest"),
        "invalid configured digest must be rejected",
    )

    previous = os.environ.pop("FLEET_API_MAX_PERIOD_DAYS", None)
    try:
        expect(validate_period(date(2026, 8, 1), date(2026, 8, 31)) == 31, "inclusive period")
        try:
            validate_period(date(2026, 8, 2), date(2026, 8, 1))
        except ValueError:
            pass
        else:
            raise AssertionError("reversed period must fail")
        os.environ["FLEET_API_MAX_PERIOD_DAYS"] = "2"
        try:
            validate_period(date(2026, 8, 1), date(2026, 8, 3))
        except ValueError:
            pass
        else:
            raise AssertionError("oversized period must fail")
    finally:
        if previous is None:
            os.environ.pop("FLEET_API_MAX_PERIOD_DAYS", None)
        else:
            os.environ["FLEET_API_MAX_PERIOD_DAYS"] = previous


def check_statuses() -> None:
    now = datetime.fromisoformat("2026-08-15T12:00:00+03:00")
    moving, age = classify_vehicle_status(
        now - timedelta(minutes=5),
        12.0,
        now,
        offline_minutes=180,
        moving_speed_kmh=3.0,
    )
    expect(moving == "moving" and round(age or 0) == 5, "fresh moving vehicle")
    parked, _ = classify_vehicle_status(
        now - timedelta(minutes=30),
        0.0,
        now,
        offline_minutes=180,
        moving_speed_kmh=3.0,
    )
    expect(parked == "parked", "fresh stopped vehicle")
    offline, _ = classify_vehicle_status(
        now - timedelta(minutes=181),
        50.0,
        now,
        offline_minutes=180,
        moving_speed_kmh=3.0,
    )
    expect(offline == "offline", "stale vehicle must be offline")
    expect(
        classify_vehicle_status(
            None,
            None,
            now,
            offline_minutes=180,
            moving_speed_kmh=3.0,
        )[0]
        == "offline",
        "vehicle without telemetry must be offline",
    )


def sample_snapshots() -> tuple[dict, dict, datetime]:
    now = datetime.fromisoformat("2026-08-15T12:00:00+03:00")
    arvento = {
        "daily_rows": [
            {
                "report_day": date(2026, 8, 14),
                "normalized_plate": "01ABC123",
                "plate": "01 ABC 123",
                "company": "Akkuyu",
                "user_name": "Driver One",
                "grade": "A",
                "max_speed": 90,
                "total_km": 100,
                "inside_km": 40,
                "outside_km": 60,
                "worked_hours": 5,
                "boundary_violation": 1,
                "personal_use": 0,
                "weekend_work": 0,
                "night_work": 0,
                "fuel_liters": 8,
            },
            {
                "report_day": date(2026, 8, 15),
                "normalized_plate": "01ABC123",
                "plate": "01 ABC 123",
                "company": "Akkuyu",
                "user_name": "Driver One",
                "grade": "A",
                "max_speed": 80,
                "total_km": 50,
                "inside_km": 20,
                "outside_km": 30,
                "worked_hours": 3,
                "boundary_violation": 0,
                "personal_use": 0,
                "weekend_work": 0,
                "night_work": 0,
                "fuel_liters": 7,
            },
            {
                "report_day": date(2026, 8, 15),
                "normalized_plate": "34XYZ987",
                "plate": "34 XYZ 987",
                "company": "Akkuyu",
                "user_name": "Driver Two",
                "grade": "B",
                "max_speed": None,
                "total_km": 0,
                "inside_km": 0,
                "outside_km": 0,
                "worked_hours": None,
                "boundary_violation": 0,
                "personal_use": 0,
                "weekend_work": 0,
                "night_work": 0,
                "fuel_liters": 0,
            },
        ],
        "current_rows": [
            {
                "normalized_plate": "01ABC123",
                "plate": "01 ABC 123",
                "driver": "Driver One",
                "group_name": "TSM",
                "last_seen_at": now - timedelta(minutes=5),
                "speed_kmh": 12,
                "latitude": 36.145,
                "longitude": 33.535,
            },
            {
                "normalized_plate": "34XYZ987",
                "plate": "34 XYZ 987",
                "driver": "Driver Two",
                "group_name": "TSM",
                "last_seen_at": now - timedelta(minutes=30),
                "speed_kmh": 0,
                "latitude": 36.1,
                "longitude": 33.5,
            },
        ],
        "cache_refreshed_at": now - timedelta(minutes=10),
        "cache_complete": True,
        "cache_success_days": 2,
        "cache_expected_days": 2,
    }
    fuel = {
        "status": "ok",
        "latest_event_at": now - timedelta(hours=1),
        "rows": [
            {
                "event_day": date(2026, 8, 14),
                "plate": "01-ABC-123",
                "source": "shell",
                "liters": 8,
                "amount_try": 400,
                "transaction_count": 1,
            },
            {
                "event_day": date(2026, 8, 15),
                "plate": "01 ABC 123",
                "source": "turpak",
                "liters": 7,
                "amount_try": 350,
                "transaction_count": 1,
            },
            {
                "event_day": date(2026, 8, 15),
                "plate": "UNMATCHED",
                "source": "shell",
                "liters": 5,
                "amount_try": 250,
                "transaction_count": 1,
            },
        ],
    }
    return arvento, fuel, now


def check_merge_and_detail() -> None:
    arvento, fuel, now = sample_snapshots()
    dashboard = merge_dashboard_payload(
        arvento,
        fuel,
        date(2026, 8, 14),
        date(2026, 8, 15),
        now=now,
    )
    FleetDashboardResponse.model_validate(dashboard)
    summary = dashboard["summary"]
    expect(summary["vehicles_total"] == 2, "vehicle count")
    expect(summary["moving"] == 1 and summary["parked"] == 1, "live status counts")
    expect(summary["total_mileage_km"] == 150.0, "Arvento mileage total")
    expect(summary["inside_km"] == 60.0 and summary["outside_km"] == 90.0, "mileage split")
    expect(summary["worked_hours"] == 8.0, "worked hours")
    second_vehicle = next(
        item for item in dashboard["vehicles"] if item["normalized_plate"] == "34XYZ987"
    )
    expect(second_vehicle["worked_hours"] is None, "unknown worked hours stay null")
    expect(summary["max_speed_kmh"] == 90.0, "maximum speed")
    expect(summary["boundary_violations"] == 1, "boundary violations")
    expect(summary["fuel_liters"] == 15.0, "matched Fuel Monitor liters")
    expect(summary["fuel_amount_try"] == 750.0, "matched Fuel Monitor cost")
    expect(summary["liters_per_100km"] == 10.0, "fleet fuel consumption")
    expect(dashboard["meta"]["unmatched_fuel_liters"] == 5.0, "unmatched fuel QA")
    expect(dashboard["meta"]["live_vs_cached_fuel_difference_liters"] == 0.0, "cache QA")
    expect(
        [item["source"] for item in dashboard["fuel_by_source"]] == ["SHELL", "TURPAK"],
        "fuel source split",
    )
    degraded = merge_dashboard_payload(
        arvento,
        {"status": "unavailable", "rows": [], "latest_event_at": None},
        date(2026, 8, 14),
        date(2026, 8, 15),
        now=now,
    )
    expect(degraded["summary"]["fuel_liters"] is None, "unavailable fuel is not zero")
    expect(degraded["vehicles"][0]["fuel_liters"] is None, "vehicle fuel degrades to null")

    normalized = "01ABC123"
    events = [
        {
            "event_dt": now - timedelta(hours=1),
            "plate": "01 ABC 123",
            "source": "TURPAK",
            "fuel_type_norm": "DIESEL",
            "liters": 7,
            "amount_try": 350,
            "station_name": "Station",
            "station_city": "Silifke",
        }
    ]
    detail = vehicle_detail_payload(
        dashboard,
        arvento,
        fuel,
        normalized,
        events,
        date(2026, 8, 14),
        date(2026, 8, 15),
    )
    expect(detail is not None, "vehicle detail exists")
    FleetVehicleDetailResponse.model_validate(detail)
    expect(detail["vehicle"]["fuel_liters"] == 15.0, "vehicle fuel total")
    expect(detail["daily"][0]["mileage_km"] == 100.0, "daily mileage")
    expect(detail["fuel_events"][0]["fuel_type"] == "DIESEL", "fuel event contract")
    unknown_hours_detail = vehicle_detail_payload(
        dashboard,
        arvento,
        fuel,
        "34XYZ987",
        [],
        date(2026, 8, 14),
        date(2026, 8, 15),
    )
    expect(unknown_hours_detail is not None, "second vehicle detail exists")
    expect(
        all(item["worked_hours"] is None for item in unknown_hours_detail["daily"]),
        "unknown daily worked hours stay null",
    )
    expect(
        vehicle_detail_payload(
            dashboard,
            arvento,
            fuel,
            "NOTFOUND",
            [],
            date(2026, 8, 14),
            date(2026, 8, 15),
        )
        is None,
        "unknown vehicle",
    )


def check_route_registration() -> None:
    app = FastAPI()
    apply_fleet_dashboard_api(app, lambda: "postgresql://unused")
    apply_fleet_dashboard_api(app, lambda: "postgresql://unused")
    paths = [route.path for route in app.routes]
    for expected in (
        "/api/v1/fleet/health",
        "/api/v1/fleet/dashboard",
        "/api/v1/fleet/vehicles/{plate}",
    ):
        expect(paths.count(expected) == 1, f"route must be registered once: {expected}")

    schema = app.openapi()
    security_scheme = schema["components"]["securitySchemes"]["FleetBearerAuth"]
    expect(security_scheme["type"] == "http", "Swagger must expose HTTP auth")
    expect(security_scheme["scheme"] == "bearer", "Swagger must expose Bearer auth")
    expect(
        any(tag["name"] == "Fleet API" for tag in schema.get("tags", [])),
        "Fleet routes must have a documented OpenAPI tag",
    )

    expected_models = {
        "/api/v1/fleet/health": "FleetHealthResponse",
        "/api/v1/fleet/dashboard": "FleetDashboardResponse",
        "/api/v1/fleet/vehicles/{plate}": "FleetVehicleDetailResponse",
    }
    for path, model_name in expected_models.items():
        operation = schema["paths"][path]["get"]
        expect(operation.get("summary"), f"Swagger summary missing: {path}")
        expect(operation.get("description"), f"Swagger description missing: {path}")
        expect(operation.get("tags") == ["Fleet API"], f"Swagger tag mismatch: {path}")
        expect(
            operation.get("security") == [{"FleetBearerAuth": []}],
            f"Bearer security missing: {path}",
        )
        response_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        expect(
            response_schema.get("$ref", "").endswith(f"/{model_name}"),
            f"response model mismatch: {path}",
        )

    dashboard_parameters = schema["paths"]["/api/v1/fleet/dashboard"]["get"]["parameters"]
    parameter_names = {parameter["name"] for parameter in dashboard_parameters}
    expect(parameter_names == {"date_from", "date_to"}, "dashboard period parameters")


def main() -> int:
    check_authorization_and_period()
    check_statuses()
    check_merge_and_detail()
    check_route_registration()
    print("OK: fleet dashboard API contract verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
