#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Source-backed vehicle efficiency calculation for the Arvento report portal.

The consolidated daily cache is the source of mileage and roster coverage.
GPS is read in bounded daily streams only for measures absent from that cache:
movement, office parking, selected-area use, lunch crossings and speed events.
No GPS interval is inferred across long telemetry gaps.
"""
from __future__ import annotations

import io
import json
import math
import threading
import time as clock
import unicodedata
from collections import OrderedDict, defaultdict
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import psycopg
import psycopg.rows
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet
from pydantic import BaseModel, Field, model_validator

from business_rules import (
    DEFAULT_OUTSIDE_SPEED_THRESHOLD_KMH,
    DEFAULT_SITE_SPEED_THRESHOLD_KMH,
    MIN_SPEED_EVENT_DURATION_SECONDS,
)
from cache_freshness import CacheCoverage, load_cache_coverage
from efficiency_metric_cache import DailyMetricStore, metric_digest, persistent_cache_enabled
from geozone_registry import load_database_registry, point_in_polygon
from roster_registry import normalize_plate
from site_boundary_speed import (
    classify_site_state_by_polygon,
    detect_speed_violations_by_polygon,
)
from speed_violation_report import _valid_speed, validate_speed_thresholds


TZ = ZoneInfo("Europe/Istanbul")
MAX_PERIOD_DAYS = 31
MAX_INTERVAL_SECONDS = 300
MAX_OFFICE_GAP_SECONDS = 900
MIN_MOVING_SPEED_KMH = 3.0
MAX_PLAUSIBLE_SEGMENT_KM = 5.0
SPEED_PENALTY_PER_EVENT = 3
SPEED_PENALTY_CAP = 15
SCORE_WEIGHTS = {
    "norm_days": 0.50,
    "office_motion": 0.25,
    "inside_share": 0.15,
    "authorized_use": 0.10,
}
GPS_METRIC_CACHE_TTL_SECONDS = 3600
GPS_METRIC_CACHE_MAX_ENTRIES = 10_000
COVERAGE_CACHE_TTL_SECONDS = 60


class EfficiencySettings(BaseModel):
    date_from: date
    date_to: date
    plates: list[str] = Field(default_factory=list, max_length=1500)
    directorates: list[str] = Field(default_factory=list, max_length=250)
    users: list[str] = Field(default_factory=list, max_length=1500)
    models: list[str] = Field(default_factory=list, max_length=250)
    office_zone_codes: list[str] = Field(default_factory=list, max_length=50)
    allowed_zone_codes: list[str] = Field(default_factory=list, max_length=50)
    lunch_exit_from: time = time(11, 30)
    lunch_exit_to: time = time(12, 30)
    lunch_return_from: time = time(12, 30)
    lunch_return_to: time = time(14, 0)
    transit_min_inside_km: float = Field(default=50.0, ge=0, le=500)
    other_min_inside_km: float = Field(default=30.0, ge=0, le=500)
    parking_min_minutes: float = Field(default=5.0, ge=0, le=240)
    site_speed_threshold_kmh: float = DEFAULT_SITE_SPEED_THRESHOLD_KMH
    outside_speed_threshold_kmh: float = DEFAULT_OUTSIDE_SPEED_THRESHOLD_KMH
    speed_min_duration_seconds: int = Field(
        default=MIN_SPEED_EVENT_DURATION_SECONDS, ge=0, le=3600
    )

    @model_validator(mode="after")
    def validate_scope(self) -> "EfficiencySettings":
        if self.date_to < self.date_from:
            raise ValueError("Дата окончания раньше даты начала")
        if (self.date_to - self.date_from).days + 1 > MAX_PERIOD_DAYS:
            raise ValueError(f"Период не должен превышать {MAX_PERIOD_DAYS} дней")
        if self.lunch_exit_to <= self.lunch_exit_from:
            raise ValueError("Окончание окна выезда на обед должно быть позже начала")
        if self.lunch_return_to <= self.lunch_return_from:
            raise ValueError("Окончание окна возвращения должно быть позже начала")
        validate_speed_thresholds(
            self.site_speed_threshold_kmh,
            self.outside_speed_threshold_kmh,
        )
        self.plates = list(dict.fromkeys(normalize_plate(p) for p in self.plates if p.strip()))
        for attr in ("directorates", "users", "models", "office_zone_codes", "allowed_zone_codes"):
            values = getattr(self, attr)
            setattr(self, attr, list(dict.fromkeys(v.strip() for v in values if v.strip())))
        return self


@dataclass(frozen=True, slots=True)
class Shape:
    code: str
    name: str
    kind: str
    # Each polygon consists of an exterior ring and zero or more holes.
    polygons: tuple[tuple[tuple[tuple[float, float], ...], ...], ...]

    def contains(self, lat: float, lon: float) -> bool:
        for rings in self.polygons:
            if not rings:
                continue
            exterior = rings[0]
            if not point_in_polygon(lat, lon, exterior):
                continue
            if any(point_in_polygon(lat, lon, hole) for hole in rings[1:]):
                continue
            return True
        return False


def shape_from_geojson(code: str, name: str, kind: str, geometry: Mapping[str, Any]) -> Shape | None:
    kind_geo = geometry.get("type")
    if kind_geo == "Polygon":
        polygons = [geometry.get("coordinates") or []]
    elif kind_geo == "MultiPolygon":
        polygons = geometry.get("coordinates") or []
    else:
        return None
    rings_out: list[tuple[tuple[tuple[float, float], ...], ...]] = []
    for poly in polygons:
        rings = []
        for ring in poly:
            points = tuple((float(lat), float(lon)) for lon, lat, *_ in ring)
            if len(points) >= 4:
                rings.append(points)
        if rings:
            rings_out.append(tuple(rings))
    if not rings_out:
        return None
    return Shape(code, name, kind.upper(), tuple(rings_out))


def load_shapes(connection: psycopg.Connection[Any]) -> dict[str, Shape]:
    rows = connection.execute(
        """
        SELECT g.code, g.name, g.geofence_type, ST_AsGeoJSON(v.geometry)::json
        FROM geofences g
        JOIN geofence_versions v ON v.geofence_id=g.id AND v.valid_to IS NULL
        WHERE g.is_active
        ORDER BY g.name
        """
    ).fetchall()
    result: dict[str, Shape] = {}
    for code, name, kind, geom in rows:
        shape = shape_from_geojson(code, name, kind, geom if isinstance(geom, dict) else json.loads(geom))
        if shape:
            result[shape.code] = shape
    return result


def choose_shapes(
    available: Mapping[str, Shape], requested: Sequence[str], purpose: str
) -> list[Shape]:
    missing = sorted(set(requested) - available.keys())
    if missing:
        raise ValueError(f"Геозоны {purpose} не найдены или отключены: {', '.join(missing)}")
    forbidden = [code for code in requested if available[code].kind in {"SPEED_EXCLUSION", "TUNNEL"}]
    if forbidden:
        raise ValueError(f"Тоннельные зоны не могут быть зонами {purpose}: {', '.join(forbidden)}")
    if purpose == "стоянок":
        not_offices = [
            code for code in requested
            if available[code].kind not in {"OFFICE", "PARKING", "WORK_ZONE"}
        ]
        if not_offices:
            raise ValueError(
                "Для офисных стоянок используйте геозоны OFFICE, PARKING или WORK_ZONE: "
                + ", ".join(not_offices)
            )
    return [available[code] for code in requested]


@dataclass(frozen=True, slots=True)
class GPSPoint:
    plate: str
    timestamp: datetime
    lat: float
    lon: float
    speed: float | None
    distance_km: float | None
    address: str = ""


@dataclass(slots=True)
class GPSDailyMetrics:
    moving_minutes: float = 0.0
    office_parking_minutes: float = 0.0
    office_stops: int = 0
    unauthorized_minutes: float = 0.0
    unauthorized_km: float = 0.0
    lunch_exits: int = 0
    lunch_returns: int = 0
    weekend_site_movement: bool = False
    speed_site: int = 0
    speed_outside: int = 0
    speed_events: list[dict[str, Any]] = field(default_factory=list)
    points: int = 0


def _restore_daily_metrics(payload: Mapping[str, Any]) -> GPSDailyMetrics | None:
    """Reject incomplete/corrupt persisted rows instead of trusting old JSON."""
    if not isinstance(payload, dict) or set(payload) != {item.name for item in fields(GPSDailyMetrics)}:
        return None
    integer_fields = (
        "office_stops", "lunch_exits", "lunch_returns", "speed_site", "speed_outside", "points",
    )
    float_fields = (
        "moving_minutes", "office_parking_minutes", "unauthorized_minutes", "unauthorized_km",
    )
    for name in integer_fields:
        value = payload[name]
        if type(value) is not int or value < 0:
            return None
    for name in float_fields:
        value = payload[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            return None
    if type(payload["weekend_site_movement"]) is not bool:
        return None
    events = payload["speed_events"]
    if not isinstance(events, list) or len(events) > 100_000 or any(
        not isinstance(event, dict)
        or not {"plate", "start", "finish", "max_speed_kmh"}.issubset(event)
        for event in events
    ):
        return None
    return GPSDailyMetrics(**payload)


# Derived metrics (not raw tracks) are small enough to retain across requests.
# Keep the key independent of plate/directorate/user/model filters so a change
# to the report selection does not re-read the same closed-day GPS history.
_METRIC_CACHE: OrderedDict[tuple[Any, ...], tuple[float, GPSDailyMetrics]] = OrderedDict()
_METRIC_CACHE_LOCK = threading.Lock()
_COVERAGE_CACHE: OrderedDict[tuple[date, date], tuple[float, CacheCoverage]] = OrderedDict()
_COVERAGE_CACHE_LOCK = threading.Lock()


def _metric_settings_signature(settings: EfficiencySettings) -> tuple[Any, ...]:
    return (
        tuple(sorted(settings.office_zone_codes)),
        tuple(sorted(settings.allowed_zone_codes)),
        settings.lunch_exit_from, settings.lunch_exit_to,
        settings.lunch_return_from, settings.lunch_return_to,
        settings.parking_min_minutes,
        settings.site_speed_threshold_kmh,
        settings.outside_speed_threshold_kmh,
        settings.speed_min_duration_seconds,
    )


def _cached_metric(key: tuple[Any, ...]) -> GPSDailyMetrics | None:
    with _METRIC_CACHE_LOCK:
        item = _METRIC_CACHE.get(key)
        if item is None:
            return None
        loaded_at, metrics = item
        if clock.monotonic() - loaded_at >= GPS_METRIC_CACHE_TTL_SECONDS:
            _METRIC_CACHE.pop(key, None)
            return None
        _METRIC_CACHE.move_to_end(key)
        return metrics


def _remember_metric(key: tuple[Any, ...], metrics: GPSDailyMetrics) -> None:
    with _METRIC_CACHE_LOCK:
        _METRIC_CACHE[key] = (clock.monotonic(), metrics)
        _METRIC_CACHE.move_to_end(key)
        while len(_METRIC_CACHE) > GPS_METRIC_CACHE_MAX_ENTRIES:
            _METRIC_CACHE.popitem(last=False)


def _cached_coverage(
    connection: psycopg.Connection[Any], start: date, finish: date
) -> CacheCoverage:
    """Bound repeated heavy source-watermark scans to once per minute.

    Only historical, completely covered periods qualify; current dates and
    stale sources must be verified anew on every request.
    """
    historical = finish < datetime.now(TZ).date()
    key = (start, finish)
    if historical:
        with _COVERAGE_CACHE_LOCK:
            cached = _COVERAGE_CACHE.get(key)
            if cached and clock.monotonic() - cached[0] < COVERAGE_CACHE_TTL_SECONDS:
                _COVERAGE_CACHE.move_to_end(key)
                return cached[1]
            _COVERAGE_CACHE.pop(key, None)
    coverage = load_cache_coverage(connection, start, finish)
    if historical and coverage.complete:
        with _COVERAGE_CACHE_LOCK:
            _COVERAGE_CACHE[key] = (clock.monotonic(), coverage)
            _COVERAGE_CACHE.move_to_end(key)
            while len(_COVERAGE_CACHE) > 32:
                _COVERAGE_CACHE.popitem(last=False)
    return coverage


def _within_window(timestamp: datetime, start: time, finish: time) -> bool:
    local_time = timestamp.astimezone(TZ).time().replace(tzinfo=None)
    return start <= local_time <= finish


def _valid_interval(first: GPSPoint, second: GPSPoint, max_seconds: int) -> float:
    seconds = (second.timestamp - first.timestamp).total_seconds()
    return seconds if 0 < seconds <= max_seconds else 0.0


def _in_any(point: GPSPoint, shapes: Sequence[Shape]) -> bool:
    return any(shape.contains(point.lat, point.lon) for shape in shapes)


def _valid_distance(value: float | None) -> float:
    if value is None or not math.isfinite(value):
        return 0.0
    return value if 0 <= value <= MAX_PLAUSIBLE_SEGMENT_KM else 0.0


def is_transit(model: str) -> bool:
    """Match FORD TRANSIT and Turkish FORD TRANSİT consistently."""
    normalized = unicodedata.normalize("NFKD", model)
    plain = "".join(c for c in normalized if unicodedata.category(c) != "Mn")
    return "transit" in plain.casefold()


def analyze_daily_track(
    track: Sequence[GPSPoint],
    registry: Any,
    office_shapes: Sequence[Shape],
    allowed_shapes: Sequence[Shape],
    settings: EfficiencySettings,
) -> GPSDailyMetrics:
    metrics = GPSDailyMetrics(points=len(track))
    if not track:
        return metrics
    classified = classify_site_state_by_polygon(track, registry)
    stop_start: GPSPoint | None = None
    stop_last: GPSPoint | None = None

    def close_stop() -> None:
        nonlocal stop_start, stop_last
        if stop_start is not None and stop_last is not None:
            duration = (stop_last.timestamp - stop_start.timestamp).total_seconds()
            if duration > settings.parking_min_minutes * 60:
                metrics.office_parking_minutes += duration / 60
                metrics.office_stops += 1
        stop_start = stop_last = None

    previous: tuple[GPSPoint, bool] | None = None
    for point, inside in classified:
        speed = _valid_speed(point.speed)
        if previous is not None:
            preceding, was_inside = previous
            previous_speed = _valid_speed(preceding.speed)
            dt = _valid_interval(preceding, point, MAX_INTERVAL_SECONDS)
            if dt and was_inside and inside and (
                previous_speed is not None and previous_speed > MIN_MOVING_SPEED_KMH
                and speed is not None and speed > MIN_MOVING_SPEED_KMH
            ):
                metrics.moving_minutes += dt / 60
            # A change of GPS position after a long outage is not proof of a
            # crossing at the timestamp of the first newly received point.
            if dt and was_inside and not inside and _within_window(
                point.timestamp, settings.lunch_exit_from, settings.lunch_exit_to
            ):
                metrics.lunch_exits += 1
            if dt and not was_inside and inside and _within_window(
                point.timestamp, settings.lunch_return_from, settings.lunch_return_to
            ):
                metrics.lunch_returns += 1

        is_stopped_at_office = (
            office_shapes
            and speed is not None
            and speed <= MIN_MOVING_SPEED_KMH
            and _in_any(point, office_shapes)
        )
        if is_stopped_at_office:
            if stop_last is not None and not _valid_interval(
                stop_last, point, MAX_OFFICE_GAP_SECONDS
            ):
                close_stop()
            if stop_start is None:
                stop_start = point
            stop_last = point
        else:
            close_stop()

        if allowed_shapes and not _in_any(point, allowed_shapes):
            if speed is not None and speed > MIN_MOVING_SPEED_KMH:
                metrics.unauthorized_km += _valid_distance(point.distance_km)
                if previous is not None and not _in_any(previous[0], allowed_shapes):
                    metrics.unauthorized_minutes += _valid_interval(
                        previous[0], point, MAX_INTERVAL_SECONDS
                    ) / 60
        previous = (point, inside)
    close_stop()

    if track[0].timestamp.astimezone(TZ).weekday() >= 5:
        metrics.weekend_site_movement = metrics.moving_minutes > 0

    site_events, outside_events = detect_speed_violations_by_polygon(
        track,
        registry,
        settings.site_speed_threshold_kmh,
        settings.outside_speed_threshold_kmh,
        min_duration_seconds=settings.speed_min_duration_seconds,
    )
    metrics.speed_site = len(site_events)
    metrics.speed_outside = len(outside_events)
    for event in (*site_events, *outside_events):
        metrics.speed_events.append({
            "plate": event.plate,
            "place": "На площадке" if event.on_site else "Вне площадки",
            "start": event.start.astimezone(TZ).isoformat(timespec="seconds"),
            "finish": event.finish.astimezone(TZ).isoformat(timespec="seconds"),
            "duration_seconds": event.duration_seconds,
            "max_speed_kmh": round(event.max_speed_kmh, 1),
            "threshold_kmh": event.threshold_kmh,
            "latitude": float(event.max_point.lat),
            "longitude": float(event.max_point.lon),
        })
    return metrics


def load_daily_rows(
    connection: psycopg.Connection[Any], settings: EfficiencySettings
) -> list[dict[str, Any]]:
    """Apply the latest roster snapshot effective on each day; include zero-GPS cars."""
    query = """
        WITH days AS (
            SELECT generate_series(%s::date, %s::date, interval '1 day')::date AS report_day
        ),
        effective AS (
            SELECT d.report_day, s.roster_day
            FROM days d
            LEFT JOIN LATERAL (
                SELECT roster_day FROM consolidated_roster_snapshots
                WHERE roster_day <= d.report_day ORDER BY roster_day DESC LIMIT 1
            ) s ON TRUE
        ),
        roster AS (
            SELECT d.report_day, r.normalized_plate, r.plate, r.company,
                   r.user_name, r.model, r.directorate
            FROM effective d
            JOIN consolidated_roster_entries r ON r.roster_day=d.roster_day
        )
        SELECT
            COALESCE(r.report_day,c.report_day) AS report_day,
            COALESCE(r.normalized_plate,c.normalized_plate) AS normalized_plate,
            COALESCE(NULLIF(r.plate,''), NULLIF(c.plate,''),c.normalized_plate) AS plate,
            COALESCE(NULLIF(r.company,''),c.company,'') AS company,
            COALESCE(r.user_name,c.user_name,'') AS user_name,
            COALESCE(r.model,'') AS model,
            COALESCE(r.directorate,'') AS directorate,
            (r.normalized_plate IS NOT NULL) AS in_roster,
            COALESCE(c.inside_km,0) AS inside_km,
            COALESCE(c.outside_km,0) AS outside_km,
            COALESCE(c.total_km,0) AS total_km,
            COALESCE(c.raw_points,0) AS raw_points,
            (cd.status='SUCCESS' AND c.normalized_plate IS NOT NULL) AS cache_ready,
            cd.gps_max_event_time AS cache_gps_event_time,
            cd.gps_max_received_at AS cache_gps_received_at,
            cd.geofence_updated_at AS cache_geofence_updated_at
        FROM roster r
        FULL JOIN consolidated_report_cache c
          ON c.report_day=r.report_day AND c.normalized_plate=r.normalized_plate
         AND c.report_day BETWEEN %s AND %s
        LEFT JOIN consolidated_cache_days cd
          ON cd.report_day=COALESCE(r.report_day,c.report_day)
        WHERE COALESCE(r.report_day,c.report_day) BETWEEN %s AND %s
        ORDER BY report_day,normalized_plate
    """
    params = (
        settings.date_from, settings.date_to,
        settings.date_from, settings.date_to,
        settings.date_from, settings.date_to,
    )
    with connection.cursor(row_factory=psycopg.rows.dict_row) as cursor:
        cursor.execute(query, params)
        rows = [dict(row) for row in cursor.fetchall()]
    return [r for r in rows if matches_filters(r, settings)]


def matches_filters(row: Mapping[str, Any], settings: EfficiencySettings) -> bool:
    return (
        (not settings.plates or row["normalized_plate"] in settings.plates)
        and (not settings.directorates or row["directorate"] in settings.directorates)
        and (not settings.users or row["user_name"] in settings.users)
        and (not settings.models or row["model"] in settings.models)
    )


def apply_cache_coverage(
    rows: Sequence[dict[str, Any]], coverage: CacheCoverage,
) -> list[str]:
    """Invalidate affected cars without penalizing them for other missing cars.

    ``roster_coverage`` only means that some vehicles in the day's roster have
    no consolidated-cache row. A vehicle with its own successful cache row can
    still be scored. All other freshness failures remain day-wide blockers.
    Cars without their own cache entry already have ``cache_ready=False``.
    """
    reasons_by_day = {day: set(reasons) for day, reasons in coverage.stale_reasons}
    # Fail closed if an external coverage implementation reports an invalid
    # date without detailed reasons. Only the explicit roster-only case is safe.
    for day in coverage.stale_days:
        reasons_by_day.setdefault(day, {"unknown"})
    roster_partial_days: list[str] = []
    for day, reasons in sorted(reasons_by_day.items()):
        if "roster_coverage" in reasons:
            roster_partial_days.append(day.isoformat())
    for row in rows:
        reasons = reasons_by_day.get(row["report_day"], set())
        row["roster_partial"] = "roster_coverage" in reasons
        if reasons - {"roster_coverage"}:
            row["cache_ready"] = False
    return roster_partial_days


def iter_daily_gps(
    connection: psycopg.Connection[Any],
    report_day: date,
    plates: Sequence[str],
) -> Iterable[tuple[str, list[GPSPoint]]]:
    """Read a single partition/day as a named server-side cursor, one car at a time."""
    if not plates:
        return
    start = datetime.combine(report_day, time.min, TZ)
    finish = start + timedelta(days=1)
    name = f"efficiency_{report_day:%Y%m%d}"
    with connection.cursor(name=name, row_factory=psycopg.rows.tuple_row) as cursor:
        cursor.itersize = 2500
        cursor.execute(
            """
            SELECT normalized_plate, plate, event_time, latitude, longitude,
                   speed_kmh, distance_km
            FROM gps_points
            WHERE event_time >= %s AND event_time < %s
              AND normalized_plate = ANY(%s::text[])
            ORDER BY normalized_plate, event_time, id
            """,
            (start, finish, list(plates)),
        )
        previous_plate: str | None = None
        current: list[GPSPoint] = []
        for normalized, plate, instant, lat, lon, speed, distance in cursor:
            if previous_plate is not None and normalized != previous_plate:
                yield previous_plate, current
                current = []
            previous_plate = normalized
            current.append(GPSPoint(
                plate=plate, timestamp=instant, lat=float(lat), lon=float(lon),
                speed=float(speed) if speed is not None else None,
                distance_km=float(distance) if distance is not None else None,
            ))
        if previous_plate is not None:
            yield previous_plate, current


def compute_score(
    eligible_days: int,
    norm_days: int,
    inside_km: float,
    outside_km: float,
    moving_minutes: float,
    office_minutes: float,
    outside_allowed_km: float,
    speed_events: int,
    office_configured: bool,
    allowed_configured: bool,
) -> int | None:
    if eligible_days <= 0:
        return None
    metrics: list[tuple[float, float]] = [
        (SCORE_WEIGHTS["norm_days"], min(1.0, norm_days / eligible_days)),
        (SCORE_WEIGHTS["inside_share"], inside_km / (inside_km + outside_km)
         if inside_km + outside_km > 0 else 0.0),
    ]
    if office_configured:
        metrics.append((
            SCORE_WEIGHTS["office_motion"],
            moving_minutes / (moving_minutes + office_minutes)
            if moving_minutes + office_minutes > 0 else 0.0,
        ))
    if allowed_configured:
        metrics.append((
            SCORE_WEIGHTS["authorized_use"],
            1.0 - min(1.0, outside_allowed_km / max(inside_km + outside_km, 1.0)),
        ))
    weight = sum(item[0] for item in metrics)
    raw = 100.0 * sum(w * result for w, result in metrics) / weight
    penalty = min(SPEED_PENALTY_CAP, speed_events * SPEED_PENALTY_PER_EVENT)
    return max(0, min(100, round(raw - penalty)))


def aggregate_report(
    rows: Sequence[Mapping[str, Any]],
    gps_by_day: Mapping[tuple[date, str], GPSDailyMetrics],
    settings: EfficiencySettings,
) -> dict[str, Any]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["normalized_plate"])].append(row)
    vehicles: list[dict[str, Any]] = []
    all_events: list[dict[str, Any]] = []
    detail: list[dict[str, Any]] = []
    incomplete_days = set()
    provisional_days = set()
    today_local = datetime.now(TZ).date()
    for normalized, days in grouped.items():
        days = sorted(days, key=lambda r: r["report_day"])
        current = days[-1]
        total_inside = sum(float(r["inside_km"] or 0) for r in days)
        total_outside = sum(float(r["outside_km"] or 0) for r in days)
        # Weekends/non-roster dates contribute to overall mileage and site
        # activity but cannot inflate the mean per planned working day.
        weekday_inside = sum(
            float(r["inside_km"] or 0)
            for r in days if r["in_roster"] and r["report_day"].weekday() < 5
        )
        weekday_outside = sum(
            float(r["outside_km"] or 0)
            for r in days if r["in_roster"] and r["report_day"].weekday() < 5
        )
        eligible_days = 0
        norm_days = 0
        weekend_days = 0
        gps_days = 0
        moving = office = unauthorized_km = unauthorized_minutes = 0.0
        stops = lunch_exits = lunch_returns = speed_site = speed_outside = 0
        for row in days:
            day = row["report_day"]
            if not row["cache_ready"]:
                incomplete_days.add(day)
            if day >= today_local:
                provisional_days.add(day)
            gps = gps_by_day.get((day, normalized), GPSDailyMetrics())
            if gps.points > 0:
                gps_days += 1
            if row["in_roster"] and day.weekday() < 5:
                eligible_days += 1
                norm = (
                    settings.transit_min_inside_km
                    if is_transit(str(row["model"]))
                    else settings.other_min_inside_km
                )
                if row["cache_ready"] and gps.points > 0 and float(row["inside_km"] or 0) >= norm:
                    norm_days += 1
            if day.weekday() >= 5 and gps.weekend_site_movement:
                weekend_days += 1
            moving += gps.moving_minutes
            office += gps.office_parking_minutes
            stops += gps.office_stops
            unauthorized_km += gps.unauthorized_km
            unauthorized_minutes += gps.unauthorized_minutes
            lunch_exits += gps.lunch_exits
            lunch_returns += gps.lunch_returns
            speed_site += gps.speed_site
            speed_outside += gps.speed_outside
            all_events.extend(gps.speed_events)
            detail.append({
                "date": day.isoformat(), "plate": row["plate"],
                "model": row["model"], "directorate": row["directorate"],
                "user": row["user_name"], "in_roster": row["in_roster"],
                "cache_ready": row["cache_ready"], "inside_km": round(float(row["inside_km"] or 0), 2),
                "outside_km": round(float(row["outside_km"] or 0), 2),
                "moving_minutes": round(gps.moving_minutes, 1),
                "office_minutes": round(gps.office_parking_minutes, 1),
                "speed_events": gps.speed_site + gps.speed_outside,
                "gps_points": gps.points,
            })
        score = (
            compute_score(
                eligible_days, norm_days, total_inside, total_outside,
                moving, office, unauthorized_km, speed_site + speed_outside,
                bool(settings.office_zone_codes), bool(settings.allowed_zone_codes),
            )
            if gps_days and all(r["cache_ready"] for r in days) else None
        )
        preliminary = (
            not (settings.office_zone_codes and settings.allowed_zone_codes)
            or any(row["report_day"] >= today_local for row in days)
            or any(row.get("roster_partial", False) for row in days)
        )
        vehicle = {
            "plate": current["plate"],
            "normalized_plate": normalized,
            "company": current["company"],
            "model": current["model"],
            "directorate": current["directorate"],
            "user": current["user_name"],
            "eligible_days": eligible_days,
            "norm_days": norm_days,
            "inside_daily_norm_km": (
                settings.transit_min_inside_km
                if is_transit(str(current["model"]))
                else settings.other_min_inside_km
            ),
            "gps_days": gps_days,
            "days": len(days),
            "inside_km": round(total_inside, 2),
            "outside_km": round(total_outside, 2),
            "inside_km_per_day": round(weekday_inside / eligible_days, 1) if eligible_days else None,
            "outside_km_per_day": round(weekday_outside / eligible_days, 1) if eligible_days else None,
            "inside_share_percent": round(100 * total_inside / (total_inside + total_outside), 1)
            if total_inside + total_outside else None,
            "inside_to_outside_percent": round(100 * total_inside / total_outside, 1)
            if total_outside > 0 else None,
            "moving_minutes": round(moving, 1),
            "office_parking_minutes": round(office, 1) if settings.office_zone_codes else None,
            "motion_vs_office_percent": round(100 * moving / (moving + office), 1)
            if settings.office_zone_codes and moving + office > 0 else None,
            "office_stops": stops if settings.office_zone_codes else None,
            "lunch_exits": lunch_exits,
            "lunch_returns": lunch_returns,
            "weekend_site_days": weekend_days,
            "unauthorized_km": round(unauthorized_km, 2) if settings.allowed_zone_codes else None,
            "unauthorized_minutes": round(unauthorized_minutes, 1) if settings.allowed_zone_codes else None,
            "speed_site_events": speed_site,
            "speed_outside_events": speed_outside,
            "speed_events": speed_site + speed_outside,
            "speed_penalty": min(SPEED_PENALTY_CAP, (speed_site + speed_outside) * SPEED_PENALTY_PER_EVENT),
            "score": score,
            "data_status": (
                "Предварительно" if score is not None and preliminary
                else "ok" if score is not None else "Недостаточно данных"
            ),
            "preliminary": preliminary,
        }
        vehicles.append(vehicle)
    vehicles.sort(key=lambda v: (v["score"] is None, -(v["score"] or 0), v["plate"]))
    for i, vehicle in enumerate(vehicles, 1):
        vehicle["position"] = i
        vehicle["rank"] = i if vehicle["score"] is not None else None
    total_inside = sum(v["inside_km"] for v in vehicles)
    total_outside = sum(v["outside_km"] for v in vehicles)
    result = {
        "period": {"from": settings.date_from.isoformat(), "to": settings.date_to.isoformat()},
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "settings": settings.model_dump(mode="json"),
        "summary": {
            "vehicles": len(vehicles),
            "scored_vehicles": sum(v["score"] is not None for v in vehicles),
            "norm_days": sum(v["norm_days"] for v in vehicles),
            "eligible_days": sum(v["eligible_days"] for v in vehicles),
            "inside_share_percent": round(100 * total_inside / (total_inside + total_outside), 1)
            if total_inside + total_outside else None,
            "moving_minutes": round(sum(v["moving_minutes"] for v in vehicles), 1),
            "office_parking_minutes": round(sum(v["office_parking_minutes"] or 0 for v in vehicles), 1)
            if settings.office_zone_codes else None,
            "speed_events": sum(v["speed_events"] for v in vehicles),
            "weekend_site_days": sum(v["weekend_site_days"] for v in vehicles),
        },
        "coverage": {
            "incomplete_days": [day.isoformat() for day in sorted(incomplete_days)],
            "provisional_days": [day.isoformat() for day in sorted(provisional_days)],
            "gps_missing_vehicles": [v["plate"] for v in vehicles if not v["gps_days"]],
            "score_preliminary": (
                not (settings.office_zone_codes and settings.allowed_zone_codes)
                or bool(provisional_days)
            ),
            "notice": "Показатели движения/стоянок основаны на подтверждённых GPS-интервалах; разрывы связи не считаются работой или стоянкой.",
        },
        "vehicles": vehicles,
        "daily": detail,
        "speed_events": sorted(all_events, key=lambda e: (e["plate"], e["start"])),
    }
    return result


def generate_efficiency_report(
    database_url: str, settings: EfficiencySettings
) -> dict[str, Any]:
    with psycopg.connect(database_url, connect_timeout=10, options="-c statement_timeout=120000") as conn:
        shapes = load_shapes(conn)
        office = choose_shapes(shapes, settings.office_zone_codes, "стоянок")
        allowed = choose_shapes(shapes, settings.allowed_zone_codes, "разрешённого движения")
        rows = load_daily_rows(conn, settings)
        coverage = _cached_coverage(conn, settings.date_from, settings.date_to)
    roster_partial_days = apply_cache_coverage(rows, coverage)
    if not rows:
        result = aggregate_report([], {}, settings)
        result["coverage"]["roster_coverage_days"] = roster_partial_days
        result["coverage"]["score_preliminary"] |= bool(roster_partial_days)
        result["coverage"]["gps_metric_cache_hits"] = 0
        result["coverage"]["gps_metric_cache_misses"] = 0
        result["coverage"]["gps_metric_persisted_hits"] = 0
        result["coverage"]["gps_metric_persisted_writes"] = 0
        result["coverage"]["gps_metric_persisted_available"] = persistent_cache_enabled()
        result["coverage"]["stale_reasons"] = [
            {"date": day.isoformat(), "reasons": list(reasons)}
            for day, reasons in coverage.stale_reasons
        ]
        return result
    registry = load_database_registry(database_url)
    metric_signature = (
        _metric_settings_signature(settings), tuple(registry.version_refs)
    )
    day_plate_map: dict[date, list[str]] = defaultdict(list)
    pending_keys: dict[tuple[date, str], tuple[Any, ...]] = {}
    pending_digests: dict[tuple[date, str], str] = {}
    gps: dict[tuple[date, str], GPSDailyMetrics] = {}
    today_local = datetime.now(TZ).date()
    cache_hits = 0
    for item in rows:
        day, plate = item["report_day"], item["normalized_plate"]
        key = (
            day, plate, item["cache_gps_event_time"],
            item["cache_gps_received_at"], item["cache_geofence_updated_at"],
            metric_signature,
        )
        cached = (
            _cached_metric(key)
            if item["cache_ready"] and day < today_local
            else None
        )
        if cached is not None:
            gps[(day, plate)] = cached
            cache_hits += 1
        else:
            day_plate_map[day].append(plate)
            if item["cache_ready"] and day < today_local:
                pending_keys[(day, plate)] = key
                pending_digests[(day, plate)] = metric_digest(key)
    persisted_hits = persisted_writes = 0
    persist_available = persistent_cache_enabled()
    if day_plate_map:
        with DailyMetricStore(database_url) as store:
            saved = store.load(list(pending_digests.values()))
            for day_plate, digest in pending_digests.items():
                payload = saved.get(digest)
                restored = _restore_daily_metrics(payload) if payload is not None else None
                if restored is not None:
                    gps[day_plate] = restored
                    _remember_metric(pending_keys[day_plate], restored)
                    persisted_hits += 1
                    cache_hits += 1

            remaining = {
                day: sorted({plate for plate in plates if (day, plate) not in gps})
                for day, plates in day_plate_map.items()
            }
            if any(remaining.values()):
                with psycopg.connect(database_url, connect_timeout=10, options="-c statement_timeout=120000") as conn:
                    for day, plates in sorted(remaining.items()):
                        if not plates:
                            continue
                        to_persist: list[tuple[str, date, str, Mapping[str, Any]]] = []

                        def remember(plate: str, metrics: GPSDailyMetrics) -> None:
                            gps[(day, plate)] = metrics
                            key = pending_keys.get((day, plate))
                            if key is not None:
                                _remember_metric(key, metrics)
                                to_persist.append((
                                    pending_digests[(day, plate)], day, plate, asdict(metrics),
                                ))

                        for normalized, track in iter_daily_gps(conn, day, plates):
                            remember(normalized, analyze_daily_track(
                                track, registry, office, allowed, settings,
                            ))
                        # Zero-point vehicles must stay in the report and cache.
                        for plate in plates:
                            if (day, plate) not in gps:
                                remember(plate, GPSDailyMetrics())
                        # Commit after each complete day: a later failing day
                        # must not discard the safely calculated earlier days.
                        persisted_writes += store.save(to_persist)
            persist_available = store.enabled
    result = aggregate_report(rows, gps, settings)
    result["coverage"]["roster_coverage_days"] = roster_partial_days
    result["coverage"]["score_preliminary"] |= bool(roster_partial_days)
    result["coverage"]["gps_metric_cache_hits"] = cache_hits
    result["coverage"]["gps_metric_cache_misses"] = len(rows) - cache_hits
    result["coverage"]["gps_metric_persisted_hits"] = persisted_hits
    result["coverage"]["gps_metric_persisted_writes"] = persisted_writes
    result["coverage"]["gps_metric_persisted_available"] = persist_available
    result["coverage"]["stale_reasons"] = [
        {"date": day.isoformat(), "reasons": list(reasons)}
        for day, reasons in coverage.stale_reasons
    ]
    return result


def filter_options(database_url: str, start: date, finish: date) -> dict[str, Any]:
    settings = EfficiencySettings(date_from=start, date_to=finish)
    with psycopg.connect(database_url, connect_timeout=10, options="-c statement_timeout=15000") as conn:
        rows = load_daily_rows(conn, settings)
        shapes = load_shapes(conn)
    return {
        "plates": sorted({row["plate"] for row in rows if row["plate"]}),
        "directorates": sorted({row["directorate"] for row in rows if row["directorate"]}),
        "users": sorted({row["user_name"] for row in rows if row["user_name"]}),
        "models": sorted({row["model"] for row in rows if row["model"]}),
        "zones": [
            {"code": s.code, "name": s.name, "type": s.kind}
            for s in shapes.values()
            if s.kind not in {"SPEED_EXCLUSION", "TUNNEL"}
        ],
        "speed_exclusion_zones": [
            {"code": s.code, "name": s.name}
            for s in shapes.values()
            if s.kind in {"SPEED_EXCLUSION", "TUNNEL"}
        ],
        "defaults": {
            "site_speed_threshold_kmh": DEFAULT_SITE_SPEED_THRESHOLD_KMH,
            "outside_speed_threshold_kmh": DEFAULT_OUTSIDE_SPEED_THRESHOLD_KMH,
            "speed_min_duration_seconds": MIN_SPEED_EVENT_DURATION_SECONDS,
        },
    }


def _append_safe_excel_row(sheet: Worksheet, values: Sequence[Any]) -> None:
    """Keep external roster/geozone names as text, never executable Excel formulas."""
    sheet.append(list(values))
    for cell in sheet[sheet.max_row]:
        if isinstance(cell.value, str) and cell.value.startswith("="):
            cell.data_type = "s"


def workbook_from_report(report: Mapping[str, Any]) -> bytes:
    """Produce a highlighted, inspectable XLSX without another GPS calculation."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Рейтинг ТС"
    headers = [
        "№ строки", "Госномер", "Марка / модель", "Дирекция", "Пользователь",
        "Дней с выполненным нормативом пробега", "Плановых рабочих дней", "Общий пробег внутри Аккую, км",
        "Средний пробег внутри Аккую, км/плановый рабочий день", "Общий пробег вне Аккую, км",
        "Доля пробега внутри Аккую, %", "Отношение пробега внутри к пробегу вне, %", "Время движения внутри Аккую, мин",
        "Время стоянок в выбранных офисных зонах, мин", "Доля движения среди движения и офисных стоянок, %",
        "Выездов / возвращений с обеда, шт.", "Выходных дней с движением внутри Аккую",
        "Пробег вне разрешённых зон по GPS, км", "Эпизодов превышения скорости внутри Аккую", "Эпизодов превышения скорости вне Аккую",
        "Штраф за превышения скорости, баллы", "Итоговый балл, 0–100", "Статус оценки",
        "Место в рейтинге", "Норматив внутри Аккую по текущей модели, км/плановый рабочий день",
    ]
    _append_safe_excel_row(sheet, headers)
    for vehicle in report["vehicles"]:
        cells = [
            vehicle["position"], vehicle["plate"], vehicle["model"], vehicle["directorate"], vehicle["user"],
            vehicle["norm_days"], vehicle["eligible_days"], vehicle["inside_km"],
            vehicle["inside_km_per_day"],
            vehicle["outside_km"], vehicle["inside_share_percent"],
            vehicle["inside_to_outside_percent"], vehicle["moving_minutes"],
            vehicle["office_parking_minutes"], vehicle["motion_vs_office_percent"],
            f'{vehicle["lunch_exits"]} / {vehicle["lunch_returns"]}',
            vehicle["weekend_site_days"], vehicle["unauthorized_km"],
            vehicle["speed_site_events"], vehicle["speed_outside_events"],
            vehicle["speed_penalty"], vehicle["score"],
            "Рассчитано" if vehicle["data_status"] == "ok" else vehicle["data_status"],
            vehicle["rank"], vehicle["inside_daily_norm_km"],
        ]
        _append_safe_excel_row(sheet, cells)
        line = sheet.max_row
        norm = vehicle["inside_daily_norm_km"]
        avg = vehicle["inside_km_per_day"]
        if avg is not None:
            sheet.cell(line, 9).fill = PatternFill("solid", fgColor="FDE2E2" if avg < norm else "E0F4E8")
        if vehicle["speed_events"]:
            for col in (19, 20):
                sheet.cell(line, col).fill = PatternFill("solid", fgColor="FDE2E2")
        if vehicle["score"] is not None:
            sheet.cell(line, 22).fill = PatternFill(
                "solid", fgColor="E0F4E8" if vehicle["score"] >= 85 else "FFF0CD"
                if vehicle["score"] >= 60 else "FDE2E2"
            )
        if vehicle["unauthorized_km"]:
            sheet.cell(line, 18).fill = PatternFill("solid", fgColor="FDE2E2")
    sheet.freeze_panes = "C2"
    sheet.auto_filter.ref = sheet.dimensions

    daily = workbook.create_sheet("По дням")
    _append_safe_excel_row(daily, [
        "Дата", "Госномер", "Модель", "Дирекция", "Пользователь", "Есть в разнарядке",
        "Данные сводного кэша готовы", "Пробег внутри Аккую, км", "Пробег вне Аккую, км", "Время движения внутри Аккую, мин",
        "Стоянки в выбранных офисных зонах, мин", "Эпизоды превышения скорости, шт.", "Количество GPS-точек",
    ])
    for item in report["daily"]:
        _append_safe_excel_row(daily, [
            item["date"], item["plate"], item["model"], item["directorate"], item["user"],
            "Да" if item["in_roster"] else "Нет", "Да" if item["cache_ready"] else "Нет",
            item["inside_km"], item["outside_km"], item["moving_minutes"],
            item["office_minutes"], item["speed_events"], item["gps_points"],
        ])
    daily.freeze_panes = "C2"
    daily.auto_filter.ref = daily.dimensions

    speed = workbook.create_sheet("Превышения скорости")
    _append_safe_excel_row(speed, [
        "Госномер", "Внутри или вне Аккую", "Начало", "Окончание", "Длительность превышения, сек",
        "Максимальная скорость, км/ч", "Установленный порог, км/ч", "Широта", "Долгота",
    ])
    for event in report["speed_events"]:
        _append_safe_excel_row(speed, [
            event["plate"], event["place"], event["start"], event["finish"],
            event["duration_seconds"], event["max_speed_kmh"], event["threshold_kmh"],
            event["latitude"], event["longitude"],
        ])
    speed.freeze_panes = "C2"
    speed.auto_filter.ref = speed.dimensions

    notes = workbook.create_sheet("Методика")
    _append_safe_excel_row(notes, ["Показатель", "Правило"])
    _append_safe_excel_row(notes, ["Источник пробегов", "consolidated_report_cache (сводный отчёт)"])
    _append_safe_excel_row(notes, ["Источник GPS", "gps_points; интервалы со связью и достоверной скоростью"])
    _append_safe_excel_row(notes, ["Часовой пояс", "Europe/Istanbul"])
    _append_safe_excel_row(notes, ["Выходные", "суббота и воскресенье; требуется движение внутри площадки"])
    _append_safe_excel_row(notes, ["Норматив пробега внутри Аккую", f'Ford Transit — от {report["settings"]["transit_min_inside_km"]} км за плановый рабочий день; другие модели — от {report["settings"]["other_min_inside_km"]} км (текущие настройки)'])
    _append_safe_excel_row(notes, ["Средний пробег за плановый рабочий день", "Сумма пробега только за будни по разнарядке / число плановых будней; пробег в выходные на среднее не влияет"])
    _append_safe_excel_row(notes, ["Цвет среднего пробега внутри", "Зелёный — среднее не ниже нормы по модели; красный — среднее ниже нормы. Дни с выполненным нормативом считаются отдельно по каждому дню"])
    _append_safe_excel_row(notes, ["Доля пробега внутри Аккую", "Общий пробег внутри за период / общий пробег внутри и вне за период; включает выходные"])
    _append_safe_excel_row(notes, ["Доля движения среди движения и офисных стоянок", "Время подтверждённого движения внутри Аккую / (это время движения + время стоянок в выбранных офисных зонах)"])
    _append_safe_excel_row(notes, ["Стоянка", "Полная длительность подтверждённого непрерывного интервала строго больше порога"])
    _append_safe_excel_row(notes, ["Тоннели", "SPEED_EXCLUSION/TUNNEL всегда исключаются из контроля скорости"])
    _append_safe_excel_row(notes, ["Скорость", "Не менее 3 последовательных правдоподобных GPS-точек и заданной длительности"])
    _append_safe_excel_row(notes, ["Итоговый балл", "50% норматив дней + 25% движение/стоянки + 15% доля пробега внутри + 10% разрешённые зоны"])
    _append_safe_excel_row(notes, ["Ненастроенные геозоны", "Соответствующий компонент исключается, остальные веса пропорционально нормируются; балл предварительный"])
    _append_safe_excel_row(notes, ["Штраф скорости", "−3 балла за подтверждённое превышение, максимум −15 баллов"])
    _append_safe_excel_row(notes, ["№ и место", "№ — порядковый номер каждой строки; место присваивается только ТС с рассчитанным баллом"])
    _append_safe_excel_row(notes, ["Недостаточно данных", "Балл не присваивается без GPS, плановых дней или готового кэша самого ТС; отсутствие строки другого ТС не блокирует балл"])
    setting_labels = {
        "date_from": "Начало периода", "date_to": "Конец периода",
        "plates": "Фильтр по госномерам", "directorates": "Фильтр по дирекциям",
        "users": "Фильтр по пользователям", "models": "Фильтр по моделям",
        "office_zone_codes": "Коды выбранных офисных геозон",
        "allowed_zone_codes": "Коды выбранных разрешённых геозон",
        "lunch_exit_from": "Окно выезда на обед: начало",
        "lunch_exit_to": "Окно выезда на обед: конец",
        "lunch_return_from": "Окно возвращения с обеда: начало",
        "lunch_return_to": "Окно возвращения с обеда: конец",
        "transit_min_inside_km": "Норма Ford Transit внутри Аккую, км/рабочий день",
        "other_min_inside_km": "Норма остальных моделей внутри Аккую, км/рабочий день",
        "parking_min_minutes": "Минимальная стоянка: строго больше, мин",
        "site_speed_threshold_kmh": "Порог превышения скорости внутри Аккую, км/ч",
        "outside_speed_threshold_kmh": "Порог превышения скорости вне Аккую, км/ч",
        "speed_min_duration_seconds": "Минимальная длительность превышения, сек",
    }
    for key, value in report["settings"].items():
        label = setting_labels.get(key, key)
        readable_value = ", ".join(str(x) for x in value) if isinstance(value, list) else str(value)
        if isinstance(value, list) and not value:
            readable_value = "Не выбрано (все значения)" if key in {"plates", "directorates", "users", "models"} else "Не выбрано"
        _append_safe_excel_row(notes, [label, readable_value])

    for ws in workbook.worksheets:
        ws.sheet_view.showGridLines = False
        ws.row_dimensions[1].height = 57
        for cell in ws[1]:
            cell.fill = PatternFill("solid", fgColor="183153")
            cell.font = Font(color="FFFFFF", bold=True)
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        for column in ws.columns:
            letter = get_column_letter(column[0].column)
            width = max(len(str(c.value or "")) for c in list(column)[:300]) + 2
            ws.column_dimensions[letter].width = min(max(width, 12), 48)
    notes.column_dimensions["A"].width = 47
    notes.column_dimensions["B"].width = 100
    for row in notes.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        description_length = len(str(row[1].value or ""))
        notes.row_dimensions[row[0].row].height = min(90, max(22, 16 * ((description_length // 95) + 1)))
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


__all__ = [
    "EfficiencySettings", "GPSDailyMetrics", "GPSPoint", "Shape", "aggregate_report",
    "analyze_daily_track", "choose_shapes", "compute_score", "filter_options", "is_transit",
    "generate_efficiency_report", "shape_from_geojson", "workbook_from_report",
]
