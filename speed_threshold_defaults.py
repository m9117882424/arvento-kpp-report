#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Apply operational speed thresholds and anti-spike defaults before imports."""
from __future__ import annotations

import speed_violation_report as speed

DEFAULT_SITE_THRESHOLD_KMH = 50.0
DEFAULT_OUTSIDE_THRESHOLD_KMH = 103.0
DEFAULT_MIN_SPEED_EVENT_DURATION_SECONDS = 3
DEFAULT_MAX_ACCELERATION_MPS2 = 3.0
_APPLIED = False


def apply_speed_threshold_defaults() -> None:
    """Set module constants and function defaults used by all portal reports."""
    global _APPLIED

    speed.DEFAULT_SITE_SPEED_THRESHOLD_KMH = DEFAULT_SITE_THRESHOLD_KMH
    speed.DEFAULT_OUTSIDE_SPEED_THRESHOLD_KMH = DEFAULT_OUTSIDE_THRESHOLD_KMH
    speed.MIN_SPEED_EVENT_DURATION_SECONDS = (
        DEFAULT_MIN_SPEED_EVENT_DURATION_SECONDS
    )
    speed.MAX_ACCELERATION_MPS2 = DEFAULT_MAX_ACCELERATION_MPS2

    # Python binds default arguments when a function is defined, so update the
    # two public functions whose signatures contain the threshold defaults.
    speed.detect_speed_violations.__defaults__ = (
        DEFAULT_SITE_THRESHOLD_KMH,
        DEFAULT_OUTSIDE_THRESHOLD_KMH,
    )
    speed.append_speed_sheets.__defaults__ = (
        DEFAULT_SITE_THRESHOLD_KMH,
        DEFAULT_OUTSIDE_THRESHOLD_KMH,
    )
    _APPLIED = True


__all__ = [
    "DEFAULT_MAX_ACCELERATION_MPS2",
    "DEFAULT_MIN_SPEED_EVENT_DURATION_SECONDS",
    "DEFAULT_OUTSIDE_THRESHOLD_KMH",
    "DEFAULT_SITE_THRESHOLD_KMH",
    "apply_speed_threshold_defaults",
]
