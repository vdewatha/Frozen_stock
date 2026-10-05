"""Back up the local paper stack and verify an isolated, networkless restore.

Run with Python 3.11+. No broker calls, source writes or recovery-gate overrides.
Archives contain private account data: keep the output directory private.
"""
import hashlib
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
CONTEXT = os.environ.get("PAPER_DOCKER_CONTEXT", "colima-frozen-stock" if sys.platform == "darwin" else "")
DOCKER = ["docker"] + (["--context", CONTEXT] if CONTEXT else [])
COMPOSE = DOCKER + ["compose", "--env-file", str(ROOT / "deploy/paper/paper.env"),
                    "-f", str(ROOT / "deploy/paper/compose.yaml")]

# The exported PostgreSQL snapshot is held until pg_dump finishes. Both table
# hashes and pg_dump therefore see the same database, even with active workers.
SOURCE = r'''
import hashlib, json, sys
import psycopg
from psycopg import sql
from app.core.config import settings
with psycopg.connect(settings.database_url.replace("postgresql+psycopg://", "postgresql://")) as conn:
    conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
    snapshot = conn.execute("SELECT pg_export_snapshot()").fetchone()[0]
    print(json.dumps({"snapshot": snapshot}), flush=True)
    result = {}
    names = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename").fetchall()
    for (name,) in names:
        digest, count = hashlib.sha256(), 0
        query = sql.SQL('SELECT row_to_json(t)::text FROM public.{} t ORDER BY row_to_json(t)::text COLLATE "C"').format(sql.Identifier(name))
        with conn.cursor(name="fingerprint") as cursor:
            cursor.execute(query)
            for (row,) in cursor:
                digest.update((row + "\n").encode())
                count += 1
        result[name] = {"rows": count, "sha256": digest.hexdigest()}
    print(json.dumps(result), flush=True)
    sys.stdin.readline()
'''

ARTIFACTS = r'''
import hashlib, json, pathlib, sys, tarfile
root = pathlib.Path("/data/models")
paths = sorted(root.rglob("*"))
if any(p.is_symlink() for p in paths):
    raise RuntimeError("Symlink in model artifacts")
files = [p for p in paths if p.is_file()]
if sys.argv[1] == "manifest":
    print(json.dumps({str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}))
else:
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
        for p in files:
            archive.add(p, arcname=str(p.relative_to(root)), recursive=False)
'''


def run(command, **kwargs):
    result = subprocess.run(command, stderr=subprocess.PIPE, timeout=180,
                            stdout=kwargs.pop("stdout", subprocess.PIPE), **kwargs)
    if result.returncode:
        # Database errors can include row contents. Keep them out of terminal logs.
        raise RuntimeError("Restore verification subprocess failed; no verification granted")
    return result.stdout


def line(process, timeout=180):
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        if not selector.select(timeout):
            raise RuntimeError("Timed out waiting for source snapshot")
    raw = process.stdout.readline()
    if not raw:
        raise RuntimeError("Source snapshot process failed")
    return json.loads(raw)


def restore_artifacts(archive_path, destination):
    hashes = {}
    with tarfile.open(archive_path) as archive:
        for member in archive:
            path = Path(member.name)
            if not member.isfile() or path.is_absolute() or ".." in path.parts or member.name in hashes:
                raise ValueError("Unsafe or duplicate artifact member")
            target = destination / path
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with archive.extractfile(member) as source, target.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
            hashes[member.name] = digest.hexdigest()
    return hashes


def main(output_root=None):
    os.umask(0o077)
    ident = uuid.uuid4().hex[:12]
    output = (Path(output_root) if output_root is not None else ROOT / ".paper-backups") / ident
    output.mkdir(parents=True, mode=0o700)
    name = "paper-restore-drill-" + ident
    source = None
    created = False
    start = time.monotonic()
    try:
        source = subprocess.Popen(COMPOSE + ["exec", "-T", "backend", "python", "-u", "-c", SOURCE],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        snapshot = line(source)["snapshot"]
        expected = line(source)
        with (output / "database.dump").open("xb") as archive:
            run(COMPOSE + ["exec", "-T", "postgres", "pg_dump", "-U", "paper", "-d", "paper",
                           "-Fc", "--snapshot", snapshot], stdout=archive)
        source.stdin.write(b"done\n")
        source.stdin.flush()
        if source.wait(timeout=30):
            raise RuntimeError("Snapshot holder failed")
        before = json.loads(run(COMPOSE + ["exec", "-T", "backend", "python", "-c", ARTIFACTS, "manifest"]))
        with (output / "models.tar").open("xb") as archive:
            run(COMPOSE + ["exec", "-T", "backend", "python", "-c", ARTIFACTS, "archive"], stdout=archive)
        after = json.loads(run(COMPOSE + ["exec", "-T", "backend", "python", "-c", ARTIFACTS, "manifest"]))
        if before != after:
            raise RuntimeError("Model artifacts changed during backup; retry required")
        run(DOCKER + ["run", "-d", "--name", name, "--network", "none",
                      "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "POSTGRES_DB=restore_check",
                      "postgres:16-alpine"])
        created = True
        for _ in range(30):
            ready = subprocess.run(DOCKER + ["exec", name, "pg_isready", "-U", "postgres"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("Isolated restore database did not become ready")
        with (output / "database.dump").open("rb") as archive:
            run(DOCKER + ["exec", "-i", name, "pg_restore", "-U", "postgres", "-d", "restore_check",
                          "--exit-on-error", "--single-transaction", "--no-owner", "--no-acl"], stdin=archive)
        actual = {}
        psql = DOCKER + ["exec", name, "psql", "-XAt", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "restore_check", "-c"]
        for table in expected:
            quoted = '"' + table.replace('"', '""') + '"'
            data = run(psql + [f'SELECT row_to_json(t)::text FROM public.{quoted} t ORDER BY row_to_json(t)::text COLLATE "C"'])
            actual[table] = {"rows": len(data.splitlines()), "sha256": hashlib.sha256(data).hexdigest()}
        if actual != expected:
            raise RuntimeError("Restored table contents differ from source snapshot")
        with tempfile.TemporaryDirectory(prefix="paper-model-restore-") as directory:
            if restore_artifacts(output / "models.tar", Path(directory)) != before:
                raise RuntimeError("Restored artifact hashes differ")
        report = {"status": "verified", "tables": len(actual),
                  "rows": sum(item["rows"] for item in actual.values()), "model_files": len(before),
                  "seconds": round(time.monotonic()-start, 2), "broker_calls": 0,
                  "recovery_gate_changed": False, "scope": "database contents and model files only",
                  "limitations": ["Not an off-host encrypted backup", "No roles or ACL restoration",
                                  "No automatic broker recovery or queue replay certification"],
                  "table_manifest": actual, "model_manifest": before}
        (output / "verification.json").write_text(json.dumps(report, indent=2))
        print(json.dumps({k: v for k, v in report.items() if not k.endswith("manifest")}))
        print("Private backup: " + str(output))
        return output
    finally:
        if source and source.poll() is None:
            source.stdin.close()
            try:
                source.wait(timeout=15)
            except subprocess.TimeoutExpired:
                source.kill()
                source.wait()
        if created:
            run(DOCKER + ["rm", "-fv", name])


if __name__ == "__main__":
    main()
