#!/usr/bin/env python3
"""Canonical entrypoint for synchronizing Arvento GPS data into PostgreSQL/PostGIS.

The implementation currently remains in arvento_postgres_sync_v2.py for backward
compatibility with existing deployments. New deployments and documentation must
use this task-oriented filename.
"""

from arvento_postgres_sync_v2 import main


if __name__ == "__main__":
    main()
