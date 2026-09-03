#!/usr/bin/env python3
"""Single entrypoint for repository verification.

Host-side deployment checks use ``--static`` and require only the Python
standard library. Runtime checks execute inside the server image, where the
locked application dependencies are installed.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent

STATIC_CHECKS = (
    "verify_repository.py",
    "verify_deployment.py",
    "verify_healthcheck.py",
)

RUNTIME_CHECKS = (
    "verify_runtime.py",
    "verify_left_turn_runtime.py",
    "verify_kpp_preview.py",
    "verify_arvento_sync_batching.py",
    "verify_consolidated_time_logic.py",
    "verify_consolidated_mileage_logic.py",
    "verify_consolidated_performance.py",
    "verify_central_roster_reports.py",
    "verify_consolidated_export_layout.py",
    "verify_consolidated_export_optimization.py",
    "verify_fleet_dashboard_api.py",
    "verify_mileage_review_policy.py",
    "verify_cache_freshness.py",
    "verify_database_migrations.py",
    "verify_download_delivery.py",
    "verify_operational_geofences.py",
    "verify_report_contracts.py",
    "verify_generation_jobs.py",
    "verify_speed_threshold_defaults.py",
)

# Requires a live production database and an explicit report day.
MANUAL_CHECKS = ("verify_vehicle_distance_day.py",)


def validate_check_inventory() -> None:
    discovered = {
        path.name
        for path in ROOT.glob("verify_*.py")
        if path.name != Path(__file__).name
    }
    configured = set(STATIC_CHECKS + RUNTIME_CHECKS + MANUAL_CHECKS)
    missing = sorted(discovered - configured)
    unknown = sorted(configured - discovered)
    if missing or unknown:
        details = []
        if missing:
            details.append("unclassified=" + ",".join(missing))
        if unknown:
            details.append("missing=" + ",".join(unknown))
        raise SystemExit("Invalid verification inventory: " + "; ".join(details))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Arvento verification suites")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--static",
        action="store_true",
        help="run dependency-free repository and deployment checks",
    )
    group.add_argument(
        "--runtime",
        action="store_true",
        help="run application checks that require locked dependencies",
    )
    return parser.parse_args()


def run_checks(checks: tuple[str, ...]) -> None:
    environment = os.environ.copy()
    environment["ARVENTO_RUNTIME_CHECK_STRICT"] = "1"
    for script in checks:
        path = ROOT / script
        if not path.is_file():
            raise SystemExit(f"Verification script is missing: {script}")
        print(f"\n=== {script} ===", flush=True)
        completed = subprocess.run(
            [sys.executable, str(path)],
            cwd=ROOT,
            env=environment,
            check=False,
        )
        if completed.returncode != 0:
            raise SystemExit(
                f"Verification failed: {script} (rc={completed.returncode})"
            )


def main() -> None:
    args = parse_args()
    validate_check_inventory()
    if args.static:
        checks = STATIC_CHECKS
    elif args.runtime:
        checks = RUNTIME_CHECKS
    else:
        checks = STATIC_CHECKS + RUNTIME_CHECKS
    run_checks(checks)
    print(f"\nOK: verification suites passed; checks={len(checks)}")


if __name__ == "__main__":
    main()
