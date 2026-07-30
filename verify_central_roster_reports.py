#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke test for the central roster source integration."""
from __future__ import annotations

import central_roster_reports
import portal_entrypoint
import portal_runtime_patch
import run_report_portal


def main() -> None:
    html = portal_entrypoint.portal.implementation.HTML
    assert "#rosterBox, #consolidatedRosterBox { display:none !important; }" in html
    assert "roster.required = false;" in html
    assert "consolidatedRosters.required = false;" in html
    assert "Все отчёты используют разнарядки из центральной базы" in html
    assert (
        run_report_portal.generate_report_with_thresholds
        is portal_runtime_patch.generate_report_with_regional_summary
    )
    assert (
        portal_runtime_patch._original_generate_report
        is central_roster_reports.generate_report_from_central_roster
    )
    assert (
        portal_entrypoint.portal.generate_consolidated_web
        is central_roster_reports.generate_consolidated_from_central_store
    )
    print("OK: все отчёты используют центральную базу разнарядок")


if __name__ == "__main__":
    main()
