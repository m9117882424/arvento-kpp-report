#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline acceptance tests for the new Arvento efficiency portal."""
from __future__ import annotations

import io
import asyncio
import os
import sys
import unittest
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from openpyxl import load_workbook
from pydantic import ValidationError
from fastapi import FastAPI

from efficiency_report import (
    EfficiencySettings,
    GPSDailyMetrics,
    GPSPoint,
    _COVERAGE_CACHE,
    _METRIC_CACHE,
    OUTSIDE_SPEED_SCOPE_VERSION,
    _metric_settings_signature,
    _restore_daily_metrics,
    apply_cache_coverage,
    aggregate_report,
    analyze_daily_track,
    choose_shapes,
    compute_score,
    generate_efficiency_report,
    is_transit,
    load_daily_rows,
    filter_options,
    parking_band,
    shape_from_geojson,
    workbook_from_report,
)
from cache_freshness import CacheCoverage
from efficiency_metric_cache import metric_digest
from efficiency_metric_cache import DailyMetricStore
from geozone_registry import Geozone, Registry
from site_boundary_speed import detect_speed_violations_by_polygon
from efficiency_portal import apply_efficiency_portal
from operational_geofences import ALLOWED_TYPES, POLYGON_TYPES


TZ = ZoneInfo("Europe/Istanbul")


def polygon(a: float, b: float, c: float, d: float) -> dict:
    return {
        "type": "Polygon",
        "coordinates": [[[b, a], [d, a], [d, c], [b, c], [b, a]]],
    }


def settings(**overrides) -> EfficiencySettings:
    config = {
        "date_from": date(2026, 11, 2),
        "date_to": date(2026, 11, 7),
    }
    config.update(overrides)
    return EfficiencySettings(**config)


def fixture_registry() -> Registry:
    return Registry(
        gates=[],
        zones=[
            Geozone("Площадка Аккую", "polygon", "site_boundary",
                    points=[(0, 0), (0, 10), (10, 10), (10, 0)]),
            Geozone("Тоннель", "polygon", "speed_exclusion",
                    points=[(5, 5), (5, 6), (6, 6), (6, 5)]),
        ],
        route_polygon=[(11, 1), (11, 4), (13, 4), (13, 1)],
    )


def points(day: date, specs: list[tuple[int, float, float, float]]) -> list[GPSPoint]:
    start = datetime(day.year, day.month, day.day, 10, 0, tzinfo=TZ)
    return [
        GPSPoint("34 TEST 01", start+timedelta(seconds=seconds),
                 lat=lat, lon=lon, speed=speed, distance_km=.15)
        for seconds, lat, lon, speed in specs
    ]


