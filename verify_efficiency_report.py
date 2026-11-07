#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline acceptance tests for the new Arvento efficiency portal."""
from __future__ import annotations

import io
import asyncio
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch
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
    aggregate_report,
    analyze_daily_track,
    choose_shapes,
    compute_score,
    generate_efficiency_report,
    is_transit,
    shape_from_geojson,
    workbook_from_report,
)
from cache_freshness import CacheCoverage
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
        self.assertTrue(is_transit("FORD TRANSİT"))
        self.assertTrue(is_transit("Ford Transit Custom"))
        self.assertFalse(is_transit("FORD Tourneo Courier"))

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

    def test_weekend_mileage_does_not_inflate_weekday_average(self):
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
        self.assertEqual(car["inside_km"],210)
        self.assertEqual(car["inside_km_per_day"],10)
        self.assertEqual(car["outside_km_per_day"],20)
        self.assertEqual(car["eligible_days"],1)

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
        self.assertEqual(report["summary"]["vehicles"],1)
        self.assertEqual(report["coverage"]["gps_missing_vehicles"],["34 TEST 01"])
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
                "Рейтинг ТС","По дням","Превышения скорости","Методика"
            ])
            self.assertEqual(book.worksheets[0].cell(2,2).value,"34 TEST 01")
            self.assertEqual(book.worksheets[0].cell(2,18).value,1)
            self.assertEqual(book.worksheets[0].cell(2,18).fill.fgColor.rgb,"00FDE2E2")
        finally:
            book.close()


class PortalRouteTest(unittest.TestCase):
    def test_page_jobs_and_excel_routes_are_registered_once(self):
        app = FastAPI()
        apply_efficiency_portal(app, lambda: "not-used-during-registration")
        apply_efficiency_portal(app, lambda: "not-used-during-registration")
        paths = [route.path for route in app.routes]
        for path in (
            "/efficiency", "/api/efficiency/options", "/api/efficiency/jobs",
            "/api/efficiency/jobs/{job_id}",
            "/api/efficiency/jobs/{job_id}/result",
            "/api/efficiency/jobs/{job_id}/excel",
        ):
            self.assertEqual(paths.count(path), 1)
        page = next(route for route in app.routes if route.path == "/efficiency")
        self.assertTrue(page.endpoint().path.is_file())

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


if __name__=="__main__":
    unittest.main()
