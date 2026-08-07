#!/usr/bin/env python3
from __future__ import annotations

import inspect
from datetime import datetime, timedelta
from types import SimpleNamespace

import arvento_postgres_sync_v2 as sync


class FakeConnection:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


def check_partition_planning() -> None:
    start = datetime(2026, 7, 3, 0, 0, tzinfo=sync.TZ)
    finish = start + timedelta(days=1)
    assert sync.partition_days(start, finish) == [start.date()]

    cross_start = datetime(2026, 7, 3, 23, 30, tzinfo=sync.TZ)
    cross_finish = datetime(2026, 7, 4, 0, 30, tzinfo=sync.TZ)
    assert sync.partition_days(cross_start, cross_finish) == [
        cross_start.date(),
        cross_finish.date(),
    ]

    calls = []
    connection = FakeConnection()
    original = sync.ensure_partition
    sync.ensure_partition = lambda conn, day: calls.append(day)
    try:
        days = sync.ensure_range_partitions(connection, cross_start, cross_finish)
    finally:
        sync.ensure_partition = original

    assert calls == days
    assert len(calls) == len(set(calls)) == 2
    assert connection.commits == 1


def sample_row(timestamp: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        device_no="123",
        plate="33 BEK 663",
        timestamp=timestamp,
        latitude=36.1234567,
        longitude=33.7654321,
        speed=42.0,
        distance=0.75,
        address="Test",
        event_type="Motion",
        driver="Driver",
        pause_duration=None,
        idling_duration=None,
        ignition_duration=None,
        region_name="Akkuyu",
    )


def check_stage_preparation() -> None:
    row = sample_row(datetime(2026, 7, 3, 12, 30))
    prepared = sync.prepare_stage_rows([row])
    assert len(prepared) == 1
    values = prepared[0]
    assert values[0] == "123"
    assert values[2] == "33BEK663"
    assert values[3].tzinfo is not None
    assert values[-1] == sync.source_hash(row)


def check_half_open_chunk_filter() -> None:
    start = datetime(2026, 7, 4, 22, 0, tzinfo=sync.TZ)
    finish = datetime(2026, 7, 5, 0, 0, tzinfo=sync.TZ)
    rows = [
        sample_row(datetime(2026, 7, 4, 21, 59, 59)),
        sample_row(datetime(2026, 7, 4, 22, 0, 0)),
        sample_row(datetime(2026, 7, 4, 23, 59, 59)),
        sample_row(datetime(2026, 7, 5, 0, 0, 0)),
    ]
    filtered = sync.filter_rows_to_half_open_interval(rows, start, finish)
    assert [sync._event_time(row) for row in filtered] == [
        datetime(2026, 7, 4, 22, 0, 0, tzinfo=sync.TZ),
        datetime(2026, 7, 4, 23, 59, 59, tzinfo=sync.TZ),
    ]


def check_no_per_row_sql_or_ddl() -> None:
    insert_source = inspect.getsource(sync.insert_rows)
    assert "ensure_partition" not in insert_source
    assert "for row in rows" not in insert_source
    assert "_copy_stage_rows" in insert_source
    assert "_insert_gps_and_queue" in insert_source
    assert "_upsert_vehicles" in insert_source

    copy_source = inspect.getsource(sync._copy_stage_rows)
    assert "COPY" in copy_source
    assert "write_row" in copy_source

    vehicle_source = inspect.getsource(sync._upsert_vehicles)
    assert "INSERT INTO vehicles" in vehicle_source
    assert "PARTITION BY device_no" in vehicle_source

    range_source = inspect.getsource(sync.sync_range)
    assert "ensure_range_partitions" in range_source
    assert "filter_rows_to_half_open_interval" in range_source
    assert range_source.index("ensure_range_partitions") < range_source.index("while current < end")


def main() -> int:
    check_partition_planning()
    check_stage_preparation()
    check_half_open_chunk_filter()
    check_no_per_row_sql_or_ddl()
    print(
        "OK: GPS sync prepares partitions once per range, enforces half-open "
        "chunk boundaries and uses COPY/batch SQL for GPS, queue, regions and vehicles"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
