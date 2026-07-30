from __future__ import annotations

"""Production ASGI entrypoint with explicit, testable portal integration."""

import consolidated_portal as portal
from consolidated_cache_portal import apply_cache_portal
from consolidated_time_logic import apply_consolidated_date_preview
from fuel_enriched_consolidated_report import generate_multi_roster_report
from kpp_preview_format import apply_kpp_preview_format
from portal_runtime_patch import apply_runtime_patch
from roster_management_portal import apply_roster_management_portal

# The consolidated report builder is resolved from the portal module at request time.
portal.generate_multi_roster_report = generate_multi_roster_report
apply_runtime_patch()
apply_kpp_preview_format(portal.implementation)
apply_consolidated_date_preview(portal.implementation)
apply_cache_portal()
apply_roster_management_portal()
app = portal.app

__all__ = ["app"]
