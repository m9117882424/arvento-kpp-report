#!/usr/bin/env python3
from __future__ import annotations

import inspect
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import arvento_postgres_sync_v2 as sync


class FakeConnection:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


def check_existing_region_name_does_not_lock_gps_table() -> None:
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (1,)
    sync.ensure_schema(connection)
    sql = [call.args[0] for call in cursor.execute.call_args_list]
    assert len(sql) == 1, "An existing column must not cause repeated DDL"
    assert "pg_attribute" in sql[0]
    connection.commit.assert_called_once()

    cursor.reset_mock()
    connection.commit.reset_mock()
    cursor.fetchone.return_value = None
    sync.ensure_schema(connection)
    sql = [call.args[0] for call in cursor.execute.call_args_list]
    assert len(sql) == 2
    assert "ALTER TABLE gps_points ADD COLUMN IF NOT EXISTS region_name" in sql[1]
    connection.commit.assert_called_once()


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


def check_existing_partition_skips_catalog_ddl() -> None:
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = tuple(range(4))
    sync.ensure_partition(connection, datetime(2026, 11, 7).date())
    assert cursor.execute.call_count == 1, "Existing partitions/indexes must not request DDL"
    query, params = cursor.execute.call_args.args
    assert "to_regclass" in query
    assert params == (
        "gps_points_2026_11_07", "ix_gps_points_2026_11_07_plate_time",
        "ix_gps_points_2026_11_07_position", "ix_gps_points_2026_11_07_region",
    )

    cursor.reset_mock()
    cursor.fetchone.return_value = (None, None, None, None)
    sync.ensure_partition(connection, datetime(2026, 11, 7).date())
    assert cursor.execute.call_count == 5, "Missing partition needs the table and three indexes"
    statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
    assert "CREATE TABLE IF NOT EXISTS" in statements[1]
    assert all("CREATE INDEX IF NOT EXISTS" in value for value in statements[2:])


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
    check_existing_region_name_does_not_lock_gps_table()
    check_partition_planning()
    check_existing_partition_skips_catalog_ddl()
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
