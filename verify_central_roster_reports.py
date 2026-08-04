#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke test for the central roster source and final export integration."""
from __future__ import annotations

import central_roster_reports
import consolidated_export_portal_patch
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

    # The portal's public consolidated generator is now the final-download
    # wrapper. Its preserved base generator must remain the central-roster
    # implementation so cache reads/calculations still use the authoritative
    # PostgreSQL roster store before service sheets are removed from the XLSX.
    assert (
        portal_entrypoint.portal.generate_consolidated_web
        is consolidated_export_portal_patch.generate_final_consolidated_download
    )
    assert (
        consolidated_export_portal_patch._BASE_GENERATOR
        is central_roster_reports.generate_consolidated_from_central_store
    )
    print(
        "OK: все отчёты используют центральную базу разнарядок; "
        "сводная выгрузка проходит финальную обработку"
    )


if __name__ == "__main__":
    main()
