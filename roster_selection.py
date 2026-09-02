#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One dated-roster selection policy for every reporting path."""
from __future__ import annotations

from datetime import date
from typing import Callable, Sequence, TypeVar


T = TypeVar("T")


def missing_roster_message(target_day: date) -> str:
    return (
        f"Для даты {target_day:%d.%m.%Y} нет разнарядки с этой или более ранней датой. "
        "Загрузите подходящую разнарядку в центральную базу."
    )


def select_effective_roster(
    rosters: Sequence[T],
    target_day: date,
    day_of: Callable[[T], date],
) -> T:
    """Return exact/latest-previous roster; never apply future data backwards."""
    applicable = [item for item in rosters if day_of(item) <= target_day]
    if not applicable:
        raise ValueError(missing_roster_message(target_day))

    latest_day = max(day_of(item) for item in applicable)
    # Preserve the established duplicate-date rule: the last loaded item wins.
    return [item for item in applicable if day_of(item) == latest_day][-1]


__all__ = ["missing_roster_message", "select_effective_roster"]
