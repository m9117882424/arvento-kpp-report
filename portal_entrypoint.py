from __future__ import annotations

"""Production ASGI entrypoint with explicit, testable portal integration."""

import consolidated_portal as portal
from portal_runtime_patch import apply_runtime_patch

apply_runtime_patch()
app = portal.app

__all__ = ["app"]
