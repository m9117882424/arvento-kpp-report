#!/usr/bin/env python3
"""Generate the consolidated KPP report from an Arvento export."""

import runpy


if __name__ == "__main__":
    runpy.run_module("arvento_kpp_report", run_name="__main__")
