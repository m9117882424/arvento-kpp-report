#!/usr/bin/env python3
"""Run the configured scheduled report generation workflow."""

import runpy


if __name__ == "__main__":
    runpy.run_module("run_automated_reports", run_name="__main__")
