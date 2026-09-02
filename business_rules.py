#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Canonical operational rules shared by CLI, portal, cache, and exports.

Keep user-visible thresholds here instead of changing module constants during
portal startup.  Environment variables are intentionally limited to settings
that operators may need to tune without rebuilding the image.
"""
from __future__ import annotations

import os
from datetime import time


def _float_setting(name: str, default: float, minimum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} должен быть числом") from exc
    if value < minimum:
        raise RuntimeError(f"{name} должен быть не меньше {minimum:g}")
    return value


def _int_setting(name: str, default: int, minimum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} должен быть целым числом") from exc
    if value < minimum:
        raise RuntimeError(f"{name} должен быть не меньше {minimum}")
    return value


TIMEZONE_NAME = "Europe/Istanbul"
CONSOLIDATED_CALCULATION_VERSION = "2026-09-02-contracts-v1"

# Speed-event rules. These values were already operational in the web portal;
# they are now also the standalone CLI defaults.
DEFAULT_SITE_SPEED_THRESHOLD_KMH = _float_setting(
    "ARVENTO_SITE_SPEED_THRESHOLD_KMH", 50.0, 5.0
)
DEFAULT_OUTSIDE_SPEED_THRESHOLD_KMH = _float_setting(
    "ARVENTO_OUTSIDE_SPEED_THRESHOLD_KMH", 103.0, 20.0
)
MIN_SPEED_EVENT_DURATION_SECONDS = _int_setting(
    "ARVENTO_MIN_SPEED_EVENT_DURATION_SECONDS", 3, 0
)
MAX_ACCELERATION_MPS2 = _float_setting(
    "ARVENTO_MAX_ACCELERATION_MPS2", 3.0, 0.1
)

# Consolidated-report rules.
GEOFENCE_VIOLATION_KM = _float_setting(
    "CONSOLIDATED_GEOFENCE_VIOLATION_KM", 80.0, 0.0
)
PERSONAL_USE_DISTANCE_DIFF_KM = _float_setting(
    "CONSOLIDATED_PERSONAL_USE_DISTANCE_DIFF_KM", 10.0, 0.0
)
PERSONAL_USE_PERCENT_DIFF = _float_setting(
    "CONSOLIDATED_PERSONAL_USE_PERCENT_DIFF", 0.10, 0.0
)
ENTRY_EXIT_TIME_FROM = time(5, 0)
ENTRY_EXIT_TIME_TO = time(23, 0)
NIGHT_START = time(22, 0)
NIGHT_END = time(5, 0)
SITE_EXIT_DISTANCE_THRESHOLD_KM = _float_setting(
    "CONSOLIDATED_SITE_EXIT_DISTANCE_THRESHOLD_KM", 10.0, 0.0
)

# VehicleDistanceReport review rules. Preserve the established environment
# variable names for deployment compatibility.
MILEAGE_REVIEW_ABSOLUTE_GAP_KM = _float_setting(
    "CONSOLIDATED_HYBRID_ABSOLUTE_GAP_KM", 10.0, 0.0
)
MILEAGE_REVIEW_RATIO = _float_setting(
    "CONSOLIDATED_HYBRID_RATIO", 1.20, 1.0
)


__all__ = [
    "DEFAULT_OUTSIDE_SPEED_THRESHOLD_KMH",
    "DEFAULT_SITE_SPEED_THRESHOLD_KMH",
    "CONSOLIDATED_CALCULATION_VERSION",
    "ENTRY_EXIT_TIME_FROM",
    "ENTRY_EXIT_TIME_TO",
    "GEOFENCE_VIOLATION_KM",
    "MAX_ACCELERATION_MPS2",
    "MILEAGE_REVIEW_ABSOLUTE_GAP_KM",
    "MILEAGE_REVIEW_RATIO",
    "MIN_SPEED_EVENT_DURATION_SECONDS",
    "NIGHT_END",
    "NIGHT_START",
    "PERSONAL_USE_DISTANCE_DIFF_KM",
    "PERSONAL_USE_PERCENT_DIFF",
    "SITE_EXIT_DISTANCE_THRESHOLD_KM",
    "TIMEZONE_NAME",
]
