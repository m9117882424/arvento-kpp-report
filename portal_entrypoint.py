from __future__ import annotations

"""Production ASGI entrypoint with explicit, testable portal integration."""

from speed_threshold_defaults import apply_speed_threshold_defaults

# Speed defaults must be patched before the portal imports the constants into
# its form and API layers.
apply_speed_threshold_defaults()

import consolidated_portal as portal
from central_roster_reports import apply_central_roster_reports
from consolidated_cache_portal import apply_cache_portal
from consolidated_time_logic import apply_consolidated_date_preview
from database_status_patch import apply_database_status_patch
from extended_roster_fields import apply_extended_roster_fields
from fuel_enriched_consolidated_report import generate_multi_roster_report
from kpp_preview_format import apply_kpp_preview_format
from portal_runtime_patch import apply_runtime_patch
from roster_management_portal import apply_roster_management_portal

# The consolidated report builder is resolved from the portal module at request time.
portal.generate_multi_roster_report = generate_multi_roster_report
apply_runtime_patch()
apply_kpp_preview_format(portal.implementation)
apply_consolidated_date_preview(portal.implementation)
apply_extended_roster_fields()
apply_cache_portal()
apply_roster_management_portal()
# Reapply after the roster page is fully initialized, then make it the only source.
apply_extended_roster_fields()
apply_central_roster_reports()
apply_database_status_patch(
    portal.app,
    portal.implementation.db_url,
    portal.implementation.TZ,
)
app = portal.app

__all__ = ["app"]
