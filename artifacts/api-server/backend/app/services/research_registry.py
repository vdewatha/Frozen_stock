"""Integrity-checked registration, without deserializing executable model files.

Hashes establish consistency, not authenticity of caller-supplied research. This
service never certifies data provenance, promotes a model or authorizes trading.
The caller owns the transaction and must commit on success.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.models import ResearchModelRun

IDENTITY_KEYS = ("format_version", "symbol", "source_claim", "horizon_bars", "seed",
                 "dataset_sha256", "feature_config_id", "code_sha256", "versions")
FILES = {"dataset.csv", "holdout_predictions.csv", "logistic_regression.joblib", "random_forest.joblib"}
HEX = re.compile(r"[0-9a-f]{64}\Z")


def _canonical(value):
    return json.dumps(value, sort_keys=True, indent=2, allow_nan=False).encode()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate manifest key")
        result[key] = value
    return result


def _read(directory_fd, name, *, maximum=1024 * 1024 * 1024):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError("Artifact must be a bounded regular file")
        content = stream.read(maximum + 1)
        if len(content) > maximum:
            raise ValueError("Artifact exceeds size limit")
        return content


def validate_research_run(path: Path) -> tuple[dict, str, str]:
    directory = Path(path).absolute()
    if ".." in directory.parts or any(p.is_symlink() for p in (directory, *directory.parents)):
        raise ValueError("Symlinks and parent traversal are not accepted")
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            manifest = json.loads(_read(fd, "manifest.json", maximum=1024 * 1024), object_pairs_hook=_unique_object,
                                  parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
            if not isinstance(manifest, dict):
                raise ValueError("Manifest must be an object")
            identity = {key: manifest[key] for key in IDENTITY_KEYS}
            if type(identity["format_version"]) is not int or identity["format_version"] not in (1, 2):
                raise ValueError("Unsupported format version")
            portable = False
            if identity["format_version"] == 2:
                identity.update({key: manifest[key] for key in ("instrument_id", "timeframe_minutes")})
                if identity["instrument_id"] is not None or identity["timeframe_minutes"] is not None:
                    if identity["instrument_id"] != "crypto_spot:KRAKEN:BTC:USD" or type(identity["timeframe_minutes"]) is not int or identity["timeframe_minutes"] != 60:
                        raise ValueError("Invalid portable instrument binding")
                    portable = True
            for key in ("symbol", "source_claim"):
                if not isinstance(identity[key], str) or not identity[key].strip():
                    raise ValueError("Missing identity text")
            if type(identity["horizon_bars"]) is not int or identity["horizon_bars"] not in (1, 5, 20) or type(identity["seed"]) is not int:
                raise ValueError("Invalid training parameters")
            for key in ("dataset_sha256", "feature_config_id", "code_sha256"):
                if not isinstance(identity[key], str) or not HEX.fullmatch(identity[key]):
                    raise ValueError("Invalid identity checksum")
            versions = identity["versions"]
            if not isinstance(versions, dict) or any(not isinstance(versions.get(k), str) or not versions[k] for k in ("python", "numpy", "pandas", "sklearn", "joblib")):
                raise ValueError("Missing environment versions")
            run_id = hashlib.sha256(_canonical(identity)).hexdigest()
            if manifest["run_id"] != run_id or directory.name != run_id:
                raise ValueError("Inconsistent run identity")
            if manifest["status"] != "experimental" or manifest["eligible_for_trading"] is not False:
                raise ValueError("Only experimental ineligible runs may be registered")
            for key, minimum in (("train_rows", 100), ("holdout_rows", 30)):
                if type(manifest[key]) is not int or manifest[key] < minimum:
                    raise ValueError("Insufficient training metadata")
            dates = [datetime.fromisoformat(manifest[key]) for key in ("train_end", "train_label_end", "holdout_start", "holdout_end")]
            if any(d.tzinfo is None for d in dates) or not dates[0] <= dates[1] < dates[2] <= dates[3]:
                raise ValueError("Invalid purged training chronology")
            if not isinstance(manifest["limitations"], list) or not manifest["limitations"] or any(not isinstance(v, str) for v in manifest["limitations"]):
                raise ValueError("Missing research limitations")
            for model in ("logistic_regression", "random_forest", "training_prevalence_baseline"):
                metrics = manifest["metrics"][model]
                for key in ("brier_score", "log_loss"):
                    value = metrics[key]
                    if type(value) not in (float, int) or not math.isfinite(value) or value < 0 or (key == "brier_score" and value > 1):
                        raise ValueError("Invalid model metrics")
            files = manifest["files"]
            expected_files = FILES | ({"logistic_regression.json"} if portable else set())
            if not isinstance(files, dict) or set(files) != expected_files:
                raise ValueError("Unexpected artifact names")
            if set(os.listdir(fd)) != expected_files | {"manifest.json"}:
                raise ValueError("Unexpected directory contents")
            for name, expected in files.items():
                if not isinstance(expected, str) or not HEX.fullmatch(expected) or hashlib.sha256(_read(fd, name)).hexdigest() != expected:
                    raise ValueError("Artifact checksum mismatch")
            if files["dataset.csv"] != identity["dataset_sha256"]:
                raise ValueError("Dataset identity mismatch")
            digest = hashlib.sha256(_canonical(manifest)).hexdigest()
            return manifest, digest, str(directory)
        finally:
            os.close(fd)
    except (OSError, KeyError, TypeError, json.JSONDecodeError, OverflowError) as error:
        raise ValueError("Invalid research artifact bundle") from error


def register_research_run(db: Session, path: Path) -> ResearchModelRun:
    manifest, digest, directory = validate_research_run(path)
    run_id = manifest["run_id"]

    def existing():
        row = db.scalar(select(ResearchModelRun).where(ResearchModelRun.run_id == run_id))
        if row is not None and (row.manifest_sha256 != digest or row.training_metadata != manifest or row.status != "experimental" or row.eligible_for_trading):
            raise ValueError("Registered run identity cannot be overwritten")
        return row

    if row := existing():
        return row
    row = ResearchModelRun(run_id=run_id, manifest_sha256=digest, artifact_path=directory,
                           status="experimental", eligible_for_trading=False, training_metadata=manifest)
    connection = db.connection()
    # sqlite3 legacy transaction mode does not BEGIN for SELECT or SAVEPOINT.
    # Without this, releasing the first savepoint commits before caller commit.
    if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN")
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        if row := existing():
            return row
        raise
    return row
