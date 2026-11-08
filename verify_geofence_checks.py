#!/usr/bin/env python3
"""Offline regression checks for independent geofence purposes and controls."""
from __future__ import annotations

import os
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

# Import the editor without requiring a production connection or credentials.
with patch.dict(os.environ, {"DATABASE_URL": "postgresql://localhost/checks"}):
    from geofence_editor_api import (
        GeofenceChecks, GeofencePayload, default_geofence_checks,
        stored_geofence_checks, validate_geofence_role,
    )

from efficiency_report import choose_shapes, filter_options, shape_from_geojson
from geozone_registry import Geozone, Registry, is_speed_excluded_zone, point_in_zone, _polygon_parts
from site_boundary_speed import detect_speed_violations_by_polygon
from verify_efficiency_report import points, polygon


class EditorControlsTest(unittest.TestCase):
    def test_legacy_clients_and_tunnel_defaults(self) -> None:
        legacy = GeofencePayload(
            code="TUNNEL", name="Тоннель", geofence_type="SPEED_EXCLUSION",
            geometry=polygon(1, 1, 2, 2),
        )
        self.assertIsNone(legacy.checks)
        self.assertEqual(default_geofence_checks("SPEED_EXCLUSION").speed_rule, "exclude")
        self.assertEqual(default_geofence_checks("OFFICE").speed_rule, "inherit")
        self.assertEqual(stored_geofence_checks((True, True, "limit", 25.0)).speed_threshold_kmh, 25)

    def test_speed_mode_requires_valid_threshold(self) -> None:
        self.assertEqual(
            GeofenceChecks(parking_enabled=True, allowed_area_enabled=True,
                           speed_rule="limit", speed_threshold_kmh=30).speed_rule,
            "limit",
        )
        for config in (
            {"speed_rule": "limit"},
            {"speed_rule": "exclude", "speed_threshold_kmh": 30},
            {"speed_rule": "limit", "speed_threshold_kmh": 300},
            {"speed_rule": "limit", "speed_threshold_kmh": float("nan")},
            {"speed_rule": "wrong"},
        ):
            with self.subTest(config=config), self.assertRaises(ValidationError):
                GeofenceChecks(**config)

    def test_enabled_polygon_checks_rejected_on_gate(self) -> None:
        def payload(checks: GeofenceChecks) -> GeofencePayload:
            return GeofencePayload(code="GATE", name="КПП", geofence_type="GATE",
                                   geometry={"type": "LineString", "coordinates": [[1, 1], [2, 2]]},
                                   checks=checks)

        cursor = MagicMock()
        self.assertEqual(validate_geofence_role(cursor, payload(GeofenceChecks()), "LINESTRING"), "GATE")
        for flags in ({"parking_enabled": True}, {"allowed_area_enabled": True},
                      {"speed_rule": "exclude"}):
            with self.subTest(flags=flags), self.assertRaises(HTTPException):
                validate_geofence_role(cursor, payload(GeofenceChecks(**flags)), "LINESTRING")

    def test_editor_has_independent_controls(self) -> None:
        html = (Path(__file__).parent / "web" / "geofence_editor.html").read_text(encoding="utf-8")
        for field in ("parkingEnabled", "allowedAreaEnabled", "speedRule", "speedLimit"):
            self.assertIn(f'id="{field}"', html)
        self.assertIn("speed_threshold_kmh", html)

    def test_google_layer_uses_one_map_not_one_map_per_tile(self) -> None:
        html = (Path(__file__).parent / "web" / "geofence_editor.html").read_text(encoding="utf-8")
        self.assertEqual(html.count("new google.maps.Map("), 1)
        self.assertNotIn("googleLayer.createTile", html)
        self.assertNotIn("L.gridLayer(", html)
        self.assertIn("map.on('move zoom resize', scheduleGoogleSync)", html)
        self.assertIn("window.gm_authFailure", html)


