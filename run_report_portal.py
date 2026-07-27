#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Canonical ASGI entrypoint for the report portal.

Keeps the compatibility implementation in ``report_portal.py`` while exposing
current user-facing report names.
"""

from __future__ import annotations

from typing import Any

import report_portal as implementation


implementation.HTML = implementation.HTML.replace(
    '<option value="violation">Запрещённый поворот</option>',
    '<option value="violation">Нарушения</option>',
)

_original_generate_report = implementation.generate_report


def generate_report(*args: Any, **kwargs: Any) -> dict[str, Any]:
    result = _original_generate_report(*args, **kwargs)
    report_type = args[0] if args else kwargs.get("report_type")
    if report_type == "violation":
        result["filename"] = str(result["filename"]).replace(
            "Запрещенный_поворот_",
            "Нарушения_",
        )
        result["summary"]["Отчёт"] = "Нарушения"
    return result


implementation.generate_report = generate_report
app = implementation.app

__all__ = ["app"]
