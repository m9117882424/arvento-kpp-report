#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Short-lived file downloads for generated reports without Base64 JSON."""
from __future__ import annotations

import base64
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Path as ApiPath
from fastapi.responses import FileResponse

from runtime_settings import report_runtime_settings


_SETTINGS = report_runtime_settings()
DOWNLOAD_DIR = _SETTINGS.download_dir
DOWNLOAD_TTL_SECONDS = _SETTINGS.download_ttl_seconds
DOWNLOAD_MAX_FILES = _SETTINGS.download_max_files
DOWNLOAD_MAX_BYTES = _SETTINGS.download_max_bytes


@dataclass(frozen=True, slots=True)
class DownloadEntry:
    token: str
    path: Path
    filename: str
    expires_at: float


_ENTRIES: dict[str, DownloadEntry] = {}
_LOCK = threading.Lock()


def _safe_filename(value: Any) -> str:
    filename = Path(str(value or "Отчет.xlsx")).name
    return filename if filename.lower().endswith(".xlsx") else f"{filename}.xlsx"


def _cleanup_locked(now: float) -> None:
    expired = [token for token, entry in _ENTRIES.items() if entry.expires_at <= now]
    for token in expired:
        entry = _ENTRIES.pop(token)
        entry.path.unlink(missing_ok=True)

    overflow = max(0, len(_ENTRIES) - DOWNLOAD_MAX_FILES + 1)
    if overflow:
        oldest = sorted(_ENTRIES.values(), key=lambda item: item.expires_at)[:overflow]
        for entry in oldest:
            _ENTRIES.pop(entry.token, None)
            entry.path.unlink(missing_ok=True)


def materialize_download(
    result: dict[str, Any],
    directory: Path = DOWNLOAD_DIR,
) -> dict[str, Any]:
    encoded = str(result.get("excel_base64") or "")
    if not encoded:
        return result
    try:
        content = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise RuntimeError("Генератор вернул повреждённый Excel") from exc
    if len(content) > DOWNLOAD_MAX_BYTES:
        raise RuntimeError("Сформированный Excel превышает допустимый размер")

    directory.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    target = directory / f"{token}.xlsx"
    temporary = directory / f"{token}.tmp"
    temporary.write_bytes(content)
    temporary.replace(target)
    entry = DownloadEntry(
        token=token,
        path=target,
        filename=_safe_filename(result.get("filename")),
        expires_at=time.time() + DOWNLOAD_TTL_SECONDS,
    )
    with _LOCK:
        _cleanup_locked(time.time())
        _ENTRIES[token] = entry

    result.pop("excel_base64", None)
    result["download_url"] = f"/api/download/{token}"
    result["download_expires_in_seconds"] = DOWNLOAD_TTL_SECONDS
    return result


def resolve_download(token: str) -> DownloadEntry:
    with _LOCK:
        _cleanup_locked(time.time())
        entry = _ENTRIES.get(token)
    if entry is None or not entry.path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден или срок скачивания истёк")
    return entry


def restore_legacy_base64(result: dict[str, Any]) -> dict[str, Any]:
    """Preserve deprecated v1/v2 response shape after delegating through v3."""
    url = str(result.get("download_url") or "")
    if not url or result.get("excel_base64"):
        return result
    token = url.rsplit("/", 1)[-1]
    entry = resolve_download(token)
    result["excel_base64"] = base64.b64encode(entry.path.read_bytes()).decode("ascii")
    result.pop("download_url", None)
    result.pop("download_expires_in_seconds", None)
    return result


def apply_download_routes(app: FastAPI) -> None:
    if getattr(app.state, "download_routes_applied", False):
        return

    @app.get("/api/download/{token}", include_in_schema=False)
    def download_report(
        token: str = ApiPath(min_length=20, max_length=64),
    ) -> FileResponse:
        entry = resolve_download(token)
        return FileResponse(
            entry.path,
            filename=entry.filename,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Cache-Control": "private, no-store"},
        )

    app.state.download_routes_applied = True


__all__ = [
    "apply_download_routes",
    "materialize_download",
    "resolve_download",
    "restore_legacy_base64",
]
