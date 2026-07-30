#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import speed_violation_report as speed
from speed_threshold_defaults import (
    DEFAULT_OUTSIDE_THRESHOLD_KMH,
    DEFAULT_SITE_THRESHOLD_KMH,
    apply_speed_threshold_defaults,
)


def main() -> None:
    apply_speed_threshold_defaults()

    assert speed.DEFAULT_SITE_SPEED_THRESHOLD_KMH == 50.0
    assert speed.DEFAULT_OUTSIDE_SPEED_THRESHOLD_KMH == 103.0
    assert speed.detect_speed_violations.__defaults__ == (50.0, 103.0)
    assert speed.append_speed_sheets.__defaults__ == (50.0, 103.0)
    assert DEFAULT_SITE_THRESHOLD_KMH == 50.0
    assert DEFAULT_OUTSIDE_THRESHOLD_KMH == 103.0

    print("Speed threshold defaults verified: site=50, outside=103")


if __name__ == "__main__":
    main()
