#!/usr/bin/env python3
"""Deterministic equivalence checks for consolidated report optimizations."""
from __future__ import annotations

import math
import random
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import consolidated_report as core
from consolidated_performance import (
    apply_consolidated_performance,
    optimized_sanitize_position_outliers,
    optimized_validated_speed_indices,
)


def speed_track(seed: int, count: int) -> list[SimpleNamespace]:
    randomizer = random.Random(seed)
    current = datetime(2026, 7, 13)
    result: list[SimpleNamespace] = []
    choices = [None, -1.0, 0.0, 20.0, 80.0, 140.0, 251.0, math.nan]
    for _ in range(count):
        current += timedelta(seconds=randomizer.choice([-1, 0, 1, 2, 3, 5, 10, 301]))
        speed = randomizer.choice(choices)
        if randomizer.random() < 0.7:
            speed = max(0.0, min(250.0, randomizer.gauss(70.0, 40.0)))
        result.append(SimpleNamespace(time=current, speed=speed))
    return result


def coordinate_track(seed: int, count: int) -> list[SimpleNamespace]:
    randomizer = random.Random(seed)
    current = datetime(2026, 7, 13)
    lat = 36.0
    lon = 33.0
    result: list[SimpleNamespace] = []
    for _ in range(count):
        current += timedelta(seconds=randomizer.choice([-1, 0, 1, 2, 5, 10, 60]))
        lat += randomizer.gauss(0.0, 0.001)
        lon += randomizer.gauss(0.0, 0.001)
        if randomizer.random() < 0.03:
            lat += randomizer.choice((-1.0, 1.0)) * randomizer.uniform(0.1, 1.0)
        result.append(SimpleNamespace(time=current, lat=lat, lon=lon))
    return result


def verify_equivalence() -> None:
    legacy_speed = core.validated_speed_indices
    legacy_sanitize = core.sanitize_position_outliers

    for seed in range(1_000):
        points = speed_track(seed, seed % 90)
        expected = legacy_speed(points)
        actual = optimized_validated_speed_indices(points)
        if actual != expected:
            raise AssertionError(
                f"validated_speed_indices differs for seed={seed}: "
                f"expected={sorted(expected)} actual={sorted(actual)}"
            )

    for seed in range(500):
        points = coordinate_track(seed, seed % 120)
        expected = legacy_sanitize(points)
        actual = optimized_sanitize_position_outliers(points)
        if actual != expected:
            raise AssertionError(f"sanitize_position_outliers differs for seed={seed}")


def benchmark() -> None:
    legacy = core.validated_speed_indices
    start = datetime(2026, 7, 13)
    points = [
        SimpleNamespace(time=start + timedelta(seconds=index), speed=60.0 + index % 4)
        for index in range(25_000)
    ]

    started = time.perf_counter()
    expected = legacy(points)
    legacy_seconds = time.perf_counter() - started

    started = time.perf_counter()
    actual = optimized_validated_speed_indices(points)
    optimized_seconds = time.perf_counter() - started

    if actual != expected:
        raise AssertionError("benchmark track produced different speed indexes")
    speedup = legacy_seconds / optimized_seconds if optimized_seconds else math.inf
    print(
        f"speed-window benchmark: legacy={legacy_seconds:.3f}s "
        f"optimized={optimized_seconds:.3f}s speedup={speedup:.1f}x"
    )


def main() -> int:
    verify_equivalence()
    benchmark()
    apply_consolidated_performance()
    apply_consolidated_performance()
    if core.validated_speed_indices is not optimized_validated_speed_indices:
        raise AssertionError("performance patch was not installed")
    print("OK: optimized calculations preserve legacy synthetic-track results")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
