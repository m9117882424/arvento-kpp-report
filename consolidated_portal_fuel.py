#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ASGI entrypoint enabling Fuel Monitor enrichment for the consolidated report."""
from __future__ import annotations

import consolidated_portal as portal
from fuel_enriched_consolidated_report import generate_multi_roster_report

# ``generate_consolidated_web`` resolves this module global at request time.
portal.generate_multi_roster_report = generate_multi_roster_report
app = portal.app

__all__ = ["app"]
