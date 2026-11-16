#!/usr/bin/env python3
"""Offline database-status checks: day pruning, freshness and JSON failures."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import psycopg
from fastapi import FastAPI

from database_status_patch import (
    DATABASE_STATUS_ERROR,
    DATABASE_STATUS_PATH,
    apply_database_status_patch,
    database_status_payload,
)


TZ = ZoneInfo("Europe/Istanbul")


def payload(now, latest, points=123, vehicles=7):
    connection = MagicMock()
    connection.execute.side_effect = [
        MagicMock(fetchone=MagicMock(return_value=(latest, now, vehicles))),
        MagicMock(fetchone=MagicMock(return_value=(points,))),
    ]
    with patch("database_status_patch.psycopg.connect") as connect:
        connect.return_value.__enter__.return_value = connection
        result = database_status_payload("offline", TZ)
    summary, count = connection.execute.call_args_list
    assert "gps_points" not in summary.args[0]
    assert "event_time >= %s AND event_time < %s" in count.args[0]
    return result, count.args[1]


def check_days_and_freshness():
    # UTC is still the previous day while Istanbul has crossed midnight.
    now = datetime(2026, 11, 15, 21, 0, tzinfo=timezone.utc)
    result, (start, end) = payload(now, now - timedelta(minutes=5))
    assert result["status"] == "fresh"
    assert result["server_date"] == "2026-11-16"
    assert result["latest_date"] == "2026-11-15"
    assert result["today_points"] == 123 and result["today_vehicles"] == 7
    assert start == datetime(2026, 11, 16, tzinfo=TZ)
    assert end == datetime(2026, 11, 17, tzinfo=TZ)
    for minutes, status in ((60, "fresh"), (61, "warning"), (180, "warning"), (181, "stale")):
        result, _ = payload(now, now - timedelta(minutes=minutes))
        assert result["status"] == status
        assert result["age_minutes"] == minutes
    result, _ = payload(now, None, points=0, vehicles=0)
    assert result["status"] == "empty" and result["latest_date"] is None
    assert result["today_points"] == result["today_vehicles"] == 0


async def request(app):
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": DATABASE_STATUS_PATH,
        "raw_path": DATABASE_STATUS_PATH.encode(), "query_string": b"",
        "headers": [], "server": ("test", 80), "client": ("test", 1), "root_path": "",
    }, receive, send)
    start = next(item for item in messages if item["type"] == "http.response.start")
    body = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), json.loads(body)


def check_http_failures():
    app = FastAPI()
    app.add_api_route(DATABASE_STATUS_PATH, lambda: {})
    apply_database_status_patch(app, lambda: "offline", TZ)
    assert sum(route.path == DATABASE_STATUS_PATH for route in app.routes) == 1
    connection = MagicMock()
    connection.execute.side_effect = [
        MagicMock(fetchone=MagicMock(return_value=(None, datetime(2026, 11, 16, tzinfo=TZ), 0))),
        psycopg.errors.QueryCanceled("timeout"),
    ]
    for error in (None, psycopg.OperationalError("offline")):
        with patch("database_status_patch.psycopg.connect") as connect:
            if error is None:
                connect.return_value.__enter__.return_value = connection
            else:
                connect.side_effect = error
            status, headers, body = asyncio.run(request(app))
        assert status == 503 and headers[b"content-type"].startswith(b"application/json")
        assert headers[b"retry-after"] == b"60"
        assert body == {"detail": DATABASE_STATUS_ERROR}
    with patch("database_status_patch.database_status_payload", return_value={"status": "fresh"}):
        assert asyncio.run(request(app))[::2] == (200, {"status": "fresh"})


def main():
    check_days_and_freshness()
    check_http_failures()
    print("OK: explicit Istanbul day bounds, freshness states and JSON 503 errors")


if __name__ == "__main__":
    main()
