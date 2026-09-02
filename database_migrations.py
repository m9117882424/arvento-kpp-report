#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small transactional migration runner for the existing PostgreSQL schema."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Callable

import psycopg
import psycopg.rows
from fastapi import FastAPI


MIGRATIONS_DIR = Path(__file__).resolve().parent / "sql" / "migrations"
MIGRATION_NAME = re.compile(r"^(\d{3,})_[a-z0-9_]+\.sql$")
LOCK_NAME = "arvento-report-schema-migrations"


def discover_migrations(folder: Path = MIGRATIONS_DIR) -> list[Path]:
    paths = sorted(folder.glob("*.sql"))
    invalid = [path.name for path in paths if MIGRATION_NAME.fullmatch(path.name) is None]
    if invalid:
        raise RuntimeError(f"Некорректные имена миграций: {', '.join(invalid)}")
    versions = [path.name.split("_", 1)[0] for path in paths]
    if len(versions) != len(set(versions)):
        raise RuntimeError("Номера миграций должны быть уникальными")
    return paths


def migration_checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_applied_migration(
    version: str,
    filename: str,
    checksum: str,
    existing: tuple[str, str] | None,
) -> bool:
    if existing is None:
        return False
    if existing != (filename, checksum):
        raise RuntimeError(f"Применённая миграция {version} была изменена: {filename}")
    return True


def run_migrations(
    connection: psycopg.Connection,
    folder: Path = MIGRATIONS_DIR,
) -> list[str]:
    """Apply pending SQL files under one PostgreSQL advisory transaction lock."""
    applied_now: list[str] = []
    # Callers may use dict_row (the cache reader does). Migration metadata is
    # positional by design, so isolate the runner from the connection default.
    with connection.cursor(row_factory=psycopg.rows.tuple_row) as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (LOCK_NAME,))
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                checksum_sha256 TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        cursor.execute("SELECT version, filename, checksum_sha256 FROM schema_migrations")
        applied = {
            str(version): (str(filename), str(checksum))
            for version, filename, checksum in cursor.fetchall()
        }

        for path in discover_migrations(folder):
            version = path.name.split("_", 1)[0]
            checksum = migration_checksum(path)
            existing = applied.get(version)
            if _validate_applied_migration(version, path.name, checksum, existing):
                continue

            # Claim the version before executing its SQL. ON CONFLICT is a
            # second line of defence if several application processes start
            # together or a caller enters with an older transaction snapshot.
            cursor.execute(
                """
                INSERT INTO schema_migrations(version, filename, checksum_sha256)
                VALUES (%s,%s,%s)
                ON CONFLICT (version) DO NOTHING
                RETURNING version
                """,
                (version, path.name, checksum),
            )
            claimed = cursor.fetchone()
            if claimed is None:
                cursor.execute(
                    """
                    SELECT filename, checksum_sha256
                    FROM schema_migrations
                    WHERE version=%s
                    """,
                    (version,),
                )
                concurrent = cursor.fetchone()
                if concurrent is None:
                    raise RuntimeError(
                        f"Не удалось зарегистрировать миграцию {version}: {path.name}"
                    )
                _validate_applied_migration(
                    version,
                    path.name,
                    checksum,
                    (str(concurrent[0]), str(concurrent[1])),
                )
                continue

            cursor.execute(path.read_text(encoding="utf-8"))
            applied_now.append(path.name)
    return applied_now


def register_database_migrations(
    app: FastAPI,
    database_url: Callable[[], str],
) -> None:
    """Run migrations before the application starts accepting requests."""
    if getattr(app.state, "database_migrations_registered", False):
        return

    def migrate_on_startup() -> None:
        with psycopg.connect(database_url(), connect_timeout=15) as connection:
            run_migrations(connection)
            connection.commit()

    app.router.add_event_handler("startup", migrate_on_startup)
    app.state.database_migrations_registered = True


__all__ = [
    "MIGRATIONS_DIR",
    "discover_migrations",
    "migration_checksum",
    "_validate_applied_migration",
    "register_database_migrations",
    "run_migrations",
]
