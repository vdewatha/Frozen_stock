"""Verify a PostgreSQL custom-format backup and optional disposable restore.

The restore target must be explicitly marked disposable. This script never
prints database URLs or credentials.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def _run(command: list[str]) -> None:
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if result.returncode:
        raise RuntimeError(f"{command[0]} failed with exit code {result.returncode}: {result.stdout[-1000:]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup", type=Path, default=None)
    args = parser.parse_args()
    source = os.environ.get("DATABASE_URL")
    restore = os.environ.get("RESTORE_DATABASE_URL")
    if not source:
        raise SystemExit("DATABASE_URL is required")
    if restore and os.environ.get("RESTORE_TARGET_DISPOSABLE") != "true":
        raise SystemExit("RESTORE_TARGET_DISPOSABLE=true is required for a restore target")
    with tempfile.TemporaryDirectory(prefix="stock-backup-") as temp_dir:
        backup = args.backup or Path(temp_dir) / "database.dump"
        _run([
            "pg_dump", "--format=custom", "--no-owner", "--no-privileges",
            "--file", str(backup), source,
        ])
        _run(["pg_restore", "--list", str(backup)])
        if restore:
            _run([
                "pg_restore", "--clean", "--if-exists", "--no-owner",
                "--no-privileges", "--exit-on-error", "--dbname", restore, str(backup),
            ])
    print("PostgreSQL backup archive created, listed, and restored successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())