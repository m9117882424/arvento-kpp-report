#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline contract checks for report rules, routes, and roster selection."""
from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import business_rules
import consolidated_portal
import portal_entrypoint
import report_portal
import roster_registry
import run_report_portal
from consolidated_multi_report import DatedRoster, select_roster
from roster_registry import Roster


def check_business_rules() -> None:
    assert business_rules.DEFAULT_SITE_SPEED_THRESHOLD_KMH == 50.0
    assert business_rules.DEFAULT_OUTSIDE_SPEED_THRESHOLD_KMH == 103.0
    assert business_rules.MIN_SPEED_EVENT_DURATION_SECONDS == 3
    assert business_rules.MAX_ACCELERATION_MPS2 == 3.0
    assert business_rules.MILEAGE_REVIEW_ABSOLUTE_GAP_KM == 10.0
    assert business_rules.MILEAGE_REVIEW_RATIO == 1.20


def check_roster_policy() -> None:
    old = DatedRoster(Path("2026-08-01.xlsx"), date(2026, 8, 1), {})
    current = DatedRoster(Path("2026-08-10.xlsx"), date(2026, 8, 10), {})
    assert select_roster([old, current], date(2026, 8, 10)) is current
    assert select_roster([old, current], date(2026, 8, 7)) is old

    registry_old = Roster(date(2026, 8, 1), Path("old.xlsx"), {})
    registry_current = Roster(date(2026, 8, 10), Path("current.xlsx"), {})
    assert (
        roster_registry.select_roster(
            [registry_old, registry_current], date(2026, 8, 7)
        )
        is registry_old
    )

    for selector, rosters in (
        (select_roster, [old, current]),
        (roster_registry.select_roster, [registry_old, registry_current]),
    ):
        try:
            selector(rosters, date(2026, 7, 31))
        except ValueError as exc:
            assert "более ранней датой" in str(exc)
        else:
            raise AssertionError("Будущая разнарядка не должна применяться назад")


def check_routes() -> None:
    routes = {
        (route.path, next(iter(route.methods or set()), "")): route
        for route in portal_entrypoint.app.routes
    }
    assert routes[("/api/generate", "POST")].deprecated is True
    assert routes[("/api/generate-v2", "POST")].deprecated is True
    assert routes[("/api/generate-v3", "POST")].deprecated is None
    assert "readResponsePayload" in portal_entrypoint.portal.implementation.HTML
    assert "return {detail: text};" in portal_entrypoint.portal.implementation.HTML


def check_v1_delegation() -> None:
    original = consolidated_portal.api_generate_v3
    captured = {}

    async def fake_v3(**kwargs):
        captured.update(kwargs)
        return {"delegated": True}

    consolidated_portal.api_generate_v3 = fake_v3
    try:
        result = asyncio.run(
            report_portal.api_generate(
                report_type="violation",
                report_date="2026-09-01",
                report_end_date="",
                roster=None,
                grade_from="",
                grade_to="",
                time_from="",
                time_to="",
                consider_previous_exits=False,
            )
        )
    finally:
        consolidated_portal.api_generate_v3 = original
    assert result == {"delegated": True}
    assert captured["site_speed_threshold"] == "50.0"
    assert captured["outside_speed_threshold"] == "103.0"


def check_v2_consolidated_delegation() -> None:
    original = consolidated_portal.api_generate_v3
    captured = {}

    async def fake_v3(**kwargs):
        captured.update(kwargs)
        return {"delegated": True}

    consolidated_portal.api_generate_v3 = fake_v3
    try:
        result = asyncio.run(
            run_report_portal.api_generate_v2(
                report_type="consolidated",
                report_date="2026-09-01",
                report_end_date="2026-09-01",
                roster=None,
                grade_from="",
                grade_to="",
                time_from="",
                time_to="",
                consider_previous_exits=False,
                site_speed_threshold="50.0",
                outside_speed_threshold="103.0",
            )
        )
    finally:
        consolidated_portal.api_generate_v3 = original
    assert result == {"delegated": True}
    assert captured["report_type"] == "consolidated"


def main() -> None:
    check_business_rules()
    check_roster_policy()
    check_routes()
    check_v1_delegation()
    check_v2_consolidated_delegation()
    print("OK: report contracts, current API delegation, and roster policy verified")


if __name__ == "__main__":
    main()
