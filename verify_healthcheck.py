#!/usr/bin/env python3
"""Deterministic checks for the host-side operational healthcheck."""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
HEALTHCHECK = ROOT / "deploy" / "arvento-healthcheck.sh"


FAKE_DOCKER = r"""#!/usr/bin/env bash
set -eu
if [[ " $* " == *" inspect "* ]]; then
    echo healthy
    exit 0
fi
if [[ " $* " == *" ps -q "* ]]; then
    echo fake-container
    exit 0
fi
if [[ " $* " == *" ps "* ]]; then
    echo "fake compose services healthy"
    exit 0
fi
if [[ " $* " == *" exec -i "* ]]; then
    query="$(cat)"
    if [[ "$query" == *"WHERE status <> 'RUNNING'"* ]]; then
        echo "${FAKE_SYNC_STATUS:-SUCCESS}"
    elif [[ "$query" == *"max(finished_at)"* ]]; then
        echo "${FAKE_SYNC_AGE_SECONDS:-60}"
    elif [[ "$query" == *"make_interval"* ]]; then
        echo "${FAKE_STALE_RUNNING:-0}"
    fi
    exit 0
fi
if [[ " $* " == *" exec "* ]]; then
    exit 0
fi
exit 1
"""

FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
set -eu
case "$1" in
    is-active)
        if [[ "$2" == *.timer ]]; then echo active; else echo inactive; fi
        ;;
    is-enabled) echo enabled ;;
    show)
        if [[ " $* " == *"property=Result"* ]]; then echo success; else echo 0; fi
        ;;
    list-timers) echo "arvento timers scheduled" ;;
    *) exit 1 ;;
esac
"""

FAKE_DF = r"""#!/usr/bin/env bash
echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
echo "/dev/fake 100000 20000 80000 20% /"
"""


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def run_scenario(
    folder: Path,
    *,
    sync_status: str = "SUCCESS",
    sync_age_seconds: int = 60,
    stale_running: int = 0,
    backup_age_seconds: int = 60,
) -> subprocess.CompletedProcess[str]:
    root = folder / "app"
    root.mkdir(exist_ok=True)
    (root / "docker-compose.server.yml").write_text("services: {}\n", encoding="utf-8")
    backup_dir = folder / "backups"
    backup_dir.mkdir(exist_ok=True)
    backup = backup_dir / "arvento_report_test.dump"
    backup.write_bytes(b"verified-backup")
    timestamp = time.time() - backup_age_seconds
    os.utime(backup, (timestamp, timestamp))

    bin_dir = folder / "bin"
    bin_dir.mkdir(exist_ok=True)
    write_executable(bin_dir / "docker", FAKE_DOCKER)
    write_executable(bin_dir / "systemctl", FAKE_SYSTEMCTL)
    write_executable(bin_dir / "curl", "#!/usr/bin/env bash\nexit 0\n")
    write_executable(bin_dir / "free", "#!/usr/bin/env bash\necho 'memory healthy'\n")
    write_executable(bin_dir / "df", FAKE_DF)

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{bin_dir}:{environment['PATH']}",
            "ARVENTO_DEFAULTS_FILE": str(folder / "missing-defaults"),
            "ARVENTO_ROOT": str(root),
            "BACKUP_DIR": str(backup_dir),
            "FAKE_SYNC_STATUS": sync_status,
            "FAKE_SYNC_AGE_SECONDS": str(sync_age_seconds),
            "FAKE_STALE_RUNNING": str(stale_running),
        }
    )
    return subprocess.run(
        ["bash", str(HEALTHCHECK)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="arvento_healthcheck_") as temp_name:
        folder = Path(temp_name)
        healthy = run_scenario(folder)
        assert healthy.returncode == 0, healthy.stdout + healthy.stderr
        assert "OK: core services" in healthy.stdout

    with tempfile.TemporaryDirectory(prefix="arvento_healthcheck_") as temp_name:
        stale_run = run_scenario(Path(temp_name), stale_running=1)
        assert stale_run.returncode == 1
        assert "Зависших RUNNING старше 240 мин: 1" in stale_run.stdout

    with tempfile.TemporaryDirectory(prefix="arvento_healthcheck_") as temp_name:
        failed_sync = run_scenario(Path(temp_name), sync_status="FAILED")
        assert failed_sync.returncode == 1
        assert "последний завершённый sync имеет статус FAILED" in failed_sync.stdout

    with tempfile.TemporaryDirectory(prefix="arvento_healthcheck_") as temp_name:
        stale_backup = run_scenario(Path(temp_name), backup_age_seconds=31 * 3600)
        assert stale_backup.returncode == 1
        assert "backup старше 30 ч" in stale_backup.stdout

    print("OK: operational healthcheck detects stale runs, sync failures and old backups")


if __name__ == "__main__":
    main()
