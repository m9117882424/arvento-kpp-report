#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibility hook for the now-canonical operational speed settings."""
from __future__ import annotations

import speed_violation_report as speed
from business_rules import (
    DEFAULT_OUTSIDE_SPEED_THRESHOLD_KMH,
    DEFAULT_SITE_SPEED_THRESHOLD_KMH,
    MAX_ACCELERATION_MPS2,
    MIN_SPEED_EVENT_DURATION_SECONDS,
)

DEFAULT_SITE_THRESHOLD_KMH = DEFAULT_SITE_SPEED_THRESHOLD_KMH
DEFAULT_OUTSIDE_THRESHOLD_KMH = DEFAULT_OUTSIDE_SPEED_THRESHOLD_KMH
DEFAULT_MIN_SPEED_EVENT_DURATION_SECONDS = MIN_SPEED_EVENT_DURATION_SECONDS
DEFAULT_MAX_ACCELERATION_MPS2 = MAX_ACCELERATION_MPS2
_APPLIED = False


def apply_speed_threshold_defaults() -> None:
    """Keep compatibility with older startup code; values are already canonical."""
    global _APPLIED
    expected = (DEFAULT_SITE_THRESHOLD_KMH, DEFAULT_OUTSIDE_THRESHOLD_KMH)
    if speed.detect_speed_violations.__defaults__ != expected:
        raise RuntimeError("Пороги speed_violation_report расходятся с business_rules")
    _APPLIED = True


__all__ = [
    "DEFAULT_MAX_ACCELERATION_MPS2",
    "DEFAULT_MIN_SPEED_EVENT_DURATION_SECONDS",
    "DEFAULT_OUTSIDE_THRESHOLD_KMH",
    "DEFAULT_SITE_THRESHOLD_KMH",
    "apply_speed_threshold_defaults",
]
