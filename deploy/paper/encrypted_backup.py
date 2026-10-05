"""Private local Restic backups; never grants broker recovery authorization."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile

import verify_restore

IMAGE = "restic/restic:0.19.1@sha256:136600b6ff6843d61d355f7f71f460a166429f35de6fd11b568fece3c9a4d510"
FILES = ("database.dump", "models.tar", "verification.json")


def command(vault, args, source=None, destination=None, password=None):
    mounts = [(vault / "repository", "/repo", False),
              (password or vault / "password", "/password", True)]
    if source is not None:
        mounts.append((source, "/source", True))
    if destination is not None:
        mounts.append((destination, "/restore", False))
    result = verify_restore.DOCKER + [
        "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--hostname", "frozen-stock-backup",
        "--user", f"{os.getuid()}:{os.getgid()}", "--tmpfs", "/tmp:rw,noexec,nosuid,size=128m"]
    for path, target, readonly in mounts:
        result += ["--mount", f"type=bind,source={path.resolve()},target={target}" + (",readonly" if readonly else "")]
    return result + [IMAGE, "--no-cache", "--repo", "/repo", "--password-file", "/password"] + args


def restic(vault, args, **kwargs):
    result = subprocess.run(command(vault, args, **kwargs), capture_output=True, timeout=900)
    if result.returncode:
        raise RuntimeError(f"Restic {args[0]} failed (exit {result.returncode}); no verification granted")
    return result.stdout


def initialize(vault):
    # Exclusive directory creation prevents accidentally replacing a recovery key.
    vault.mkdir(mode=0o700, parents=False, exist_ok=False)
    (vault / "repository").mkdir(mode=0o700)
    with (vault / "password").open("x") as output:
        os.chmod(vault / "password", 0o600)
        output.write(secrets.token_urlsafe(48) + "\n")
    restic(vault, ["init"])


def fingerprints(directory):
    result = {}
    for name in FILES:
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Backup member must be a regular file")
        with path.open("rb") as stream:
            result[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def encrypt_and_verify(vault, source):
    expected = fingerprints(source)
    events = [json.loads(line) for line in restic(
        vault, ["backup", "--json"] + ["/source/" + name for name in FILES], source=source).splitlines()]
    summaries = [event for event in events if event.get("message_type") == "summary"]
    if len(summaries) != 1 or not summaries[0].get("snapshot_id"):
        raise RuntimeError("Missing unique backup snapshot")
    snapshot = summaries[0]["snapshot_id"]
    restic(vault, ["check", "--read-data"])
    with tempfile.TemporaryDirectory(prefix="decrypt-", dir=vault) as directory:
        restic(vault, ["restore", snapshot, "--target", "/restore", "--verify"], destination=Path(directory))
        if fingerprints(Path(directory) / "source") != expected:
            raise RuntimeError("Decrypted backup differs from verified source")
    return {"status": "verified", "snapshot": snapshot, "file_hashes": expected,
            "encrypted": True, "off_host": False, "broker_calls": 0,
            "recovery_gate_changed": False}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "backup"))
    parser.add_argument("--vault", type=Path, default=verify_restore.ROOT / ".paper-vault")
    args = parser.parse_args()
    vault = args.vault.resolve()
    if args.action == "init":
        initialize(vault)
        print("Encrypted repository initialized. Keep its password in separate secure storage.")
        return
    if not (vault / "repository/config").is_file() or not (vault / "password").is_file():
        raise RuntimeError("Initialize the repository explicitly before backup")
    if (vault / "password").stat().st_mode & 0o077:
        raise RuntimeError("Repository password must be private (0600)")
    restic(vault, ["snapshots", "--json"])
    with tempfile.TemporaryDirectory(prefix="staging-", dir=vault) as directory:
        source = verify_restore.main(Path(directory))
        report = encrypt_and_verify(vault, source)
    with (vault / "last-verification.json").open("w") as output:
        json.dump(report, output, indent=2)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
