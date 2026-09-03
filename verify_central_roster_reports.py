#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke test for the central roster source and final export integration."""
from __future__ import annotations

import central_roster_reports
import consolidated_cache
import consolidated_export_portal_patch
import portal_entrypoint
import portal_runtime_patch
import roster_cache_policy
import roster_management_portal
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

    # Cache policy regression: roster revisions no longer invalidate heavy GPS
    # metrics; cached rows are enriched from the current effective roster and
    # duplicate semantic uploads do not advance roster loaded_at.
    assert (
        consolidated_cache.load_cached_rows
        is roster_cache_policy.load_cached_rows_with_current_roster
    )
    assert (
        roster_management_portal.save_roster_uploads
        is roster_cache_policy.save_roster_uploads_if_changed
    )
    assert (
        central_roster_reports.cache_complete
        is roster_cache_policy.cache_complete_with_logging
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
        "OK: central roster reports use roster-independent GPS cache with current roster overlay"
    )


if __name__ == "__main__":
    main()
