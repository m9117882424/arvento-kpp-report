#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Strict prohibited-left-turn detector.

A violation is confirmed only when a vehicle:

1. enters the corridor through its right/start section;
2. advances towards the left/end section without a substantial reversal;
3. physically exits the corridor through the left end;
4. does not enter the allowing control zone during the configured window.

Merely reaching 75% of the corridor is not enough. A vehicle that turns around
inside the corridor and exits through the same right side is not a violation.
A long stop or sparse GPS data near the start does not permanently disarm a
later genuine traversal: after a timeout, detection is re-armed when the next
point is still in the start section.
"""
from __future__ import annotations

from datetime import datetime

import prohibited_left_turn_report as legacy

# A point outside the corridor is treated as a left-side exit only when its
# projection is in the final 5% of the centre line. This prevents a lateral
# departure near the middle/end section from being accepted as a completed turn.
EXIT_PROGRESS_MIN = 0.95


def _new_active(point: legacy.TrackPoint, position: legacy.CorridorPosition) -> dict[str, object]:
    return {
        "start_point": point,
        "start_position": position,
        "last_along": position.along_m,
        "max_progress": position.progress,
        "min_distance": position.distance_m,
        "point_count": 1,
        "reached_finish": position.progress >= legacy.FINISH_PROGRESS_MIN,
    }


def detect_confirmed_violations(
    track: list[legacy.TrackPoint],
    width_m: float,
    max_sequence_seconds: int,
    control_window_seconds: int,
    cooldown_seconds: int,
) -> list[legacy.Violation]:
    """Detect only completed right-to-left corridor exits.

    Temporary lateral excursions outside the narrow corridor do not terminate an
    active traversal. The traversal is cancelled when the vehicle substantially
    backtracks or exits through the right/start side.

    ``max_sequence_seconds`` limits one continuously tracked candidate. If that
    candidate expires while the current point is still in the start section, a
    fresh candidate may begin from that point. This handles stops and sparse GPS
    intervals without linking unrelated movements across the whole corridor.
    """
    half_width = width_m / 2.0
    violations: list[legacy.Violation] = []
    active: dict[str, object] | None = None
    pending: legacy.Violation | None = None
    last_violation_time: datetime | None = None

    # After a reversal, a new traversal may start only after the vehicle has
    # actually left the right/start side and enters again. A timeout in the start
    # section is the exception: the same physical traversal may resume there.
    entry_armed = True

    for point in track:
        # A geometrically completed left exit remains pending during the allowing
        # control-zone window. Entering that zone cancels the candidate.
        if pending is not None:
            seconds_after_finish = (point.timestamp - pending.finish).total_seconds()
            if (
                0 <= seconds_after_finish <= control_window_seconds
                and legacy.in_control_zone(point)
            ):
                pending = None
                active = None
                entry_armed = False
                continue
            if seconds_after_finish > control_window_seconds:
                violations.append(pending)
                last_violation_time = pending.finish
                pending = None
            else:
                continue

        position = legacy.project_to_corridor(point.lat, point.lon)
        inside = position.distance_m <= half_width

        if (
            last_violation_time is not None
            and (point.timestamp - last_violation_time).total_seconds() < cooldown_seconds
        ):
            continue

        if active is not None:
            start_point = active["start_point"]
            assert isinstance(start_point, legacy.TrackPoint)
            elapsed = (point.timestamp - start_point.timestamp).total_seconds()
            if elapsed <= 0:
                active = None
                entry_armed = False
                continue
            if elapsed > max_sequence_seconds:
                active = None
                # A stopped vehicle can remain in the first quarter longer than
                # the candidate timeout. Re-arm only there; farther along the
                # corridor a timeout cannot be joined to a new traversal.
                entry_armed = position.progress <= legacy.START_PROGRESS_MAX

        # The allowing zone cancels a traversal even before its geometric exit.
        if active is not None and legacy.in_control_zone(point):
            active = None
            entry_armed = False
            continue

        if active is None:
            if not inside:
                if position.progress <= legacy.START_PROGRESS_MAX:
                    entry_armed = True
                continue
            if entry_armed and position.progress <= legacy.START_PROGRESS_MAX:
                active = _new_active(point, position)
                entry_armed = False
            continue

        if inside:
            last_along = float(active["last_along"])
            if position.along_m < last_along - legacy.MAX_BACKTRACK_M:
                # The vehicle reversed inside the corridor. Do not immediately
                # restart while it is still returning through the same corridor.
                active = None
                entry_armed = False
                continue

            active["last_along"] = max(last_along, position.along_m)
            active["max_progress"] = max(
                float(active["max_progress"]), position.progress
            )
            active["min_distance"] = min(
                float(active["min_distance"]), position.distance_m
            )
            active["point_count"] = int(active["point_count"]) + 1
            if position.progress >= legacy.FINISH_PROGRESS_MIN:
                active["reached_finish"] = True
            continue

        # The point is outside the corridor. Mid-corridor lateral excursions are
        # tolerated because the corridor is narrow relative to GPS error.
        reached_finish = bool(active["reached_finish"]) or (
            position.progress >= legacy.FINISH_PROGRESS_MIN
        )
        if reached_finish and position.progress >= EXIT_PROGRESS_MIN:
            start_point = active["start_point"]
            start_position = active["start_position"]
            assert isinstance(start_point, legacy.TrackPoint)
            assert isinstance(start_position, legacy.CorridorPosition)
            pending = legacy.Violation(
                plate=point.plate,
                start_point=start_point,
                finish_point=point,
                start_position=start_position,
                finish_position=position,
                min_distance_m=float(active["min_distance"]),
                max_progress=max(float(active["max_progress"]), position.progress),
                point_count=int(active["point_count"]) + 1,
            )
            active = None
            continue

        if position.progress <= legacy.START_PROGRESS_MAX:
            # Returned and physically exited through the right/start side.
            active = None
            entry_armed = True
            continue

        # Temporary lateral departure. Keep the traversal active and wait for a
        # definitive left or right exit, reversal, control-zone hit or timeout.

    if pending is not None:
        violations.append(pending)

    return violations


__all__ = ["EXIT_PROGRESS_MIN", "detect_confirmed_violations"]