class EfficiencySettingsTest(unittest.TestCase):
    def test_defaults_and_plate_normalization(self):
        conf = settings(plates=["34 test-01", "34TEST01"])
        self.assertEqual(conf.plates, ["34TEST01"])
        self.assertEqual(conf.transit_min_inside_km, 50)
        self.assertEqual(conf.other_min_inside_km, 30)
        self.assertEqual(conf.parking_min_minutes, 5)
        self.assertEqual(conf.speed_min_duration_seconds, 3)
        self.assertFalse(conf.latest_roster_only)
        self.assertTrue(settings(latest_roster_only=True).latest_roster_only)
        self.assertTrue(is_transit("FORD TRANSİT"))
        self.assertTrue(is_transit("Ford Transit Custom"))
        self.assertFalse(is_transit("FORD Tourneo Courier"))

    def test_latest_roster_query_restricts_plates_without_changing_historical_rosters(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = []
        for enabled in (False, True):
            result = load_daily_rows(connection, settings(latest_roster_only=enabled))
            self.assertEqual(result, [])
            sql, params = cursor.execute.call_args.args
            self.assertEqual(params[-1], enabled)
            self.assertEqual(len(params), 7)
            self.assertIn("NOT %s::boolean", sql)
            self.assertIn("SELECT MAX(roster_day) FROM consolidated_roster_snapshots", sql)
            self.assertIn("newest.normalized_plate = COALESCE(r.normalized_plate,c.normalized_plate)", sql)
            self.assertIn("WHERE roster_day <= d.report_day ORDER BY roster_day DESC LIMIT 1", sql)
            self.assertIn("(r.normalized_plate IS NOT NULL) AS in_roster", sql)

    def test_filter_options_respects_checkbox_and_discloses_newest_roster(self):
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value.fetchone.return_value = (
            date(2026, 11, 9), 205,
        )
        with (
            patch("efficiency_report.psycopg.connect") as connect,
            patch("efficiency_report.load_daily_rows", return_value=[{
                "plate": "34NEW01", "directorate": "Механизация", "user_name": "Тест",
                "model": "Fiat",
            }]) as load,
            patch("efficiency_report.load_shapes", return_value={}),
        ):
            connect.return_value.__enter__.return_value = connection
            result = filter_options(
                "readonly", date(2026, 11, 2), date(2026, 11, 7),
                latest_roster_only=True,
            )
        self.assertTrue(load.call_args.args[1].latest_roster_only)
        self.assertEqual(result["plates"], ["34NEW01"])
        self.assertEqual(result["latest_roster_day"], "2026-11-09")
        self.assertEqual(result["latest_roster_vehicles"], 205)

    def test_invalid_period_and_speed(self):
        with self.assertRaises(ValidationError):
            settings(date_to=date(2026, 11, 1))
        with self.assertRaises(ValidationError):
            settings(site_speed_threshold_kmh=180, outside_speed_threshold_kmh=103)
        with self.assertRaises(ValidationError):
            settings(speed_min_duration_seconds=-1)

    def test_geofence_restrictions_and_polygon_hole(self):
        self.assertIn("OFFICE", ALLOWED_TYPES)
        self.assertIn("OFFICE", POLYGON_TYPES)
        office = shape_from_geojson("OFFICE", "Офис", "PARKING", polygon(1, 1, 2, 2))
        site = shape_from_geojson("SITE", "Территория", "SITE", polygon(0, 0, 10, 10))
        tunnel = shape_from_geojson("TUNNEL", "Тоннель", "SPEED_EXCLUSION", polygon(5, 5, 6, 6))
        self.assertTrue(office.contains(1.5, 1.5))
        self.assertFalse(office.contains(5, 5))
        with self.assertRaises(ValueError):
            choose_shapes({"SITE":site}, ["SITE"], "стоянок")
        with self.assertRaises(ValueError):
            choose_shapes({"TUNNEL":tunnel}, ["TUNNEL"], "разрешённого движения")
        with self.assertRaises(ValueError):
            choose_shapes({"OFFICE":office}, ["MISSING"], "стоянок")
        with_hole = shape_from_geojson("HOLE", "Исключение", "PARKING", {
            "type": "Polygon",
            "coordinates": [
                [[0,0],[4,0],[4,4],[0,4],[0,0]],
                [[1,1],[2,1],[2,2],[1,2],[1,1]],
            ],
        })
        self.assertTrue(with_hole.contains(.5,.5))
        self.assertFalse(with_hole.contains(1.5,1.5))


class GPSMetricTest(unittest.TestCase):
    def test_stops_strictly_greater_than_five_minutes(self):
        office=shape_from_geojson("OFFICE", "Офис", "PARKING", polygon(1, 1, 2, 2))
        track=points(date(2026,11,6),[
            (0,1.2,1.2,0),(180,1.2,1.2,0),(300,1.2,1.2,0),
            (360,1.2,1.2,12),(540,1.2,1.2,0),(900,1.2,1.2,0),
        ])
        result=analyze_daily_track(track,fixture_registry(),[office],[],settings())
        self.assertEqual(result.office_stops,1)
        self.assertEqual(result.office_parking_minutes,6.0)

    def test_gap_does_not_create_fictitious_stop(self):
        office=shape_from_geojson("OFFICE", "Офис", "PARKING", polygon(1, 1, 2, 2))
        track=points(date(2026,11,6),[(0,1.2,1.2,0),(1200,1.2,1.2,0)])
        result=analyze_daily_track(track,fixture_registry(),[office],[],settings())
        self.assertEqual(result.office_stops,0)
        self.assertEqual(result.office_parking_minutes,0)

    def test_weekend_movement_on_site(self):
        track=points(date(2026,11,7),[(0,1,1,12),(120,1.1,1.1,15)])
        result=analyze_daily_track(track,fixture_registry(),[],[],settings())
        self.assertEqual(result.moving_minutes,2)
        self.assertTrue(result.weekend_site_movement)

    def test_lunch_crossings_require_continuous_gps(self):
        def p(hour,minute,inside):
            return GPSPoint(
                "34 TEST 01",datetime(2026,11,6,hour,minute,tzinfo=TZ),
                2 if inside else 12,2,20,.1,
            )
        lost=analyze_daily_track(
            [p(10,0,True),p(12,5,False)],fixture_registry(),[],[],settings()
        )
        self.assertEqual((lost.lunch_exits,lost.lunch_returns),(0,0))
        valid=analyze_daily_track(
            [p(12,0,True),p(12,1,False),p(13,0,False),p(13,1,True)],
            fixture_registry(),[],[],settings()
        )
        self.assertEqual((valid.lunch_exits,valid.lunch_returns),(1,1))

    def test_speed_threshold_duration_and_tunnel_exclusion(self):
        registry=fixture_registry()
        good=points(date(2026,11,6),[(0,2,2,60),(4,2,2,61),(8,2,2,62)])
        found,_=detect_speed_violations_by_polygon(
            good,registry,site_threshold_kmh=50,outside_threshold_kmh=103,
            min_duration_seconds=3,
        )
        self.assertEqual(len(found),1)
        found,_=detect_speed_violations_by_polygon(
            good,registry,site_threshold_kmh=50,outside_threshold_kmh=103,
            min_duration_seconds=10,
        )
        self.assertEqual(found,[])
        in_tunnel=points(date(2026,11,6),[(0,5.5,5.5,60),(4,5.5,5.5,61),(8,5.5,5.5,62)])
        found,_=detect_speed_violations_by_polygon(
            in_tunnel,registry,site_threshold_kmh=50,outside_threshold_kmh=103,
            min_duration_seconds=3,
        )
        self.assertEqual(found,[])
        split=points(date(2026,11,6),[
            (0,2,2,60),(4,2,2,60),(8,5.5,5.5,60),
            (12,2,2,60),(16,2,2,60)
        ])
        found,_=detect_speed_violations_by_polygon(
            split,registry,site_threshold_kmh=50,outside_threshold_kmh=103,
            min_duration_seconds=3,
        )
        self.assertEqual(found,[], "Тоннель не должен склеивать эпизоды скорости")

    def test_outside_speed_only_on_tasucu_akkuyu_route(self):
        registry = fixture_registry()
        track = points(date(2026, 11, 6), [
            (0, 2, 2, 60), (4, 2, 2, 61), (8, 2, 2, 62),
            (40, 15, 2, 120), (44, 15, 2, 121), (48, 15, 2, 122),
            (400, 12, 2, 120), (404, 12, 2, 121), (408, 12, 2, 122),
        ])
        site, unrestricted = detect_speed_violations_by_polygon(
            track, registry, 50, 103, min_duration_seconds=3,
        )
        self.assertEqual((len(site), len(unrestricted)), (1, 2))
        site, outside = detect_speed_violations_by_polygon(
            track, registry, 50, 103, min_duration_seconds=3,
            outside_route_only=True,
        )
        self.assertEqual((len(site), len(outside)), (1, 1))
        self.assertEqual(float(outside[0].max_point.lat), 12)
        daily = analyze_daily_track(track, registry, [], [], settings())
        self.assertEqual((daily.speed_site, daily.speed_outside), (1, 1))
        self.assertEqual(len(daily.speed_events), 2)

        # An off-route point must break a violation, not bridge two brief
        # bursts into an event that seemingly lasted inside the corridor.
        crossing = points(date(2026, 11, 6), [
            (0, 12, 2, 120), (4, 12, 2, 121),
            (8, 15, 2, 122),
            (12, 12, 2, 123), (16, 12, 2, 124),
        ])
        self.assertEqual(detect_speed_violations_by_polygon(
            crossing, registry, 50, 103, outside_route_only=True,
        )[1], [])

        # The existing tunnel exclusion also applies inside the route.
        registry.zones.append(Geozone(
            "Тоннель на маршруте", "polygon", "speed_exclusion",
            points=[(11.5, 1.5), (11.5, 2.5), (12.5, 2.5), (12.5, 1.5)],
        ))
        self.assertEqual(detect_speed_violations_by_polygon(
            track, registry, 50, 103, outside_route_only=True,
        )[1], [])

    def test_outside_route_required_and_cached_metrics_versioned(self):
        registry = fixture_registry()
        registry.route_polygon = None
        with self.assertRaisesRegex(ValueError, "ROUTE"):
            detect_speed_violations_by_polygon(
                points(date(2026, 11, 6), [(0, 15, 2, 120)]),
                registry, outside_route_only=True,
            )
        self.assertEqual(_metric_settings_signature(settings())[-1], OUTSIDE_SPEED_SCOPE_VERSION)

    def test_outside_zone_selectable_and_off_when_empty(self):
        track=points(date(2026,11,6),[(0,3,3,15),(60,3.2,3.2,15),(120,3.4,3.4,15)])
        allowed=shape_from_geojson("SITE_SMALL","Разрешена","PARKING",polygon(1,1,2,2))
        actual=analyze_daily_track(track,fixture_registry(),[],[allowed],settings())
        self.assertAlmostEqual(actual.unauthorized_km,.45)
        self.assertEqual(actual.unauthorized_minutes,2)
        disabled=analyze_daily_track(track,fixture_registry(),[],[],settings())
        self.assertEqual(disabled.unauthorized_km,0)
        impossible=points(date(2026,11,6),[(0,3,3,300),(60,3.2,3.2,300)])
        invalid=analyze_daily_track(impossible,fixture_registry(),[],[allowed],settings())
        self.assertEqual(invalid.unauthorized_km,0)
        self.assertEqual(invalid.moving_minutes,0)


class RatingTest(unittest.TestCase):
    def test_portal_has_two_mobile_rankings_and_daily_movement_label(self):
        html = (Path(__file__).parent / "web" / "efficiency_report.html").read_text(encoding="utf-8")
        self.assertIn('id="ranksBest"', html)
        self.assertIn('id="ranksWorst"', html)
        self.assertIn("ТОП-10 эффективных", html)
        self.assertIn("ТОП-10 малоэффективных", html)
        self.assertIn("moving_minutes_per_day", html)
        self.assertIn("moving_minutes_per_vehicle_workday", html)
        self.assertIn(".ranking-column{display:grid", html)
        self.assertNotIn(".rank-panel{display:none}", html)
        self.assertIn(".slice(0,10)", html)

    def test_average_moving_time_uses_planned_weekdays_and_weighted_fleet_days(self):
        def row(plate, day, in_roster=True):
            return {
                "report_day": day, "normalized_plate": plate, "plate": plate,
                "company": "TSM", "user_name": "Тест", "model": "FORD TRANSIT",
                "directorate": "Механизация", "in_roster": in_roster,
                "inside_km": 0, "outside_km": 0, "total_km": 0,
                "raw_points": 3, "cache_ready": True,
            }

        wednesday = date(2026, 11, 4)
        friday = date(2026, 11, 6)
        saturday = date(2026, 11, 7)
        monday = date(2026, 11, 9)
        rows = [
            row("34A", wednesday, False), row("34A", friday),
            row("34A", saturday), row("34A", monday),
            row("34B", monday), row("34NO", friday),
        ]
        gps = {
            (wednesday, "34A"): GPSDailyMetrics(points=3, moving_minutes=90, office_parking_minutes=50),
            (friday, "34A"): GPSDailyMetrics(points=3, moving_minutes=60, office_parking_minutes=30),
            (saturday, "34A"): GPSDailyMetrics(points=3, moving_minutes=180, office_parking_minutes=120),
            (monday, "34A"): GPSDailyMetrics(points=3, moving_minutes=0, office_parking_minutes=0),
            (monday, "34B"): GPSDailyMetrics(points=3, moving_minutes=120, office_parking_minutes=90),
        }
        report = aggregate_report(
            rows, gps, settings(date_from=wednesday, date_to=monday, office_zone_codes=["OFFICE"])
        )
        vehicles = {vehicle["plate"]: vehicle for vehicle in report["vehicles"]}
        self.assertEqual(vehicles["34A"]["eligible_days"], 2)
        self.assertEqual(vehicles["34A"]["gps_workday_days"], 2)
        self.assertEqual(vehicles["34A"]["moving_minutes"], 60)
        self.assertEqual(vehicles["34A"]["moving_minutes_per_day"], 30)
        self.assertEqual(vehicles["34B"]["moving_minutes_per_day"], 120)
        self.assertIsNone(vehicles["34NO"]["moving_minutes_per_day"])
        self.assertEqual(report["summary"]["moving_minutes"], 180)
        self.assertEqual(report["summary"]["moving_minutes_per_vehicle_workday"], 60)
        self.assertEqual(report["summary"]["moving_average_vehicle_count"], 2)
        self.assertEqual(report["summary"]["moving_average_workdays"], 3)
        self.assertEqual(vehicles["34A"]["office_parking_minutes"], 30)
        self.assertEqual(vehicles["34A"]["office_parking_minutes_per_day"], 15)
        self.assertEqual(vehicles["34B"]["office_parking_minutes_per_day"], 90)
        self.assertIsNone(vehicles["34NO"]["office_parking_minutes_per_day"])
        self.assertEqual(report["summary"]["office_parking_minutes"], 120)
        self.assertEqual(report["summary"]["office_parking_minutes_per_vehicle_workday"], 40)
        book = load_workbook(io.BytesIO(workbook_from_report(report)))
        try:
            ranking = book["Рейтинг ТС"]
            self.assertEqual(ranking.cell(1, 14).value, "Суммарное время стоянок в выбранных офисных зонах, чч:мм")
            self.assertEqual(ranking.cell(1, 15).value, "Среднее время офисных стоянок за плановый рабочий день, чч:мм")
            for line in range(2, ranking.max_row + 1):
                plate = ranking.cell(line, 2).value
                self.assertEqual(
                    ranking.cell(line, 15).value,
                    timedelta(minutes=vehicles[plate]["office_parking_minutes_per_day"])
                    if vehicles[plate]["office_parking_minutes_per_day"] is not None else None,
                )
                self.assertEqual(ranking.cell(line, 15).number_format, "[hh]:mm")
            self.assertEqual(ranking.auto_filter.ref, ranking.dimensions)
        finally:
            book.close()

    def test_weekends_only_affect_site_presence_and_speed(self):
        friday, saturday = date(2026, 11, 6), date(2026, 11, 7)
        rows = [
            {
                "report_day": day, "normalized_plate": "34TEST01", "plate": "34TEST01",
                "company": "TSM", "user_name": "Тест", "model": "FIAT EGEA",
                "directorate": "Механизация", "in_roster": True,
                "inside_km": inside, "outside_km": outside, "cache_ready": True,
            }
            for day, inside, outside in [(friday, 80, 20), (saturday, 900, 100)]
        ]
        gps = {
            (friday, "34TEST01"): GPSDailyMetrics(
                points=3, moving_minutes=300, office_parking_minutes=150,
                unauthorized_km=2, lunch_exits=1, office_stops=1,
            ),
            (saturday, "34TEST01"): GPSDailyMetrics(
                points=3, moving_minutes=600, office_parking_minutes=600,
                unauthorized_km=99, lunch_exits=20, office_stops=8,
                weekend_site_movement=True, speed_site=2, speed_outside=1,
            ),
        }
        conf = settings(date_from=friday, date_to=saturday,
                        office_zone_codes=["OFFICE"], allowed_zone_codes=["ALLOWED"])
        report = aggregate_report(rows, gps, conf)
        car = report["vehicles"][0]
        self.assertEqual((car["inside_km"], car["outside_km"]), (80, 20))
        self.assertEqual((car["moving_minutes"], car["office_parking_minutes"]), (300, 150))
        self.assertEqual((car["office_parking_minutes_per_day"], car["eligible_days"]), (150, 1))
        self.assertEqual((car["unauthorized_km"], car["lunch_exits"], car["office_stops"]), (2, 1, 1))
        self.assertEqual((car["weekend_site_days"], car["speed_site_events"], car["speed_outside_events"]), (1, 2, 1))
        self.assertEqual(report["summary"]["office_parking_minutes"], 150)
        self.assertEqual(report["daily"][1]["speed_events"], 3)
        self.assertIsNone(report["daily"][1]["office_minutes"])
        self.assertIsNone(report["daily"][1]["moving_minutes"])
        self.assertIsNone(report["daily"][1]["inside_km"])

        without_weekend = aggregate_report(rows[:1], {(friday, "34TEST01"): gps[(friday, "34TEST01")]}, conf)
        self.assertEqual(car["score"], without_weekend["vehicles"][0]["score"] - 9)

    def test_parking_thresholds_include_boundaries_and_scale_by_workdays(self):
        for minutes, expected in [
            (0, "good"), (149.9, "good"), (150, "good"),
            (150.1, "mid"), (240, "mid"), (240.1, "bad"),
        ]:
            self.assertEqual(parking_band(minutes), expected)
        for minutes, expected in [(300, "good"), (480, "mid"), (480.1, "bad")]:
            self.assertEqual(parking_band(minutes, 2), expected)
        self.assertIsNone(parking_band(None))
        self.assertIsNone(parking_band(100, 0))

    def test_excel_parking_totals_and_averages_share_same_traffic_light(self):
        day = date(2026, 11, 6)
        rows, gps = [], {}
        for plate, parking in [("34GREEN", 150), ("34YELLOW", 240), ("34RED", 240.1)]:
            rows.append({
                "report_day": day, "normalized_plate": plate, "plate": plate,
                "company": "TSM", "user_name": "Тест", "model": "FIAT EGEA",
                "directorate": "Механизация", "in_roster": True,
                "inside_km": 40, "outside_km": 0, "cache_ready": True,
            })
            gps[(day, plate)] = GPSDailyMetrics(points=3, office_parking_minutes=parking)
        report = aggregate_report(rows, gps, settings(
            date_from=day, date_to=day, office_zone_codes=["OFFICE"]
        ))
        book = load_workbook(io.BytesIO(workbook_from_report(report)))
        try:
            ranking = book["Рейтинг ТС"]
            expected = {"34GREEN": "00E0F4E8", "34YELLOW": "00FFF0CD", "34RED": "00FDE2E2"}
            for line in range(2, ranking.max_row + 1):
                plate = ranking.cell(line, 2).value
                self.assertEqual(ranking.cell(line, 14).fill.fgColor.rgb, expected[plate])
                self.assertEqual(ranking.cell(line, 15).fill.fgColor.rgb, expected[plate])
            self.assertIn("Цвет офисных стоянок", [
                cell[0].value for cell in book["Методика"].iter_rows(min_row=2)
            ])
        finally:
            book.close()

    def test_weekend_only_gps_does_not_turn_missing_workday_into_zero(self):
        friday, saturday = date(2026, 11, 6), date(2026, 11, 7)
        rows = [{
            "report_day": day, "normalized_plate": "34TEST01", "plate": "34TEST01",
            "company": "TSM", "user_name": "Тест", "model": "FIAT EGEA",
            "directorate": "Механизация", "in_roster": True,
            "inside_km": 0, "outside_km": 0, "cache_ready": True,
        } for day in (friday, saturday)]
        gps = {(saturday, "34TEST01"): GPSDailyMetrics(
            points=3, office_parking_minutes=360, weekend_site_movement=True, speed_site=1
        )}
        report = aggregate_report(rows, gps, settings(
            date_from=friday, date_to=saturday, office_zone_codes=["OFFICE"]
        ))
        car = report["vehicles"][0]
        self.assertEqual(car["gps_days"], 1)
        self.assertEqual(car["gps_workday_days"], 0)
        self.assertIsNone(car["office_parking_minutes"])
        self.assertIsNone(car["office_parking_minutes_per_day"])
        self.assertIsNone(car["score"])
        self.assertEqual((car["weekend_site_days"], car["speed_events"]), (1, 1))

    def test_zero_movement_with_gps_is_not_missing_and_weekend_only_is_unavailable(self):
        friday = date(2026, 11, 6)
        saturday = date(2026, 11, 7)
        base = {
            "company": "TSM", "user_name": "Тест", "model": "FORD TRANSIT",
            "directorate": "Механизация", "in_roster": True,
            "inside_km": 0, "outside_km": 0, "total_km": 0,
            "raw_points": 3, "cache_ready": True,
        }
        rows = [
            {**base, "report_day": friday, "plate": "34ZERO", "normalized_plate": "34ZERO"},
            {**base, "report_day": saturday, "plate": "34WEEKEND", "normalized_plate": "34WEEKEND"},
        ]
        gps = {
            (friday, "34ZERO"): GPSDailyMetrics(points=3, moving_minutes=0),
            (saturday, "34WEEKEND"): GPSDailyMetrics(points=3, moving_minutes=120),
        }
        report = aggregate_report(rows, gps, settings(date_from=friday, date_to=saturday))
        vehicles = {vehicle["plate"]: vehicle for vehicle in report["vehicles"]}
        self.assertEqual(vehicles["34ZERO"]["moving_minutes_per_day"], 0)
        self.assertIsNone(vehicles["34WEEKEND"]["moving_minutes_per_day"])
        self.assertEqual(report["summary"]["moving_minutes_per_vehicle_workday"], 0)
        self.assertIsNone(vehicles["34ZERO"]["office_parking_minutes_per_day"])
        self.assertIsNone(report["summary"]["office_parking_minutes_per_vehicle_workday"])

    def test_missing_other_vehicle_does_not_hide_valid_ford_score_or_number(self):
        day = date(2026, 11, 6)
        ford = {
            "report_day": day, "normalized_plate": "34MPM505", "plate": "34MPM505",
            "company": "TSM", "user_name": "Плачинда Игорь", "model": "FORD KUGA",
            "directorate": "Механизация", "in_roster": True,
            "inside_km": 0, "outside_km": 50, "total_km": 50,
            "raw_points": 3, "cache_ready": True,
            "cache_gps_event_time": datetime(2026, 11, 6, 15, tzinfo=TZ),
            "cache_gps_received_at": datetime(2026, 11, 6, 16, tzinfo=TZ),
            "cache_geofence_updated_at": datetime(2026, 11, 1, 9, tzinfo=TZ),
        }
        missing = {
            **ford, "normalized_plate": "34MISSING", "plate": "34MISSING",
            "raw_points": 0, "cache_ready": False,
        }
        roster_gap = CacheCoverage(
            1, 0, (day,), ((day, ("roster_coverage",)),),
        )
        _METRIC_CACHE.clear()
        _COVERAGE_CACHE.clear()
        with (
            patch.dict(os.environ, {"EFFICIENCY_PERSIST_METRIC_CACHE": "0"}),
            patch("efficiency_report.psycopg.connect"),
            patch("efficiency_report.load_shapes", return_value={}),
            patch("efficiency_report.load_database_registry", return_value=fixture_registry()),
            patch("efficiency_report.load_daily_rows", side_effect=lambda *_: [dict(ford), dict(missing)]),
            patch("efficiency_report.load_cache_coverage", return_value=roster_gap),
            patch("efficiency_report.iter_daily_gps", return_value=[(
                "34MPM505", points(day, [(0, 2, 2, 60), (4, 2, 2, 61), (8, 2, 2, 62)]),
            )]),
        ):
            result = generate_efficiency_report(
                "read-only", settings(date_from=day, date_to=day)
            )
        present, absent = result["vehicles"]
        self.assertEqual(present["plate"], "34MPM505")
        self.assertEqual(present["score"], 0)
        self.assertEqual((present["position"], present["rank"]), (1, 1))
        self.assertEqual((absent["position"], absent["rank"]), (2, None))
        self.assertIsNone(absent["score"])
        self.assertEqual(result["coverage"]["roster_coverage_days"], [day.isoformat()])
        self.assertTrue(result["coverage"]["score_preliminary"])
        self.assertEqual(result["coverage"]["incomplete_days"], [day.isoformat()])
        book = load_workbook(io.BytesIO(workbook_from_report(result)))
        try:
            sheet = book["Рейтинг ТС"]
            self.assertEqual([sheet.cell(i, 1).value for i in (2, 3)], [1, 2])
            rank_col = next(col for col in range(1, sheet.max_column + 1)
                            if sheet.cell(1, col).value == "Место в рейтинге")
            self.assertEqual([sheet.cell(i, rank_col).value for i in (2, 3)], [1, None])
        finally:
            book.close()

        gps_stale = CacheCoverage(
            1, 0, (day,), ((day, ("roster_coverage", "gps_received")),),
        )
        rows = [dict(ford), dict(missing)]
        apply_cache_coverage(rows, gps_stale)
        self.assertFalse(rows[0]["cache_ready"], "Поздние GPS должны блокировать оценку")
        self.assertFalse(rows[1]["cache_ready"])
        rows = [dict(ford)]
        apply_cache_coverage(rows, CacheCoverage(1, 0, (day,)))
        self.assertFalse(rows[0]["cache_ready"], "Неизвестная причина устаревания блокирует балл")
        rows = [dict(ford)]
        apply_cache_coverage(rows, roster_gap)
        partial = aggregate_report(
            rows, {(day, "34MPM505"): GPSDailyMetrics(points=3)},
            settings(office_zone_codes=["OFFICE"], allowed_zone_codes=["ALLOWED"]),
        )["vehicles"][0]
        self.assertTrue(partial["preliminary"])
        self.assertEqual(partial["data_status"], "Предварительно")

    def test_persistent_metrics_survive_restart_but_invalidate_on_changes(self):
        day = date(2026, 11, 6)
        row = {
            "report_day": day, "normalized_plate": "34TEST01", "plate": "34 TEST 01",
            "company": "TSM", "user_name": "Тест", "model": "FORD KUGA",
            "directorate": "Механизация", "in_roster": True,
            "inside_km": 35, "outside_km": 5, "total_km": 40,
            "raw_points": 3, "cache_ready": True,
            "cache_gps_event_time": datetime(2026, 11, 6, 15, tzinfo=TZ),
            "cache_gps_received_at": datetime(2026, 11, 6, 16, tzinfo=TZ),
            "cache_geofence_updated_at": datetime(2026, 11, 1, 9, tzinfo=TZ),
        }
        track = points(day, [(0, 2, 2, 15), (60, 2.1, 2.1, 15), (120, 2.2, 2.2, 15)])
        registry = fixture_registry()
        registry.version_refs = ("SITE@1",)

        class FakeStore:
            records = {}

            def __init__(self, url):
                self.enabled = True

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def load(self, digests):
                return {key: self.records[key] for key in digests if key in self.records}

            def save(self, records):
                for digest, _day, _plate, payload in records:
                    self.records[digest] = payload
                return len(records)

        _METRIC_CACHE.clear()
        _COVERAGE_CACHE.clear()
        with (
            patch("efficiency_report.psycopg.connect"),
            patch("efficiency_report.load_shapes", return_value={}),
            patch("efficiency_report.load_database_registry", return_value=registry),
            patch("efficiency_report.load_daily_rows", side_effect=lambda conn, conf: [dict(row)]),
            patch("efficiency_report.load_cache_coverage", return_value=CacheCoverage(1, 1, ())) as coverage,
            patch("efficiency_report.iter_daily_gps", return_value=[("34TEST01", track)]) as tracks,
            patch("efficiency_report.DailyMetricStore", FakeStore),
        ):
            base = settings(date_from=day, date_to=day, plates=["34TEST01"])
            cold = generate_efficiency_report("readonly", base)
            self.assertEqual(cold["coverage"]["gps_metric_persisted_writes"], 1)
            self.assertEqual(len(FakeStore.records), 1)
            _METRIC_CACHE.clear()  # Simulated restart of the portal process.
            warm = generate_efficiency_report(
                "readonly", settings(date_from=day, date_to=day, directorates=["Механизация"])
            )
            self.assertEqual(warm["coverage"]["gps_metric_persisted_hits"], 1)
            self.assertEqual(warm["coverage"]["gps_metric_cache_misses"], 0)
            self.assertEqual(warm["vehicles"][0]["moving_minutes"], 2)
            self.assertEqual(tracks.call_count, 1, "Перезапуск не должен перечитывать GPS")
            _METRIC_CACHE.clear()
            altered = generate_efficiency_report(
                "readonly", settings(date_from=day, date_to=day, site_speed_threshold_kmh=55)
            )
            self.assertEqual(altered["coverage"]["gps_metric_persisted_hits"], 0)
            self.assertEqual(tracks.call_count, 2)
            _METRIC_CACHE.clear()
            registry.version_refs = ("SITE@2",)
            generate_efficiency_report("readonly", base)
            self.assertEqual(tracks.call_count, 3, "Версия геозоны должна менять ключ")
            _METRIC_CACHE.clear()
            row["cache_gps_received_at"] += timedelta(seconds=1)
            generate_efficiency_report("readonly", base)
            self.assertEqual(tracks.call_count, 4, "Поздний GPS должен менять ключ")
            _METRIC_CACHE.clear()
            _COVERAGE_CACHE.clear()
            coverage.return_value = CacheCoverage(1, 0, (day,), ((day, ("gps_received",)),))
            stale = generate_efficiency_report("readonly", base)
            self.assertEqual(tracks.call_count, 5, "Устаревший сводный кэш нельзя брать с диска")
            self.assertEqual(stale["coverage"]["gps_metric_persisted_hits"], 0)
            self.assertEqual(stale["coverage"]["gps_metric_persisted_writes"], 0)
            self.assertIsNone(stale["vehicles"][0]["score"])

    def test_stale_or_corrupt_metrics_never_count_as_persistent_hit(self):
        self.assertIsNone(_restore_daily_metrics({"points": 1}))
        malformed = asdict(GPSDailyMetrics(points=3))
        malformed["moving_minutes"] = float("nan")
        self.assertIsNone(_restore_daily_metrics(malformed))
        self.assertIsNone(_restore_daily_metrics({**asdict(GPSDailyMetrics()), "points": True}))
        self.assertEqual(_restore_daily_metrics(asdict(GPSDailyMetrics(points=0))).points, 0)
        self.assertEqual(metric_digest((date(2026, 11, 6), "34TEST01")),
                         metric_digest((date(2026, 11, 6), "34TEST01")))
        self.assertNotEqual(metric_digest((date(2026, 11, 6), "34TEST01")),
                            metric_digest((date(2026, 11, 6), "34TEST02")))

    def test_historical_gps_metrics_reused_across_filter_changes(self):
        day=date(2026,11,6)
        base_row={
            "report_day":day,"normalized_plate":"34TEST01","plate":"34 TEST 01",
            "company":"TSM","user_name":"Тест","model":"FORD KUGA",
            "directorate":"Механизация","in_roster":True,
            "inside_km":35,"outside_km":5,"total_km":40,
            "raw_points":3,"cache_ready":True,
            "cache_gps_event_time":datetime(2026,11,6,15,tzinfo=TZ),
            "cache_gps_received_at":datetime(2026,11,6,16,tzinfo=TZ),
            "cache_geofence_updated_at":datetime(2026,11,1,9,tzinfo=TZ),
        }
        track=points(day,[(0,2,2,15),(60,2.1,2.1,15),(120,2.2,2.2,15)])
        _METRIC_CACHE.clear()
        _COVERAGE_CACHE.clear()
        with (
            patch("efficiency_report.psycopg.connect"),
            patch("efficiency_report.load_shapes",return_value={}),
            patch("efficiency_report.load_database_registry",return_value=fixture_registry()),
            patch("efficiency_report.load_daily_rows",side_effect=lambda conn,conf:[dict(base_row)]),
            patch("efficiency_report.load_cache_coverage",return_value=CacheCoverage(1,1,())) as coverage,
            patch("efficiency_report.iter_daily_gps",return_value=[("34TEST01",track)]) as tracks,
        ):
            result1=generate_efficiency_report("readonly",settings(date_to=day,plates=["34TEST01"]))
            result2=generate_efficiency_report("readonly",settings(date_to=day))
            self.assertEqual(result1["vehicles"][0]["moving_minutes"],2)
            self.assertEqual(result2["vehicles"][0]["moving_minutes"],2)
            self.assertEqual(result1["coverage"]["gps_metric_cache_misses"],1)
            self.assertEqual(result2["coverage"]["gps_metric_cache_hits"],1)
            self.assertEqual(tracks.call_count,1,"Фильтр не должен вызывать повторное чтение GPS")
            self.assertEqual(coverage.call_count,1,"Фильтр не должен повторять тяжёлую проверку водяных знаков")
            generate_efficiency_report("readonly",settings(date_to=day,site_speed_threshold_kmh=55))
            self.assertEqual(tracks.call_count,2,"Изменение порога скорости требует нового расчёта")
        _METRIC_CACHE.clear()
        _COVERAGE_CACHE.clear()

    def test_weekend_mileage_does_not_enter_efficiency_totals_or_averages(self):
        rows=[]
        for day,inside,outside in [
            (date(2026,11,6),10,20),(date(2026,11,7),200,100)
        ]:
            rows.append({
                "report_day":day,"normalized_plate":"34TEST01","plate":"34 TEST 01",
                "company":"TSM","user_name":"Тест","model":"FORD TRANSIT",
                "directorate":"Механизация","in_roster":True,
                "inside_km":inside,"outside_km":outside,
                "total_km":inside+outside,"raw_points":3,"cache_ready":True,
            })
        gps={
            (day,"34TEST01"):GPSDailyMetrics(points=3)
            for day in (date(2026,11,6),date(2026,11,7))
        }
        result=aggregate_report(rows,gps,settings())
        car=result["vehicles"][0]
        self.assertEqual(car["inside_km"],10)
        self.assertEqual(car["inside_km_per_day"],10)
        self.assertEqual(car["outside_km_per_day"],20)
        self.assertEqual(car["eligible_days"],1)
        self.assertEqual(car["inside_daily_norm_km"],50)
        self.assertEqual((car["inside_share_percent"],car["outside_share_percent"]),(33.3,66.7))
        self.assertEqual((result["summary"]["inside_share_percent"],result["summary"]["outside_share_percent"]),(33.3,66.7))

    def test_outside_share_is_complement_not_inside_outside_ratio(self):
        day=date(2026,11,6)
        rows=[{
            "report_day":day,"normalized_plate":"34TEST01","plate":"34 TEST 01",
            "company":"TSM","user_name":"Тест","model":"FORD TRANSIT",
            "directorate":"Механизация","in_roster":True,
            "inside_km":958,"outside_km":42,"total_km":1000,
            "raw_points":3,"cache_ready":True,
        }]
        gps={(day,"34TEST01"):GPSDailyMetrics(points=3)}
        result=aggregate_report(rows,gps,settings())
        car=result["vehicles"][0]
        self.assertEqual((car["inside_share_percent"],car["outside_share_percent"]),(95.8,4.2))
        self.assertEqual((result["summary"]["inside_share_percent"],result["summary"]["outside_share_percent"]),(95.8,4.2))
        self.assertGreater(car["inside_to_outside_percent"],2000)
        book=load_workbook(io.BytesIO(workbook_from_report(result)))
        try:
            rating=book["Рейтинг ТС"]
            self.assertEqual(rating.cell(1,12).value,"Доля пробега вне Аккую, %")
            self.assertEqual(rating.cell(2,11).value,95.8)
            self.assertEqual(rating.cell(2,12).value,4.2)
            self.assertEqual(rating.cell(2,27).value,42)
            self.assertEqual(rating.cell(2,27).fill.patternType,None)
        finally:
            book.close()

    def test_high_weekday_average_is_independent_of_partial_norm_days(self):
        days=[date(2026,10,19)+timedelta(days=offset) for offset in range(19)]
        weekdays=[day for day in days if day.weekday()<5]
        self.assertEqual(len(weekdays),15)
        rows=[]
        gps={}
        for plate,model,daily_km in [
            ("34TEST01","FORD TRANSİT",139.4),
            ("34TEST02","FORD TRANSIT",77.6),
        ]:
            for index,day in enumerate(weekdays):
                rows.append({
                    "report_day":day,"normalized_plate":plate,"plate":plate,
                    "company":"TSM","user_name":"Тест","model":model,
                    "directorate":"Механизация","in_roster":True,
                    "inside_km":0 if index==0 else daily_km,"outside_km":0,
                    "total_km":0 if index==0 else daily_km,
                    "raw_points":3,"cache_ready":True,
                })
                gps[(day,plate)]=GPSDailyMetrics(points=3)
        report=aggregate_report(rows,gps,settings())
        vehicles={v["plate"]:v for v in report["vehicles"]}
        for plate,expected_average in [("34TEST01",130.1),("34TEST02",72.4)]:
            car=vehicles[plate]
            self.assertEqual((car["norm_days"],car["eligible_days"]),(14,15))
            self.assertEqual(car["inside_km_per_day"],expected_average)
            self.assertEqual(car["inside_daily_norm_km"],50)
            self.assertGreater(car["inside_km_per_day"],car["inside_daily_norm_km"])
            self.assertEqual((car["inside_share_percent"],car["outside_share_percent"]),(100.0,0.0))
        book=load_workbook(io.BytesIO(workbook_from_report(report)))
        try:
            ranking=book["Рейтинг ТС"]
            self.assertEqual(
                [ranking.cell(line,9).fill.fgColor.rgb for line in (2,3)],
                ["00E0F4E8","00E0F4E8"],
            )
        finally:
            book.close()

    def test_score_components_and_speed_penalty(self):
        self.assertEqual(compute_score(
            eligible_days=10,norm_days=8,inside_km=80,outside_km=20,
            moving_minutes=120,office_minutes=30,outside_allowed_km=10,
            speed_events=2,office_configured=True,allowed_configured=True
        ), 75)
        # Office and permitted-zone scores are ignored until these zones exist.
        self.assertEqual(compute_score(
            10,8,80,20,120,100,40,0,False,False
        ),80)
        self.assertIsNone(compute_score(0,0,0,0,0,0,0,0,False,False))

    def test_unavailable_gps_remains_unranked_but_vehicle_is_visible(self):
        day=date(2026,11,6)
        rows=[{
            "report_day":day,"normalized_plate":"34TEST01","plate":"34 TEST 01",
            "company":"TSM","user_name":"Пользователь","model":"FORD TRANSİT",
            "directorate":"Механизация","in_roster":True,
            "inside_km":0,"outside_km":0,"total_km":0,
            "raw_points":0,"cache_ready":True,
        }]
        report=aggregate_report(rows,{},settings())
        self.assertEqual(report["vehicles"][0]["eligible_days"],1)
        self.assertIsNone(report["vehicles"][0]["score"])
        self.assertEqual(report["vehicles"][0]["position"], 1)
        self.assertIsNone(report["vehicles"][0]["rank"])
        self.assertEqual(report["summary"]["vehicles"],1)
        self.assertEqual(report["coverage"]["gps_missing_vehicles"],["34 TEST 01"])
        self.assertIsNone(report["vehicles"][0]["outside_share_percent"])
        self.assertIsNone(report["summary"]["outside_share_percent"])
        rows[0]["cache_ready"]=False
        rows[0]["raw_points"]=3
        gps={(day,"34TEST01"):GPSDailyMetrics(points=3,moving_minutes=12)}
        report=aggregate_report(rows,gps,settings())
        self.assertIsNone(report["vehicles"][0]["score"])

    def test_excel_sheets_and_highlight(self):
        day=date(2026,11,6)
        rows=[{
            "report_day":day,"normalized_plate":"34TEST01","plate":"34 TEST 01",
            "company":"TSM","user_name":"Пользователь","model":"FORD TRANSİT",
            "directorate":"Механизация","in_roster":True,
            "inside_km":10,"outside_km":30,"total_km":40,
            "raw_points":3,"cache_ready":True,
        }]
        gps={(day,"34TEST01"):GPSDailyMetrics(points=3, moving_minutes=20,speed_site=1)}
        result=aggregate_report(rows,gps,settings())
        data=workbook_from_report(result)
        book=load_workbook(io.BytesIO(data))
        try:
            self.assertEqual(book.sheetnames,[
                "Рейтинг ТС","По дням","Превышения скорости","ТОП-10","Методика"
            ])
            self.assertEqual(book.worksheets[0].cell(2,2).value,"34 TEST 01")
            self.assertEqual(book.worksheets[0].cell(1,9).value,"Средний пробег внутри Аккую, км/плановый рабочий день")
            self.assertEqual(book.worksheets[0].cell(1,26).value,"Норматив внутри Аккую по текущей модели, км/плановый рабочий день")
            self.assertEqual(book.worksheets[0].cell(1,27).value,"Средний пробег вне Аккую за рабочий день, км (без норматива)")
            self.assertEqual(book.worksheets[0].cell(1,13).value,"Среднее время движения внутри Аккую за плановый рабочий день, чч:мм")
            self.assertEqual(book.worksheets[0].cell(2,13).value,timedelta(minutes=20))
            self.assertEqual(book["По дням"].cell(2,10).value,timedelta(minutes=20))
            self.assertEqual(book.worksheets[0].cell(2,13).number_format,"[hh]:mm")
            self.assertEqual(book["По дням"].cell(2,10).number_format,"[hh]:mm")
            self.assertEqual(book.worksheets[0].cell(2,8).value,10)
            self.assertEqual(book.worksheets[0].cell(2,9).value,10)
            self.assertEqual(book.worksheets[0].cell(2,26).value,50)
            self.assertEqual(book.worksheets[0].cell(2,9).fill.fgColor.rgb,"00FDE2E2")
            self.assertEqual(book.worksheets[0].cell(2,20).value,1)
            self.assertEqual(book.worksheets[0].cell(2,20).fill.fgColor.rgb,"00FDE2E2")
            self.assertIn("Средний пробег за плановый рабочий день",[row[0].value for row in book["Методика"].iter_rows(min_row=2)])
            self.assertIn("Среднее время движения внутри Аккую за плановый рабочий день",[row[0].value for row in book["Методика"].iter_rows(min_row=2)])
            self.assertIn("Среднее время офисных стоянок за плановый рабочий день",[row[0].value for row in book["Методика"].iter_rows(min_row=2)])
            self.assertIn("Превышения скорости вне площадки",[row[0].value for row in book["Методика"].iter_rows(min_row=2)])
        finally:
            book.close()

    def test_excel_times_keep_numeric_durations_over_24_hours_and_speed_seconds(self):
        day = date(2026, 11, 6)
        rows = [{
            "report_day": day, "normalized_plate": "34TEST01", "plate": "34 TEST 01",
            "company": "TSM", "user_name": "Тест", "model": "FIAT EGEA",
            "directorate": "Механизация", "in_roster": True,
            "inside_km": 40, "outside_km": 2, "cache_ready": True,
        }]
        gps = {(day, "34TEST01"): GPSDailyMetrics(
            points=3, moving_minutes=125, office_parking_minutes=1565,
        )}
        report = aggregate_report(rows, gps, settings(
            date_from=day, date_to=day, office_zone_codes=["OFFICE"],
        ))
        report["speed_events"] = [{
            "plate": "34 TEST 01", "place": "На площадке",
            "start": "2026-11-06T08:23:59+00:00",
            "finish": "2026-11-06T08:24:36+00:00",
            "duration_seconds": 37, "max_speed_kmh": 62, "threshold_kmh": 50,
            "latitude": 36.2, "longitude": 33.1,
        }]
        book = load_workbook(io.BytesIO(workbook_from_report(report)))
        try:
            rating, daily, speed = (
                book["Рейтинг ТС"], book["По дням"], book["Превышения скорости"]
            )
            for cell, expected in [
                (rating.cell(2, 13), timedelta(hours=2, minutes=5)),
                (rating.cell(2, 14), timedelta(hours=26, minutes=5)),
                (rating.cell(2, 15), timedelta(hours=26, minutes=5)),
                (daily.cell(2, 10), timedelta(hours=2, minutes=5)),
                (daily.cell(2, 11), timedelta(hours=26, minutes=5)),
            ]:
                self.assertEqual(cell.value, expected)
                self.assertEqual(cell.number_format, "[hh]:mm")
            self.assertEqual(speed.cell(2, 3).value, datetime(2026, 11, 6, 11, 23, 59))
            self.assertEqual(speed.cell(2, 4).value, datetime(2026, 11, 6, 11, 24, 36))
            self.assertEqual(speed.cell(2, 3).number_format, "dd.mm.yyyy hh:mm")
            self.assertEqual(speed.cell(2, 4).number_format, "dd.mm.yyyy hh:mm")
            self.assertEqual(speed.cell(2, 5).value, 37)
            self.assertIn("Длительность коротких превышений остаётся в секундах", [
                row[1].value for row in book["Методика"].iter_rows(min_row=2)
                if row[0].value == "Формат времени в Excel"
            ][0])
        finally:
            book.close()

    def test_excel_missing_time_values_are_blank_not_zero(self):
        day = date(2026, 11, 6)
        rows = [{
            "report_day": day, "normalized_plate": "34NOGPS", "plate": "34NOGPS",
            "company": "TSM", "user_name": "Тест", "model": "FIAT EGEA",
            "directorate": "Механизация", "in_roster": True,
            "inside_km": 0, "outside_km": 0, "cache_ready": True,
        }]
        report = aggregate_report(rows, {}, settings(date_from=day, date_to=day))
        book = load_workbook(io.BytesIO(workbook_from_report(report)))
        try:
            for column in (13, 14, 15):
                cell = book["Рейтинг ТС"].cell(2, column)
                self.assertIsNone(cell.value)
                self.assertEqual(cell.number_format, "[hh]:mm")
            # The existing daily data contract uses zero for a missing GPS day.
            for column in (10, 11):
                cell = book["По дням"].cell(2, column)
                self.assertEqual(cell.value, timedelta(0))
                self.assertEqual(cell.number_format, "[hh]:mm")
        finally:
            book.close()

    def test_top_ten_export_uses_highest_and_lowest_scores_without_missing(self):
        day = date(2026, 11, 6)
        rows = [{
            "report_day": day, "normalized_plate": f"34CAR{i:02d}", "plate": f"34CAR{i:02d}",
            "company": "TSM", "user_name": "Тест", "model": "FORD TRANSIT",
            "directorate": "Механизация", "in_roster": True,
            "inside_km": 0, "outside_km": 0, "total_km": 0,
            "raw_points": 3, "cache_ready": True,
        } for i in range(13)]
        rows.append({**rows[0], "normalized_plate": "34NO", "plate": "34NO"})
        gps = {(day, f"34CAR{i:02d}"): GPSDailyMetrics(points=3) for i in range(13)}
        report = aggregate_report(rows, gps, settings(date_from=day, date_to=day))
        for vehicle in report["vehicles"]:
            if vehicle["plate"] != "34NO":
                vehicle["score"] = int(vehicle["plate"][-2:])
        # Verify export is independent of the incoming vehicle order.
        report["vehicles"].reverse()
        book = load_workbook(io.BytesIO(workbook_from_report(report)))
        try:
            leaders = book["ТОП-10"]
            self.assertEqual(leaders.max_row, 21)
            self.assertEqual([leaders.cell(i, 3).value for i in range(2, 12)],
                             [f"34CAR{i:02d}" for i in range(12, 2, -1)])
            self.assertEqual([leaders.cell(i, 3).value for i in range(12, 22)],
                             [f"34CAR{i:02d}" for i in range(10)])
            self.assertTrue(all(leaders.cell(i, 3).value != "34NO" for i in range(2, 22)))
        finally:
            book.close()

    def test_excel_roster_values_cannot_become_formulas(self):
        day=date(2026,11,6)
        formula='=HYPERLINK("https://example.invalid/","click")'
        rows=[{
            "report_day":day,"normalized_plate":"34TEST01","plate":formula,
            "company":"TSM","user_name":formula,"model":formula,
            "directorate":formula,"in_roster":True,
            "inside_km":35,"outside_km":5,"total_km":40,
            "raw_points":3,"cache_ready":True,
        }]
        gps={(day,"34TEST01"):GPSDailyMetrics(points=3,moving_minutes=20)}
        configuration=settings(users=[formula])
        result=aggregate_report(rows,gps,configuration)
        book=load_workbook(io.BytesIO(workbook_from_report(result)))
        try:
            rating=book["Рейтинг ТС"]
            daily=book["По дням"]
            for column in (2,3,4,5):
                self.assertEqual(rating.cell(2,column).value,formula)
                self.assertEqual(rating.cell(2,column).data_type,"s")
            for column in (2,3,4,5):
                self.assertEqual(daily.cell(2,column).data_type,"s")
            for sheet in book.worksheets:
                for row in sheet:
                    self.assertTrue(all(cell.data_type!="f" for cell in row))
        finally:
            book.close()


class PortalRouteTest(unittest.TestCase):
    def test_editor_uses_relative_api_urls_below_protected_prefix(self):
        editor = (Path(__file__).parent / "web" / "geofence_editor.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("fetch('api/geofences')", editor)
        self.assertIn("fetch('api/config')", editor)
        self.assertIn("`api/geofences/${currentId}`", editor)
        self.assertNotIn("fetch('/api/geofences", editor)
        self.assertNotIn("fetch('/api/config", editor)

    def test_geozone_access_is_a_separate_nginx_basic_auth_boundary(self):
        nginx = (Path(__file__).parent / "deploy" / "nginx" / "bdd-geozones.conf.example").read_text(
            encoding="utf-8"
        )
        self.assertIn("location ^~ /geozones/ {", nginx)
        self.assertIn("auth_basic_user_file /etc/nginx/.htpasswd_bdd_geozones_maksim;", nginx)
        self.assertIn("proxy_pass http://127.0.0.1:18083/;", nginx)
        self.assertIn("location = /api/efficiency/admin-tools {", nginx)
        self.assertIn("proxy_set_header X-Arvento-Authenticated-User $remote_user;", nginx)
        self.assertNotIn("auth_basic off;", nginx)

    def test_admin_menu_hidden_unless_backend_allows_it(self):
        html = (Path(__file__).parent / "web" / "efficiency_report.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('id="geozonesNav" href="/geozones/" hidden', html)
        self.assertIn("if(permissions?.can_edit_geozones)", html)
        self.assertIn("nav a[hidden]{display:none!important}", html)

    def test_visible_number_is_independent_of_score_rank(self):
        html = (Path(__file__).parent / "web" / "efficiency_report.html").read_text(encoding="utf-8")
        self.assertIn("(i+1)+'. '+esc(r.plate)", html)
        self.assertNotIn("r.rank||'—'", html)

    def test_all_table_columns_are_sortable_with_distinct_office_metrics(self):
        html = (Path(__file__).parent / "web" / "efficiency_report.html").read_text(encoding="utf-8")
        for field in (
            "plate", "directorate", "norm_days", "inside_km_per_day", "outside_km_per_day",
            "inside_share_percent", "moving_minutes_per_day", "office_parking_minutes",
            "office_parking_minutes_per_day", "lunch", "weekend_site_days", "unauthorized_km",
            "speed_events", "score",
        ):
            self.assertIn(f'data-sort="{field}"', html)
        self.assertEqual(html.count('data-sort="'), 14)
        self.assertIn("sortedVehicles(report.vehicles)", html)
        self.assertIn("th.setAttribute('aria-sort'", html)
        self.assertIn('colspan="14"', html)

    def test_outside_speed_scope_is_explained_to_operators(self):
        html = (Path(__file__).parent / "web" / "efficiency_report.html").read_text(encoding="utf-8")
        self.assertIn("только на маршруте Ташуджу — Аккую", html)
        self.assertIn("Нарушения скорости вне площадки учитываются только", html)

    def test_average_badge_uses_its_own_norm_and_labels_are_explicit(self):
        html = (Path(__file__).parent / "web" / "efficiency_report.html").read_text(encoding="utf-8")
        self.assertIn("insideAverageClass(r.inside_km_per_day,averageNorm)", html)
        self.assertIn("normDaysClass(r.norm_days,r.eligible_days,r.gps_workday_days)", html)
        self.assertNotIn("r.norm_days===r.eligible_days?'good':'bad'", html)
        self.assertIn("Средний пробег внутри Аккую", html)
        self.assertIn("норма: от ", html)
        self.assertIn("доля времени в движении", html)
        self.assertIn("r.outside_share_percent", html)
        self.assertIn("норматив не установлен", html)
        self.assertNotIn("r.inside_to_outside_percent", html)

    def test_office_parking_traffic_light_uses_weekday_scaled_thresholds(self):
        html = (Path(__file__).parent / "web" / "efficiency_report.html").read_text(encoding="utf-8")
        self.assertIn("totalMinutes/days<=150?'good':totalMinutes/days<=240?'mid':'bad'", html)
        self.assertIn("parkingClass(r.office_parking_minutes,r.eligible_days)", html)
        self.assertIn("parkingClass(r.office_parking_minutes_per_day)", html)
        self.assertIn("Выходные (суббота и воскресенье) участвуют только", html)

    def test_latest_roster_toggle_is_available_to_all_users_and_sent_to_both_apis(self):
        html = (Path(__file__).parent / "web" / "efficiency_report.html").read_text(encoding="utf-8")
        self.assertIn('type="checkbox" id="latestRosterOnly"', html)
        self.assertIn("Машины только из свежей разнарядки", html)
        self.assertIn("'&latest_roster_only='+$('latestRosterOnly').checked", html)
        self.assertIn("latest_roster_only:$('latestRosterOnly').checked", html)
        self.assertIn("reloadOptions({pruneSelections:true})", html)
        self.assertIn("$('latestRosterOnly').checked=false", html)
        self.assertIn("data.latest_roster_day", html)

    def test_page_jobs_and_excel_routes_are_registered_once(self):
        app = FastAPI()
        apply_efficiency_portal(app, lambda: "not-used-during-registration")
        apply_efficiency_portal(app, lambda: "not-used-during-registration")
        paths = [route.path for route in app.routes]
        for path in (
            "/efficiency", "/api/efficiency/admin-tools",
            "/api/efficiency/options", "/api/efficiency/jobs",
            "/api/efficiency/jobs/{job_id}",
            "/api/efficiency/jobs/{job_id}/result",
            "/api/efficiency/jobs/{job_id}/excel",
        ):
            self.assertEqual(paths.count(path), 1)
        page = next(route for route in app.routes if route.path == "/efficiency")
        self.assertTrue(page.endpoint().path.is_file())
        admin = next(
            route.endpoint for route in app.routes
            if route.path == "/api/efficiency/admin-tools"
        )
        self.assertEqual(admin(None).body, b'{"can_edit_geozones":false}')
        self.assertEqual(admin("another-user").body, b'{"can_edit_geozones":false}')
        self.assertEqual(admin("maksim").body, b'{"can_edit_geozones":true}')
        self.assertEqual(admin("maksim").headers["cache-control"], "private, no-store")

        options = next(
            route.endpoint for route in app.routes
            if route.path == "/api/efficiency/options"
        )
        with patch("efficiency_portal.filter_options", return_value={}) as fetch:
            options(date(2026, 11, 2), date(2026, 11, 7), True)
        self.assertTrue(fetch.call_args.kwargs["latest_roster_only"])

    def test_background_job_result_and_excel(self):
        app = FastAPI()
        apply_efficiency_portal(app, lambda: "read-only-database")
        endpoints = {route.path: route.endpoint for route in app.routes}
        conf = settings(date_to=date(2026,11,6))
        sample = aggregate_report([], {}, conf)

        async def lifecycle():
            started = await endpoints["/api/efficiency/jobs"](conf)
            self.assertEqual(started["status"], "queued")
            job_id = started["job_id"]
            for _ in range(30):
                state = await endpoints["/api/efficiency/jobs/{job_id}"](job_id)
                if state["status"] in {"succeeded", "failed"}:
                    break
                await asyncio.sleep(.01)
            self.assertEqual(state["status"], "succeeded")
            duplicate = await endpoints["/api/efficiency/jobs"](conf)
            self.assertEqual(duplicate["job_id"], job_id)
            result = await endpoints["/api/efficiency/jobs/{job_id}/result"](job_id)
            self.assertEqual(result["summary"], sample["summary"])
            excel = await endpoints["/api/efficiency/jobs/{job_id}/excel"](job_id)
            self.assertEqual(excel.status_code, 200)
            self.assertTrue(excel.body.startswith(b"PK"))

        with patch("efficiency_portal.generate_efficiency_report", return_value=sample):
            asyncio.run(lifecycle())

    def test_latest_roster_job_does_not_reuse_completed_historical_result(self):
        from efficiency_portal import _JOBS

        app = FastAPI()
        apply_efficiency_portal(app, lambda: "read-only-database")
        endpoints = {route.path: route.endpoint for route in app.routes}
        conf = settings(date_to=date(2026, 11, 6), latest_roster_only=True)
        sample = aggregate_report([], {}, conf)

        async def lifecycle():
            first = await endpoints["/api/efficiency/jobs"](conf)
            for _ in range(30):
                state = await endpoints["/api/efficiency/jobs/{job_id}"](first["job_id"])
                if state["status"] in {"succeeded", "failed"}:
                    break
                await asyncio.sleep(.01)
            self.assertEqual(state["status"], "succeeded")
            second = await endpoints["/api/efficiency/jobs"](conf)
            self.assertNotEqual(first["job_id"], second["job_id"])
            for _ in range(30):
                state = await endpoints["/api/efficiency/jobs/{job_id}"](second["job_id"])
                if state["status"] in {"succeeded", "failed"}:
                    break
                await asyncio.sleep(.01)
            self.assertEqual(state["status"], "succeeded")

        with patch("efficiency_portal.generate_efficiency_report", return_value=sample):
            asyncio.run(lifecycle())
        for job_id, job in list(_JOBS.items()):
            if job.settings == conf:
                _JOBS.pop(job_id, None)


class PersistenceBoundaryTest(unittest.TestCase):
    def test_jsonb_lookup_and_upsert_are_parameterized(self):
        key = metric_digest(("verified", date(2026, 11, 6)))
        payload = asdict(GPSDailyMetrics(points=3, moving_minutes=2))
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [(key, payload)]
        with (
            patch("efficiency_metric_cache.psycopg.connect", return_value=connection),
            patch.dict(os.environ, {"EFFICIENCY_PERSIST_METRIC_CACHE": "1"}),
        ):
            with DailyMetricStore("readonly") as store:
                self.assertEqual(store.load([key]), {key: payload})
                self.assertEqual(store.save([(key, date(2026, 11, 6), "34TEST01", payload)]), 1)
        query, args = cursor.execute.call_args.args
        self.assertIn("cache_key = ANY(%s::text[])", query)
        self.assertEqual(args, ([key],))
        statement, records = cursor.executemany.call_args.args
        self.assertIn("ON CONFLICT (report_day, normalized_plate)", statement)
        self.assertEqual(records[0][:3], (key, date(2026, 11, 6), "34TEST01"))
        self.assertEqual(connection.commit.call_count, 2)

    def test_database_cache_failure_falls_back_without_writing(self):
        import psycopg

        with (
            patch("efficiency_metric_cache.psycopg.connect", side_effect=psycopg.OperationalError("offline")),
            patch.dict(os.environ, {"EFFICIENCY_PERSIST_METRIC_CACHE": "1"}),
        ):
            with DailyMetricStore("readonly") as store:
                self.assertEqual(store.load(["0" * 64]), {})
                self.assertFalse(store.enabled)
                self.assertEqual(store.save([("0" * 64, date(2026, 11, 6), "34TEST01", {})]), 0)

    def test_warmup_skips_incomplete_cache_before_gps(self):
        import efficiency_cache_worker

        with (
            patch.dict(os.environ, {"DATABASE_URL": "readonly", "EFFICIENCY_PERSIST_METRIC_CACHE": "1"}),
            patch.object(sys, "argv", ["efficiency_cache_worker.py", "--date", "2026-11-06"]),
            patch("efficiency_cache_worker.psycopg.connect"),
            patch("efficiency_cache_worker.load_cache_coverage", return_value=CacheCoverage(
                1, 0, (date(2026, 11, 6),),
                ((date(2026, 11, 6), ("gps_received",)),),
            )),
            patch("efficiency_cache_worker.generate_efficiency_report") as generate,
        ):
            self.assertEqual(efficiency_cache_worker.main(), 1)
            generate.assert_not_called()

    def test_warmup_reports_failure_if_persistent_store_is_unavailable(self):
        import efficiency_cache_worker

        result = {
            "summary": {"vehicles": 2},
            "coverage": {
                "incomplete_days": [], "provisional_days": [],
                "gps_metric_cache_hits": 0, "gps_metric_cache_misses": 2,
                "gps_metric_persisted_hits": 0, "gps_metric_persisted_writes": 0,
                "gps_metric_persisted_available": False,
            },
        }
        with (
            patch.dict(os.environ, {"DATABASE_URL": "readonly", "EFFICIENCY_PERSIST_METRIC_CACHE": "1"}),
            patch.object(sys, "argv", ["efficiency_cache_worker.py", "--date", "2026-11-06"]),
            patch("efficiency_cache_worker.psycopg.connect"),
            patch("efficiency_cache_worker.load_cache_coverage", return_value=CacheCoverage(1, 1, ())),
            patch("efficiency_cache_worker.generate_efficiency_report", return_value=result),
        ):
            self.assertEqual(efficiency_cache_worker.main(), 1)


if __name__=="__main__":
    unittest.main()
