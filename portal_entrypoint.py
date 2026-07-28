from __future__ import annotations

"""Production ASGI entrypoint with explicit portal runtime fixes."""

import consolidated_portal as base
import portal_runtime_patch  # noqa: F401

app = base.app

__all__ = ["app"]
