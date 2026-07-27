#!/usr/bin/env python3
"""Small runtime smoke tests executed during the server image build."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import run_report_portal
from speed_violation_report import _event_is_smooth, validate_speed_thresholds


@dataclass
class FakePoint:
    timestamp: datetime
    speed: float


def points(*speeds: float, seconds: int = 10) -> list[FakePoint]:
    start = datetime(2026, 1, 1, 8, 0, 0)
    return [FakePoint(start + timedelta(seconds=index * seconds), speed) for index, speed in enumerate(speeds)]


def main() -> None:
    assert _event_is_smooth(points(40, 45, 50)), "Плавная последовательность отклонена"
    assert not _event_is_smooth(points(40, 120, 42)), "Одиночный выброс не отфильтрован"
    assert not _event_is_smooth(points(40, 45)), "Событие из двух точек не должно быть валидным"
    assert not _event_is_smooth(points(40, 45, 50, seconds=4)), "Слишком короткое событие принято"

    assert validate_speed_thresholds(33, 104.5) == (33.0, 104.5)
    try:
        validate_speed_thresholds(120, 100)
    except ValueError:
        pass
    else:
        raise AssertionError("Некорректное соотношение порогов не отклонено")

    html = run_report_portal.implementation.HTML
    for token in (
        'value="violation">Нарушения',
        'id="siteSpeedThreshold"',
        'id="outsideSpeedThreshold"',
        'id="plateFilter"',
        '/api/generate-v2',
    ):
        assert token in html, f"В интерфейсе отсутствует обязательный элемент: {token}"

    print("OK: runtime-проверки отчёта нарушений пройдены.")


if __name__ == "__main__":
    main()
