#!/usr/bin/env python3
"""Deployment and repository hygiene checks.

The checks are offline and safe to run on a workstation, in CI, during an image
build, or on a production checkout. They validate the deployment files required
for a clean-server installation and catch runtime artifacts, literal secrets,
superseded schedulers and stale Dockerfile references before deployment.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent

REQUIRED_FILES = (
    ".dockerignore",
    ".env.server.example",
    "Dockerfile.server",
    "docker-compose.server.yml",
    "SERVER_DEPLOY.md",
    "verify_all.py",
    "verify_healthcheck.py",
    "verify_release_workflow.py",
    "consolidated_incremental_cache.py",
    "deploy/arvento-backup.sh",
    "deploy/arvento-healthcheck.sh",
    "deploy/post-deploy-smoke.sh",
    "deploy/release.sh",
    "deploy/restore-drill.sh",
    "deploy/arvento-sync-and-cache.sh",
    "deploy/install.sh",
    "deploy/nginx/arvento-report.conf.example",
    "deploy/systemd/arvento-backup.service",
    "deploy/systemd/arvento-backup.timer",
    "deploy/systemd/arvento-intraday-pipeline.service",
    "deploy/systemd/arvento-intraday-pipeline.timer",
    "deploy/systemd/arvento-nightly-correction.service",
    "deploy/systemd/arvento-nightly-correction.timer",
)

# These modules remain only because canonical wrappers import them. They are not
# independent production entrypoints and must not be referenced by Compose,
# systemd or new deployment instructions.
LEGACY_COMPATIBILITY_FILES = {
    "arvento_first_entry_report_fixed.py",
    "arvento_postgres_sync_v2.py",
    "arvento_kpp_report.py",
    "geofence_editor_api.py",
    "prohibited_left_turn_report.py",
    "run_automated_reports.py",
}

FORBIDDEN_TRACKED_PATHS = {
    # Superseded by the unified sync-and-cache pipeline. The old service had an
    # infinite timeout and its own overlapping schedule.
    "deploy/systemd/arvento-consolidated-cache.service",
    "deploy/systemd/arvento-consolidated-cache.timer",
}

FORBIDDEN_TRACKED_NAMES = {
    ".env",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
    "service-account.json",
}

FORBIDDEN_TRACKED_SUFFIXES = {
    ".bak",
    ".csv",
    ".db",
    ".dump",
    ".gz",
    ".key",
    ".old",
    ".orig",
    ".p12",
    ".pem",
    ".pfx",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".xls",
    ".xlsm",
    ".xlsx",
    ".zip",
}

FORBIDDEN_TRACKED_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "backups",
    "logs",
    "reports",
    "venv",
}

TEXT_SUFFIXES = {
    ".example",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".txt",
    ".yaml",
    ".yml",
}

LITERAL_SECRET_PATTERNS = (
    (
        "заполненная переменная доступа",
        re.compile(
            r"(?mi)^\s*(?:ARVENTO_USER|ARVENTO_PIN1|ARVENTO_PIN2|"
            r"POSTGRES_PASSWORD)\s*=\s*(?!$|CHANGE_ME|<)[^\s#]+"
        ),
    ),
    (
        "пароль в PostgreSQL URL",
        re.compile(
            r"(?mi)^\s*DATABASE_URL\s*=\s*postgres(?:ql)?://[^:\s]+:"
            r"(?!CHANGE_ME|<)[^@\s]+@"
        ),
    ),
    ("Google API key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
)

# Build the markers from fragments so this checker does not flag its own
# source code as if it contained an actual private key block.
PRIVATE_KEY_MARKERS = (
    "-----BEGIN " + "PRIVATE KEY-----",
    "-----BEGIN OPENSSH " + "PRIVATE KEY-----",
)

EXPECTED_BACKUP_PATHS = {
    "deploy/arvento-backup.sh",
    "deploy/systemd/arvento-backup.service",
    "deploy/systemd/arvento-backup.timer",
}

MAX_TRACKED_FILE_BYTES = 5 * 1024 * 1024


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def tracked_files() -> list[Path]:
    """Return tracked files when Git metadata exists, otherwise image files."""
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z"],
            check=True,
            capture_output=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return [
            path
            for path in ROOT.rglob("*")
            if path.is_file() and ".git" not in path.parts
        ]

    return [
        ROOT / raw.decode("utf-8")
        for raw in result.stdout.split(b"\0")
        if raw
    ]


def check_required_files(errors: list[str]) -> None:
    for relative_path in REQUIRED_FILES:
        if not (ROOT / relative_path).is_file():
            errors.append(f"Отсутствует файл для развёртывания: {relative_path}")


def check_dockerfile_references(errors: list[str]) -> None:
    dockerfile = read_text("Dockerfile.server")
    referenced = {
        match.group(1)
        for match in re.finditer(r"/app/([A-Za-z0-9_./-]+\.py)\b", dockerfile)
    }
    for relative_path in sorted(referenced):
        if not (ROOT / relative_path).is_file():
            errors.append(
                "Dockerfile.server ссылается на отсутствующий файл: "
                f"{relative_path}"
            )

    required_tokens = (
        "FROM python:3.12-slim-bookworm",
        "COPY --chown=app:app . /app",
        "python /app/verify_all.py --static",
        "python /app/verify_all.py --runtime",
        "python -m compileall -q /app",
        "USER app",
        "ARG ARVENTO_COMMIT_SHA=unknown",
        "org.opencontainers.image.revision=$ARVENTO_COMMIT_SHA",
    )
    for token in required_tokens:
        if token not in dockerfile:
            errors.append(f"Dockerfile.server: отсутствует обязательный элемент {token}")


def check_compose(errors: list[str]) -> None:
    compose = read_text("docker-compose.server.yml")
    required_tokens = (
        "name: arvento_report",
        "postgres:",
        "gps-sync:",
        "profiles:",
        "legacy-daemon",
        "geofence-editor:",
        "report-portal:",
        "healthcheck:",
        "127.0.0.1:${GEOFENCE_EDITOR_PORT:-18083}:8080",
        "127.0.0.1:${REPORT_PORTAL_PORT:-18084}:8080",
        "max-size:",
        "max-file:",
        "ARVENTO_COMMIT_SHA: ${ARVENTO_COMMIT_SHA:-unknown}",
    )
    for token in required_tokens:
        if token not in compose:
            errors.append(f"docker-compose.server.yml: отсутствует {token}")

    if 'POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-' in compose:
        errors.append("docker-compose.server.yml: пароль PostgreSQL не должен иметь default")


def check_systemd(errors: list[str]) -> None:
    required_services = (
        "deploy/systemd/arvento-intraday-pipeline.service",
        "deploy/systemd/arvento-nightly-correction.service",
        "deploy/systemd/arvento-backup.service",
    )
    for relative_path in required_services:
        content = read_text(relative_path)
        if "EnvironmentFile=-/etc/default/arvento-report" not in content:
            errors.append(f"{relative_path}: отсутствует общий EnvironmentFile")

    for service_path in sorted((ROOT / "deploy/systemd").glob("*.service")):
        content = service_path.read_text(encoding="utf-8")
        if "TimeoutStartSec=infinity" in content:
            errors.append(
                f"{service_path.relative_to(ROOT)}: бесконечный timeout запрещён"
            )
        if "TimeoutStartSec=" not in content:
            errors.append(
                f"{service_path.relative_to(ROOT)}: отсутствует конечный TimeoutStartSec"
            )

    timer_expectations = {
        "deploy/systemd/arvento-intraday-pipeline.timer": (
            "01..23:00:00 Europe/Istanbul",
            "01..23:30:00 Europe/Istanbul",
            "Persistent=true",
        ),
        "deploy/systemd/arvento-nightly-correction.timer": (
            "00:10:00 Europe/Istanbul",
            "Persistent=true",
        ),
        "deploy/systemd/arvento-backup.timer": (
            "03:30:00 Europe/Istanbul",
            "Persistent=true",
        ),
    }
    for relative_path, tokens in timer_expectations.items():
        content = read_text(relative_path)
        for token in tokens:
            if token not in content:
                errors.append(f"{relative_path}: отсутствует {token}")


def check_scripts(errors: list[str]) -> None:
    pipeline = read_text("deploy/arvento-sync-and-cache.sh")
    for token in (
        "set -Eeuo pipefail",
        "/run/arvento-sync-and-cache.lock",
        "flock -n",
        "timeout --signal=TERM",
        "sync_arvento_gps_to_postgres.py recent --hours 1",
        "consolidated_cache_worker.py refresh-pending",
        "consolidated_cache_worker.py refresh",
        "if timeout --signal=TERM",
    ):
        if token not in pipeline:
            errors.append(f"deploy/arvento-sync-and-cache.sh: отсутствует {token}")

    if "set +e" in pipeline:
        errors.append(
            "deploy/arvento-sync-and-cache.sh: set +e нельзя использовать вместе с ERR trap"
        )

    healthcheck = read_text("deploy/arvento-healthcheck.sh")
    for token in (
        "ARVENTO_DEFAULTS_FILE",
        'section "LAST JOB RESULTS"',
        'section "BACKUP FRESHNESS"',
        "HEALTHCHECK_MAX_SYNC_AGE_MINUTES",
        "HEALTHCHECK_MAX_BACKUP_AGE_HOURS",
        "HEALTHCHECK_STALE_RUNNING_MINUTES",
        "Последний завершённый sync",
    ):
        if token not in healthcheck:
            errors.append(f"deploy/arvento-healthcheck.sh: отсутствует {token}")

    installer = read_text("deploy/install.sh")
    for token in (
        "docker compose",
        "verify_all.py --static",
        "systemctl daemon-reload",
        "arvento-intraday-pipeline.timer",
        "arvento-nightly-correction.timer",
        "arvento-backup.timer",
        "INSTALL_SKIP_BUILD",
        "/usr/local/sbin/arvento-release",
        "/usr/local/sbin/arvento-restore-drill",
    ):
        if token not in installer:
            errors.append(f"deploy/install.sh: отсутствует {token}")

    release = read_text("deploy/release.sh")
    for token in (
        "/run/arvento-release.lock",
        "git-$TARGET_SHA",
        "rollback-$TIMESTAMP",
        "arvento-backup.service",
        "ROLLBACK_REQUIRED=1",
        "post-deploy-smoke.sh",
        "ARVENTO_IMAGE_TAG",
        "org.opencontainers.image.revision",
    ):
        if token not in release:
            errors.append(f"deploy/release.sh: отсутствует {token}")

    smoke = read_text("deploy/post-deploy-smoke.sh")
    for token in (
        "EXPECTED_TAG",
        "org.opencontainers.image.revision",
        "/usr/local/sbin/arvento-healthcheck",
        "POST_DEPLOY_SMOKE OK",
    ):
        if token not in smoke:
            errors.append(f"deploy/post-deploy-smoke.sh: отсутствует {token}")

    restore = read_text("deploy/restore-drill.sh")
    for token in (
        "/run/arvento-restore-drill.lock",
        "/run/arvento-sync-and-cache.lock",
        "pg_restore --list",
        "arvento_restore_drill_",
        "--exit-on-error",
        "dropdb",
        "RESTORE_DRILL OK",
    ):
        if token not in restore:
            errors.append(f"deploy/restore-drill.sh: отсутствует {token}")

    for script in sorted((ROOT / "deploy").glob("*.sh")):
        completed = subprocess.run(
            ["bash", "-n", str(script)],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            errors.append(
                f"{script.relative_to(ROOT)}: bash -n: {completed.stderr.strip()}"
            )


def check_environment_example(errors: list[str]) -> None:
    content = read_text(".env.server.example")
    required_keys = (
        "POSTGRES_DB=",
        "POSTGRES_USER=",
        "POSTGRES_PASSWORD=CHANGE_ME_LONG_RANDOM_PASSWORD",
        "ARVENTO_USER=",
        "ARVENTO_PIN1=",
        "ARVENTO_PIN2=",
        "ARVENTO_GROUP=",
        "PIPELINE_INTRADAY_TIMEOUT_SECONDS=",
        "PIPELINE_NIGHTLY_TIMEOUT_SECONDS=",
        "PIPELINE_CACHE_TIMEOUT_SECONDS=",
        "BACKUP_RETENTION_DAYS=",
        "HEALTHCHECK_MAX_SYNC_AGE_MINUTES=",
        "HEALTHCHECK_MAX_BACKUP_AGE_HOURS=",
        "HEALTHCHECK_STALE_RUNNING_MINUTES=",
        "RESTORE_DRILL_LOCK_WAIT_SECONDS=",
        "RESTORE_DRILL_TIMEOUT_SECONDS=",
    )
    for token in required_keys:
        if token not in content:
            errors.append(f".env.server.example: отсутствует {token}")

    secret_assignments = (
        r"(?m)^ARVENTO_USER=\S+",
        r"(?m)^ARVENTO_PIN1=\S+",
        r"(?m)^ARVENTO_PIN2=\S+",
    )
    for pattern in secret_assignments:
        if re.search(pattern, content):
            errors.append(".env.server.example содержит заполненные реквизиты Arvento")
            break


def check_superseded_documentation(errors: list[str]) -> None:
    for relative_path in (
        "README.md",
        "SERVER_DEPLOY.md",
        "docs/consolidated-cache.md",
    ):
        content = read_text(relative_path)
        for obsolete in FORBIDDEN_TRACKED_PATHS:
            if obsolete.rsplit("/", 1)[-1] in content:
                errors.append(
                    f"{relative_path}: ссылка на устаревший scheduler {obsolete}"
                )


def check_repository_hygiene(errors: list[str], warnings: list[str]) -> None:
    for path in tracked_files():
        try:
            relative = path.relative_to(ROOT)
        except ValueError:
            continue

        if not path.exists():
            continue

        relative_text = relative.as_posix()
        if relative_text in FORBIDDEN_TRACKED_PATHS:
            errors.append(f"В Git отслеживается устаревший deployment-файл: {relative}")

        if relative.name in FORBIDDEN_TRACKED_NAMES:
            errors.append(f"В Git отслеживается секретный/локальный файл: {relative}")

        if path.suffix.casefold() in FORBIDDEN_TRACKED_SUFFIXES:
            errors.append(f"В Git отслеживается runtime/binary файл: {relative}")

        if any(part in FORBIDDEN_TRACKED_PARTS for part in relative.parts):
            errors.append(f"В Git отслеживается runtime-каталог: {relative}")

        if path.stat().st_size > MAX_TRACKED_FILE_BYTES:
            errors.append(
                f"Слишком большой отслеживаемый файл ({path.stat().st_size} байт): {relative}"
            )

        lowered = relative.name.casefold()
        suspicious_markers = (
            " copy",
            "копия",
            "_final",
            "-final",
            "_backup",
            "-backup",
        )
        if (
            relative_text not in EXPECTED_BACKUP_PATHS
            and any(marker in lowered for marker in suspicious_markers)
        ):
            warnings.append(f"Подозрительное имя файла, проверить вручную: {relative}")

        if path.suffix.casefold() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        if any(marker in text for marker in PRIVATE_KEY_MARKERS):
            errors.append(f"В отслеживаемом файле найден приватный ключ: {relative}")

        if relative_text == ".env.server.example":
            # Placeholder assignments are validated separately.
            continue

        for label, pattern in LITERAL_SECRET_PATTERNS:
            if pattern.search(text):
                errors.append(f"В отслеживаемом файле найден {label}: {relative}")

    existing_legacy = sorted(
        name for name in LEGACY_COMPATIBILITY_FILES if (ROOT / name).is_file()
    )
    if existing_legacy:
        warnings.append(
            "Совместимые legacy-модули оставлены, потому что их импортируют "
            "канонические wrappers: " + ", ".join(existing_legacy)
        )


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []

    check_required_files(errors)
    if not errors:
        check_dockerfile_references(errors)
        check_compose(errors)
        check_systemd(errors)
        check_scripts(errors)
        check_environment_example(errors)
        check_superseded_documentation(errors)
    check_repository_hygiene(errors, warnings)

    for warning in warnings:
        print(f"WARNING: {warning}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1

    print("OK: deployment files and repository hygiene are consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
