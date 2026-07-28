from __future__ import annotations

"""Production ASGI entrypoint with explicit, testable portal integration."""

import consolidated_portal as portal
from kpp_preview_format import apply_kpp_preview_format
from portal_runtime_patch import apply_runtime_patch

apply_runtime_patch()
apply_kpp_preview_format(portal.implementation)
app = portal.app

__all__ = ["app"]
