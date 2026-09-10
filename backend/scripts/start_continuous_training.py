"""Launch an isolated candidate trainer, without touching the paper executor."""
import argparse
from pathlib import Path
import sqlite3
import subprocess

NAME = "trading-model-training"
IMAGE = "trading-model-training:20260905"


def launch_arguments(database, output):
    return ["docker", "run", "--detach", "--pull", "never", "--name", NAME,
            "--restart", "unless-stopped", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--memory", "1g", "--cpus", "1", "--pids-limit", "128",
            "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
            "--tmpfs", "/tmp", "--env", "ALLOW_LIVE_TRADING=false",
            "--env", "OPENBLAS_NUM_THREADS=1", "--env", "OMP_NUM_THREADS=1",
            "--mount", f"type=bind,src={database},dst=/source/research.sqlite,readonly",
            "--mount", f"type=bind,src={output},dst=/training",
            "--entrypoint", "python", IMAGE, "scripts/run_continuous_training.py",
            "--database", "/source/research.sqlite", "--output", "/training", "--continuous"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.database.is_symlink() or not args.database.is_file() or args.output.is_symlink():
        parser.error("Existing nonsymlink source database and nonsymlink output required")
    database = args.database.resolve(strict=True)
    output = args.output.resolve()
    if output == database.parent or output in database.parents:
        parser.error("Training output must be separate from source state")
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
            parser.error("Single-file read-only mount requires DELETE journal mode; no mode is changed")
    if subprocess.run(["docker", "inspect", NAME], capture_output=True).returncode == 0:
        parser.error("Trainer already exists; inspect it instead of launching a duplicate")
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output.stat().st_mode & 0o077:
        parser.error("Training output must have private mode0700 permissions")
    subprocess.run(launch_arguments(database, output), check=True, capture_output=True)
    print("Continuous candidate trainer started; no credentials, network, or model promotion.")


if __name__ == "__main__":
    main()
