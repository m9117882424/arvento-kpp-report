#!/usr/bin/env python3
"""Validate the production env file before Docker build or service restart."""
from __future__ import annotations

import argparse
from pathlib import Path

from runtime_settings import (
    ConfigurationError,
    load_env_file,
    validate_server_environment,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("env_file", type=Path)
    args = parser.parse_args()
    try:
        values = load_env_file(args.env_file)
        validate_server_environment(values)
    except (OSError, ConfigurationError) as exc:
        raise SystemExit(f"ERROR: {exc}") from None
    print("OK: обязательные и runtime-настройки production валидны")


if __name__ == "__main__":
    main()
