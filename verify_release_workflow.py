#!/usr/bin/env python3
"""Deterministic success and rollback checks for the production release wrapper."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
RELEASE = ROOT / "deploy" / "release.sh"
SHA = "a" * 40


def executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def scenario(folder: Path, *, install_fails: bool) -> subprocess.CompletedProcess[str]:
    checkout = folder / "checkout"
    binary = folder / "bin"
    sbin = folder / "sbin"
    systemd = folder / "systemd"
    state = folder / "state"
    defaults = folder / "arvento-report"
    log = folder / "commands.log"
    for path in (checkout / ".git", checkout / "deploy", binary, sbin, systemd):
        path.mkdir(parents=True, exist_ok=True)

    (checkout / ".env").write_text("ARVENTO_IMAGE_TAG=local\n", encoding="utf-8")
    (checkout / "docker-compose.server.yml").write_text(
        "name: arvento_report\n", encoding="utf-8"
    )
    defaults.write_text("ARVENTO_ROOT=/previous\n", encoding="utf-8")
    executable(sbin / "arvento-healthcheck", "#!/bin/sh\nexit 0\n")
    executable(
        folder / "smoke",
        "#!/bin/sh\nprintf 'smoke %s\\n' \"$*\" >> \"$TEST_LOG\"\nexit 0\n",
    )
    executable(
        binary / "git",
        f"""#!/bin/sh
case "$*" in
  *"status --porcelain"*) exit 0 ;;
  *"rev-parse --verify HEAD"*) printf '%s\\n' {SHA}; exit 0 ;;
esac
exit 1
""",
    )
    executable(
        binary / "docker",
        """#!/bin/sh
printf 'docker %s\n' "$*" >> "$TEST_LOG"
case "$*" in
  *"ps -q report-portal"*) printf '%s\n' portal-container; exit 0 ;;
  "inspect -f {{.Image}} portal-container") printf '%s\n' sha256:previous; exit 0 ;;
  "image inspect arvento-report:git-"*) exit 1 ;;
esac
exit 0
""",
    )
    executable(
        binary / "systemctl",
        "#!/bin/sh\nprintf 'systemctl %s\\n' \"$*\" >> \"$TEST_LOG\"\nexit 0\n",
    )
    executable(
        binary / "bash",
        """#!/bin/sh
printf 'bash %s\n' "$*" >> "$TEST_LOG"
if [ "${TEST_INSTALL_FAIL:-0}" = 1 ]; then exit 23; fi
exit 0
""",
    )

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{binary}:{environment['PATH']}",
            "TEST_LOG": str(log),
            "TEST_INSTALL_FAIL": "1" if install_fails else "0",
            "ARVENTO_RELEASE_SKIP_ROOT_CHECK": "1",
            "ARVENTO_RELEASE_STATE_DIR": str(state),
            "ARVENTO_SBIN_DIR": str(sbin),
            "ARVENTO_SYSTEMD_DIR": str(systemd),
            "ARVENTO_DEFAULTS_PATH": str(defaults),
            "ARVENTO_RELEASE_LOCK_FILE": str(folder / "release.lock"),
            "ARVENTO_SMOKE_SCRIPT": str(folder / "smoke"),
            "ARVENTO_PYTHON_BIN": sys.executable,
        }
    )
    result = subprocess.run(
        ["/bin/bash", str(RELEASE), str(checkout)],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )
    env_text = (checkout / ".env").read_text(encoding="utf-8")
    commands = log.read_text(encoding="utf-8")
    if install_fails:
        assert result.returncode == 23, result.stdout + result.stderr
        assert "ARVENTO_IMAGE_TAG=rollback-" in env_text
        assert "ROLLBACK OK" in result.stdout
        assert "force-recreate geofence-editor report-portal" in commands
        assert "force-recreate postgres" not in commands
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert f"ARVENTO_IMAGE_TAG=git-{SHA}" in env_text
        assert "RELEASE OK" in result.stdout
        current = (state / "current.env").read_text(encoding="utf-8")
        assert f"CURRENT_COMMIT_SHA={SHA}" in current
        assert f"CURRENT_IMAGE_TAG=git-{SHA}" in current
    return result


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="arvento_release_success_") as name:
        scenario(Path(name), install_fails=False)
    with tempfile.TemporaryDirectory(prefix="arvento_release_rollback_") as name:
        scenario(Path(name), install_fails=True)
    print("OK: release success and automatic image rollback verified")


if __name__ == "__main__":
    main()
