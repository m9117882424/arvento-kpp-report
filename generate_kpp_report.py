#!/usr/bin/env python3
"""Backward-compatible wrapper for the consolidated KPP efficiency report."""

import runpy


if __name__ == "__main__":
    runpy.run_module("arvento_kpp_report", run_name="__main__")