class EfficiencyControlsTest(unittest.TestCase):
    def test_any_purpose_can_be_office_and_allowed(self) -> None:
        custom = shape_from_geojson(
            "CUSTOM", "Склад", "FORBIDDEN_AREA", polygon(1, 1, 2, 2),
            parking_enabled=True, allowed_area_enabled=True,
        )
        ordinary = shape_from_geojson("ORDINARY", "Полигон", "ROAD_CORRIDOR", polygon(3, 3, 4, 4))
        legacy_office = shape_from_geojson("OFFICE", "Офис", "OFFICE", polygon(1, 1, 2, 2))
        self.assertEqual(choose_shapes({"CUSTOM": custom}, ["CUSTOM"], "стоянок"), [custom])
        self.assertEqual(choose_shapes({"CUSTOM": custom}, ["CUSTOM"], "разрешённого движения"), [custom])
        self.assertEqual(choose_shapes({"OFFICE": legacy_office}, ["OFFICE"], "стоянок"), [legacy_office])
        with self.assertRaises(ValueError):
            choose_shapes({"ORDINARY": ordinary}, ["ORDINARY"], "стоянок")

    def test_defaults_offer_enabled_zones_but_preserve_manual_selection(self) -> None:
        shapes = {
            "BOTH": shape_from_geojson(
                "BOTH", "Офис в рабочей зоне", "WORK_ZONE", polygon(1, 1, 2, 2),
                parking_enabled=True, allowed_area_enabled=True,
            ),
            "TUNNEL": shape_from_geojson(
                "TUNNEL", "Особая зона", "SPEED_EXCLUSION", polygon(3, 3, 4, 4),
                speed_rule="exclude",
            ),
        }
        connection = MagicMock()
        with patch("efficiency_report.psycopg.connect") as connect, \
             patch("efficiency_report.load_daily_rows", return_value=[]), \
             patch("efficiency_report.load_shapes", return_value=shapes):
            connect.return_value.__enter__.return_value = connection
            result = filter_options("postgresql://unused", date(2026, 11, 1), date(2026, 11, 7))
        self.assertEqual(result["defaults"]["office_zone_codes"], ["BOTH"])
        self.assertEqual(result["defaults"]["allowed_zone_codes"], ["BOTH"])
        self.assertEqual(result["zones"][0]["speed_rule"], "inherit")
        self.assertEqual(result["speed_exclusion_zones"], [{"code": "TUNNEL", "name": "Особая зона"}])


class SpeedControlsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.site = Geozone("Аккую", "polygon", "site_boundary",
                            points=[(0, 0), (0, 10), (10, 10), (10, 0)])
        self.speeds = points(date(2026, 11, 6), [(0, 2, 2, 40), (4, 2, 2, 41), (8, 2, 2, 42)])

    def detect(self, zones: list[Geozone]) -> int:
        found, _ = detect_speed_violations_by_polygon(
            self.speeds, Registry(gates=[], zones=[self.site, *zones]),
            site_threshold_kmh=50, outside_threshold_kmh=103, min_duration_seconds=3,
        )
        return len(found)

    def test_custom_limit_works_without_changing_zone_purpose(self) -> None:
        office = Geozone("Офис", "polygon", "office",
                          points=[(1, 1), (1, 3), (3, 3), (3, 1)],
                          speed_rule="limit", speed_threshold_kmh=30)
        self.assertEqual(self.detect([]), 0)
        self.assertEqual(self.detect([office]), 1)

    def test_any_zone_can_exclude_speed_even_if_limit_overlaps(self) -> None:
        limit = Geozone("Лимит", "polygon", "work_zone",
                        points=[(1, 1), (1, 3), (3, 3), (3, 1)],
                        speed_rule="limit", speed_threshold_kmh=30)
        parking = Geozone("Офисное исключение", "polygon", "office",
                          points=[(1, 1), (1, 3), (3, 3), (3, 1)], speed_rule="exclude")
        self.assertTrue(is_speed_excluded_zone(parking))
        self.assertEqual(self.detect([limit, parking]), 0)

    def test_explicit_inherit_can_disable_legacy_tunnel_exclusion(self) -> None:
        polygon_points = [(1, 1), (1, 3), (3, 3), (3, 1)]
        legacy = Geozone("Исторический тоннель", "polygon", "speed_exclusion",
                         points=polygon_points)
        renamed = Geozone("Тоннель с контролем", "polygon", "speed_exclusion",
                          points=polygon_points, speed_rule="inherit")
        self.assertTrue(is_speed_excluded_zone(legacy))
        self.assertFalse(is_speed_excluded_zone(renamed))
        self.assertEqual(self.detect([legacy]), 0)

    def test_hole_in_geofence_does_not_suppress_speed_inside_hole(self) -> None:
        exterior = [(1, 1), (1, 4), (4, 4), (4, 1)]
        hole = [(1.5, 1.5), (1.5, 2.5), (2.5, 2.5), (2.5, 1.5)]
        exclusion = Geozone("Тоннель с отверстием", "polygon", "speed_exclusion",
                            points=exterior, holes=[hole], speed_rule="exclude")
        self.assertFalse(point_in_zone(2, 2, exclusion))
        self.assertTrue(point_in_zone(3, 3, exclusion))
        parts = _polygon_parts({"type": "Polygon", "coordinates": [
            [[lon, lat] for lat, lon in [*exterior, exterior[0]]],
            [[lon, lat] for lat, lon in [*hole, hole[0]]],
        ]})
        self.assertEqual(parts, [(exterior, [hole])])
        limit = Geozone("Лимит", "polygon", "office", points=exterior,
                        speed_rule="limit", speed_threshold_kmh=30)
        self.assertEqual(self.detect([limit, exclusion]), 1)


if __name__ == "__main__":
    unittest.main()
