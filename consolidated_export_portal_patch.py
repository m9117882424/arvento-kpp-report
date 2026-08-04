#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Finalize consolidated downloads after internal cache processing is complete."""
from __future__ import annotations

import base64
import tempfile
from pathlib import Path
from typing import Any, Callable

import consolidated_portal as portal
from consolidated_export_layout import finalize_consolidated_export

_BASE_GENERATOR: Callable[..., dict[str, Any]] | None = None
_PATCHED = False


def generate_final_consolidated_download(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Run the established generator, then simplify only the downloadable XLSX."""
    if _BASE_GENERATOR is None:
        raise RuntimeError("Генератор сводного отчёта не инициализирован")

    result = _BASE_GENERATOR(*args, **kwargs)
    excel_base64 = str(result.get("excel_base64", ""))
    filename = Path(str(result.get("filename", "Сводный_отчет.xlsx"))).name
    if not excel_base64:
        return result

    with tempfile.TemporaryDirectory(prefix="arvento_final_consolidated_") as temp_name:
        output_path = Path(temp_name) / filename
        output_path.write_bytes(base64.b64decode(excel_base64))
        layout_stats = finalize_consolidated_export(
            output_path,
            portal.implementation.db_url(),
        )
        columns, rows, total_rows = portal.implementation.workbook_preview(output_path)
        result["excel_base64"] = base64.b64encode(output_path.read_bytes()).decode("ascii")
        result["columns"] = columns
        result["rows"] = rows
        result["preview_truncated"] = total_rows > len(rows)
        summary = result.setdefault("summary", {})
        summary["Листов Excel"] = layout_stats["sheets"]
        summary["Строк дополнено разнарядкой"] = layout_stats["enriched_rows"]

    return result


def apply_consolidated_export_portal() -> None:
    """Wrap the final central-roster consolidated generator once."""
    global _BASE_GENERATOR, _PATCHED
    if _PATCHED:
        return

    _BASE_GENERATOR = portal.generate_consolidated_web
    portal.generate_consolidated_web = generate_final_consolidated_download
    _PATCHED = True


__all__ = [
    "apply_consolidated_export_portal",
    "generate_final_consolidated_download",
]
