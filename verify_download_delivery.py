#!/usr/bin/env python3
"""Offline checks for non-Base64 report delivery and route registration."""
from __future__ import annotations

import base64
import tempfile
from pathlib import Path

import portal_entrypoint
from download_store import materialize_download, resolve_download, restore_legacy_base64


def main() -> None:
    content = b"PK\x03\x04test-xlsx"
    with tempfile.TemporaryDirectory(prefix="verify_download_store_") as temp_name:
        result = materialize_download(
            {
                "filename": "Сводный_отчет.xlsx",
                "excel_base64": base64.b64encode(content).decode("ascii"),
            },
            Path(temp_name),
        )
        assert "excel_base64" not in result
        assert result["download_url"].startswith("/api/download/")
        token = result["download_url"].rsplit("/", 1)[-1]
        entry = resolve_download(token)
        assert entry.path.read_bytes() == content
        assert entry.filename == "Сводный_отчет.xlsx"
        legacy = restore_legacy_base64(dict(result))
        assert base64.b64decode(legacy["excel_base64"]) == content
        assert "download_url" not in legacy

    paths = [route.path for route in portal_entrypoint.app.routes]
    assert paths.count("/api/download/{token}") == 1
    html = portal_entrypoint.portal.implementation.HTML
    assert "downloadUrl = payload.download_url || '';" in html
    assert "if (downloadUrl)" in html
    print("OK: v3 report delivery uses short-lived binary download URLs")


if __name__ == "__main__":
    main()
