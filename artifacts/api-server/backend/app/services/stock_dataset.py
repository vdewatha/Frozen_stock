"""Verified, reproducible daily-equity training datasets.

This module deliberately reads ``MarketPrice`` rows instead of accepting a
caller supplied DataFrame. A training set is consequently bound to the
provider provenance that was actually ingested, an explicit universe, a
cutoff, and the feature contract. Synthetic, fixture and mixed-provider
histories are not a valid input to this boundary. This is a *captured-current
database extraction*, however, not a point-in-time reconstruction: current
adjustments and asset membership cannot prove what was known historically.

The owning job should create the snapshot and training job in one database
transaction.  ``persist_stock_dataset_snapshot`` flushes, but never commits,
so a caller can atomically record both state transitions.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Type

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Asset, MarketPrice
from app.services.feature_pipeline import feature_config_id as default_feature_config_id


# These are the only provenance values produced by the real daily equity
# importer.  In particular, "database:*", "fixture", "mock" and "unknown"
# are display/source claims, not provider provenance.
VERIFIED_EQUITY_PROVIDERS = frozenset({"yfinance", "yahoo_chart"})
DATASET_FORMAT = "verified-stock-dataset-v1"


class UnverifiedStockDataset(ValueError):
    """Raised when real, single-provider equity provenance cannot be proved."""


@dataclass(frozen=True)
class StockDataset:
    """In-memory view of a durable snapshot.

    ``observations`` is the exact normalized daily OHLCV content whose bytes
    hash to ``dataset_sha256``.  It is returned only to the training worker;
    consumers must use the snapshot identifier and hashes, never reconstruct
    data from whatever happens to be currently in ``market_prices``.
    """

    snapshot_id: str
    dataset_sha256: str
    identity_sha256: str
    observations: pd.DataFrame
    metadata: dict[str, Any]

    def copy_observations(self) -> pd.DataFrame:
        return self.observations.copy(deep=True)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


_PUBLICATION_RETURN_FIELDS = frozenset({"artifact_sha256", "snapshot_manifest_sha256"})
_FIRST_PUBLICATION_FIELDS = frozenset({
    "captured_at", "binding_eligible", "binding_eligibility_reason", "adjustments_point_in_time",
})


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate stock snapshot manifest key")
        result[key] = value
    return result


def _snapshot_manifest_metadata(metadata: dict[str, Any], destination: Path) -> dict[str, Any]:
    """Build the exact on-disk metadata, excluding values derived after write."""
    result = dict(metadata)
    for field in _PUBLICATION_RETURN_FIELDS:
        result.pop(field, None)
    return result | {
        "artifact_path": str(destination.absolute()),
        "artifact_data_path": "dataset.csv",
        "files": {"dataset.csv": result["dataset_sha256"]},
    }


def _validate_first_publication_metadata(metadata: dict[str, Any]) -> None:
    """Validate policy fields retained from the original immutable publish."""
    present = _FIRST_PUBLICATION_FIELDS.intersection(metadata)
    if not present:
        return
    if present != _FIRST_PUBLICATION_FIELDS:
        raise UnverifiedStockDataset("Snapshot first-publication metadata is incomplete")
    captured_at = metadata["captured_at"]
    try:
        captured = datetime.fromisoformat(str(captured_at).replace("Z", "+00:00"))
    except ValueError as error:
        raise UnverifiedStockDataset("Snapshot capture timestamp is invalid") from error
    if captured.tzinfo is None:
        raise UnverifiedStockDataset("Snapshot capture timestamp must include a timezone")
    if type(metadata["binding_eligible"]) is not bool:
        raise UnverifiedStockDataset("Snapshot binding eligibility is invalid")
    if not isinstance(metadata["binding_eligibility_reason"], str) or not metadata["binding_eligibility_reason"].strip():
        raise UnverifiedStockDataset("Snapshot binding eligibility reason is invalid")
    if metadata["adjustments_point_in_time"] is not False:
        raise UnverifiedStockDataset("Stock snapshots cannot claim point-in-time adjustments")
    cutoff = _date(metadata["cutoff_date"])
    expected_eligibility = cutoff >= captured.astimezone(timezone.utc).date()
    if metadata["binding_eligible"] is not expected_eligibility:
        raise UnverifiedStockDataset("Snapshot binding eligibility does not match its original capture date")


def _date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).date()
        else:
            value = value.date()
    elif isinstance(value, str):
        try:
            value = date.fromisoformat(value)
        except ValueError as error:
            raise UnverifiedStockDataset("Cutoff must be an ISO calendar date") from error
    if not isinstance(value, date):
        raise UnverifiedStockDataset("Cutoff must be a calendar date")
    return value


def _universe(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise UnverifiedStockDataset("Universe must be an explicit sequence of symbols")
    try:
        symbols = tuple(str(value).strip().upper() for value in values)
    except TypeError as error:
        raise UnverifiedStockDataset("Universe must be an explicit sequence of symbols") from error
    if not symbols or any(not symbol for symbol in symbols) or len(set(symbols)) != len(symbols):
        raise UnverifiedStockDataset("Universe must be nonempty, normalized, and contain no duplicates")
    # A deterministic input should have one canonical representation.  This
    # avoids a differing request order producing the same observations but a
    # different semantic claim.
    if symbols != tuple(sorted(symbols)):
        raise UnverifiedStockDataset("Universe symbols must be sorted")
    return symbols


def _number(value: Any, name: str, *, positive: bool = False, integral: bool = False) -> float:
    if isinstance(value, bool):
        raise UnverifiedStockDataset(f"{name} is not a market number")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise UnverifiedStockDataset(f"{name} is missing or invalid") from error
    if not math.isfinite(result) or (result <= 0 if positive else result < 0):
        raise UnverifiedStockDataset(f"{name} is outside its valid range")
    if integral and result != math.floor(result):
        raise UnverifiedStockDataset(f"{name} must be integral")
    return result


def _normalized_rows(rows: list[MarketPrice], *, symbols: tuple[str, ...], cutoff: date, provider: str) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, date]] = set()
    for row in rows:
        symbol, day = str(row.symbol).strip().upper(), row.price_date
        if symbol not in symbols or day > cutoff:
            raise UnverifiedStockDataset("Database query returned a row outside the requested snapshot")
        if not isinstance(day, date) or (symbol, day) in seen:
            raise UnverifiedStockDataset("Duplicate or invalid daily equity observation")
        seen.add((symbol, day))
        source = str(row.source or "").strip()
        if source != provider or source not in VERIFIED_EQUITY_PROVIDERS:
            raise UnverifiedStockDataset("Snapshot contains mock, unknown, mixed, or nonverified provider provenance")
        if row.imported_at is None:
            raise UnverifiedStockDataset("Snapshot contains an observation without ingestion provenance")
        imported_at = pd.Timestamp(row.imported_at)
        if imported_at.tzinfo is None:
            imported_at = imported_at.tz_localize("UTC")
        else:
            imported_at = imported_at.tz_convert("UTC")
        open_ = _number(row.open, "open", positive=True)
        high = _number(row.high, "high", positive=True)
        low = _number(row.low, "low", positive=True)
        close = _number(row.close, "close", positive=True)
        adjusted_close = _number(row.adjusted_close, "adjusted_close", positive=True)
        volume = _number(row.volume, "volume", integral=True)
        if high < max(open_, close, low) or low > min(open_, close, high):
            raise UnverifiedStockDataset("Snapshot contains invalid OHLC bounds")
        adjustment_factor = adjusted_close / close
        if not math.isfinite(adjustment_factor) or adjustment_factor <= 0:
            raise UnverifiedStockDataset("Snapshot contains invalid corporate-action adjustment")
        # Retain original close as provenance and use the consistently
        # adjusted OHLC series for all causal features and labels.
        records.append(
            {
                "symbol": symbol,
                "date": day.isoformat(),
                "open": open_ * adjustment_factor,
                "high": high * adjustment_factor,
                "low": low * adjustment_factor,
                "close": adjusted_close,
                "volume": int(volume),
                "raw_close": close,
                "adjustment_factor": adjustment_factor,
                "provider": provider,
                "imported_at": imported_at.isoformat(),
            }
        )
    if not records:
        raise UnverifiedStockDataset("No verified ingested equity observations exist at the requested cutoff")
    frame = pd.DataFrame(records).sort_values(["symbol", "date"], kind="mergesort").reset_index(drop=True)
    for symbol, group in frame.groupby("symbol", sort=False):
        timestamps = pd.to_datetime(group["date"], utc=True, errors="raise")
        if timestamps.duplicated().any() or not timestamps.is_monotonic_increasing:
            raise UnverifiedStockDataset(f"Invalid daily chronology for {symbol}")
    missing = set(symbols) - set(frame.symbol)
    if missing:
        raise UnverifiedStockDataset(f"Verified observations missing for requested symbols: {sorted(missing)}")
    return frame


def _dataset_bytes(frame: pd.DataFrame) -> bytes:
    """Stable serialization independent of DB decimal/dialect representation."""
    columns = [
        "symbol", "date", "open", "high", "low", "close", "volume",
        "raw_close", "adjustment_factor", "provider", "imported_at",
    ]
    lines = [",".join(columns)]
    for row in frame[columns].itertuples(index=False):
        lines.append(
            ",".join(
                (
                    str(row.symbol), str(row.date),
                    *(format(float(value), ".17g") for value in row[2:6]),
                    str(int(row.volume)),
                    format(float(row.raw_close), ".17g"),
                    format(float(row.adjustment_factor), ".17g"),
                    str(row.provider), str(row.imported_at),
                )
            )
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _snapshot_identity(*, cutoff: date, symbols: tuple[str, ...], provider: str, horizon_days: int,
                       feature_config_id: str, dataset_sha256: str, capture_vintage_sha256: str) -> dict[str, Any]:
    return {
        "format": DATASET_FORMAT,
        "cutoff_date": cutoff.isoformat(),
        "universe": list(symbols),
        "universe_sha256": hashlib.sha256(_canonical(list(symbols))).hexdigest(),
        "provider": provider,
        "adjustment_policy": "daily_adjusted_close_factor_v1",
        "horizon_days": horizon_days,
        "feature_config_id": feature_config_id,
        "dataset_sha256": dataset_sha256,
        # Derived from immutable row-level imported_at values, not wall-clock
        # extraction time, so an otherwise identical retry has the same ID.
        "capture_vintage_sha256": capture_vintage_sha256,
    }


def create_stock_dataset(
    db: Session,
    *,
    cutoff: date | datetime | str,
    universe: Iterable[str],
    horizon_days: int,
    provider: str,
    feature_config: str | None = None,
) -> StockDataset:
    """Create a verified snapshot from persisted daily equity observations.

    The provider is mandatory rather than inferred: a request must state the
    provenance contract it expects.  This prevents newly imported provider
    rows from silently changing an experiment's meaning.
    """
    cutoff_day = _date(cutoff)
    symbols = _universe(universe)
    if type(horizon_days) is not int or not 1 <= horizon_days <= 252:
        raise UnverifiedStockDataset("horizon_days must be an integer in 1..252")
    provider = str(provider or "").strip()
    if provider not in VERIFIED_EQUITY_PROVIDERS:
        raise UnverifiedStockDataset("A supported verified equity provider is required")
    feature_config = feature_config or default_feature_config_id()
    if not isinstance(feature_config, str) or len(feature_config) != 64:
        raise UnverifiedStockDataset("A concrete feature configuration hash is required")

    active = {
        symbol
        for (symbol,) in db.execute(
            select(Asset.symbol).where(Asset.symbol.in_(symbols), Asset.is_active.is_(True), Asset.asset_type == "stock")
        )
    }
    if active != set(symbols):
        raise UnverifiedStockDataset("Every requested universe member must be an active stock asset")
    rows = list(
        db.execute(
            select(MarketPrice)
            .where(
                MarketPrice.symbol.in_(symbols),
                MarketPrice.price_date <= cutoff_day,
            )
            .order_by(MarketPrice.symbol.asc(), MarketPrice.price_date.asc())
        ).scalars()
    )
    frame = _normalized_rows(rows, symbols=symbols, cutoff=cutoff_day, provider=provider)
    raw = _dataset_bytes(frame)
    dataset_sha256 = hashlib.sha256(raw).hexdigest()
    capture_vintage = {
        "kind": "persisted_market_price_import_vintage_v1",
        "latest_imported_at": max(frame["imported_at"]),
        "row_imported_at_sha256": hashlib.sha256(
            _canonical(frame[["symbol", "date", "imported_at"]].to_dict(orient="records"))
        ).hexdigest(),
    }
    capture_vintage_sha256 = hashlib.sha256(_canonical(capture_vintage)).hexdigest()
    identity = _snapshot_identity(
        cutoff=cutoff_day, symbols=symbols, provider=provider, horizon_days=horizon_days,
        feature_config_id=feature_config, dataset_sha256=dataset_sha256,
        capture_vintage_sha256=capture_vintage_sha256,
    )
    identity_sha256 = hashlib.sha256(_canonical(identity)).hexdigest()
    metadata = identity | {
        "snapshot_id": identity_sha256,
        "identity_sha256": identity_sha256,
        "row_count": int(len(frame)),
        "symbol_row_counts": {symbol: int((frame.symbol == symbol).sum()) for symbol in symbols},
        "dataset_encoding": "verified-stock-dataset-csv-v1",
        "data_file_sha256": dataset_sha256,
        "capture_vintage": capture_vintage,
        "capture_vintage_sha256": capture_vintage_sha256,
        "temporal_semantics": {
            "snapshot_type": "captured_at_extraction",
            "extraction": "captured_current_database_rows",
            "historical_cutoff": "limits observation dates only; it is not a point-in-time knowledge cutoff",
            "historical_cutoff_is_point_in_time": False,
            "point_in_time_verified": False,
            "current_adjustments_and_universe_may_reflect_later_information": True,
        },
    }
    return StockDataset(identity_sha256, dataset_sha256, identity_sha256, frame, metadata)


def publish_stock_dataset(dataset: StockDataset, output: Path) -> StockDataset:
    """Atomically publish the immutable snapshot file referenced by its DB row.

    Snapshot content is intentionally a file, never a database blob. The row
    stores this path and its hashes; a missing artifact must fail visibly at
    training time rather than being rebuilt from potentially changed prices.
    """
    if not isinstance(dataset, StockDataset):
        raise UnverifiedStockDataset("Only a verified StockDataset can be published")
    destination = Path(output) / dataset.snapshot_id
    if destination.exists():
        return _adopt_published_stock_dataset(dataset, destination)
    Path(output).mkdir(parents=True, exist_ok=True)
    content = _dataset_bytes(dataset.observations)
    if hashlib.sha256(content).hexdigest() != dataset.dataset_sha256:
        raise UnverifiedStockDataset("Dataset changed before snapshot publication")
    with tempfile.TemporaryDirectory(prefix=".stock-dataset-", dir=output) as temporary:
        stage = Path(temporary)
        (stage / "dataset.csv").write_bytes(content)
        snapshot_manifest = _snapshot_manifest_metadata(dataset.metadata, destination)
        _validate_first_publication_metadata(snapshot_manifest)
        manifest = _canonical(snapshot_manifest)
        (stage / "snapshot.json").write_bytes(manifest)
        try:
            os.rename(stage, destination)
        except FileExistsError:
            # Another worker may have published the content-addressed snapshot
            # while this transaction rolled back. Adopt it only after the same
            # complete integrity checks used by a new worker.
            return _adopt_published_stock_dataset(dataset, destination)
    metadata = dataset.metadata | {
        "artifact_path": str(destination.absolute()),
        "artifact_data_path": "dataset.csv",
        "artifact_sha256": dataset.dataset_sha256,
        "snapshot_manifest_sha256": hashlib.sha256(manifest).hexdigest(),
    }
    return replace(dataset, metadata=metadata)


def _adopt_published_stock_dataset(dataset: StockDataset, destination: Path) -> StockDataset:
    """Validate and reuse a complete snapshot orphaned by a DB rollback."""
    if destination.is_symlink() or not destination.is_dir():
        raise UnverifiedStockDataset("Existing stock dataset artifact is not a regular immutable directory")
    expected_files = {"dataset.csv", "snapshot.json"}
    if {path.name for path in destination.iterdir()} != expected_files:
        raise UnverifiedStockDataset("Existing stock dataset artifact is incomplete or has unexpected files")
    content = _dataset_bytes(dataset.observations)
    expected_manifest = _snapshot_manifest_metadata(dataset.metadata, destination)
    _validate_first_publication_metadata(expected_manifest)
    data_path = destination / "dataset.csv"
    manifest_path = destination / "snapshot.json"
    if (
        data_path.is_symlink() or manifest_path.is_symlink() or not data_path.is_file()
        or not manifest_path.is_file() or data_path.read_bytes() != content
    ):
        raise UnverifiedStockDataset("Existing stock dataset artifact does not match the requested snapshot")
    raw_manifest = manifest_path.read_bytes()
    try:
        stored_manifest = json.loads(
            raw_manifest,
            object_pairs_hook=_unique_json_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite manifest number")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise UnverifiedStockDataset("Existing stock dataset manifest is invalid") from error
    if not isinstance(stored_manifest, dict) or set(stored_manifest) != set(expected_manifest):
        raise UnverifiedStockDataset("Existing stock dataset manifest has an invalid field contract")
    # A new transaction has a new wall-clock capture timestamp. Those fields
    # are deliberately inherited from the first immutable publication; every
    # identity/provenance field remains an exact request match.
    for key, value in expected_manifest.items():
        if key not in _FIRST_PUBLICATION_FIELDS and stored_manifest.get(key) != value:
            raise UnverifiedStockDataset("Existing stock dataset manifest does not match the requested snapshot")
    _validate_first_publication_metadata(stored_manifest)
    metadata = {
        key: stored_manifest[key]
        for key in dataset.metadata
        if key in stored_manifest
    } | {
        "artifact_path": str(destination.absolute()),
        "artifact_data_path": "dataset.csv",
        "artifact_sha256": dataset.dataset_sha256,
        "snapshot_manifest_sha256": hashlib.sha256(raw_manifest).hexdigest(),
    }
    # Exercise the regular verifier as the final adoption gate rather than
    # treating byte equality above as a substitute for contract validation.
    adopted = replace(dataset, metadata=metadata)
    verify_stock_dataset_artifact(adopted)
    return adopted


def verify_stock_dataset_artifact(dataset: StockDataset) -> Path:
    """Return the immutable CSV path or raise a conspicuous integrity error."""
    if not isinstance(dataset, StockDataset):
        raise UnverifiedStockDataset("Only a verified StockDataset can be verified")
    root = dataset.metadata.get("artifact_path")
    relative = dataset.metadata.get("artifact_data_path")
    expected = dataset.metadata.get("artifact_sha256")
    if not isinstance(root, str) or not isinstance(relative, str) or not isinstance(expected, str):
        raise FileNotFoundError("Verified stock dataset artifact reference is absent from snapshot metadata")
    if dataset.metadata.get("provider") not in VERIFIED_EQUITY_PROVIDERS:
        raise UnverifiedStockDataset("Verified stock snapshot has nonverified provider provenance")
    root_path = Path(root)
    path = root_path / relative
    manifest_path = root_path / "snapshot.json"
    if (
        relative != "dataset.csv" or root_path.is_symlink() or not root_path.is_dir()
        or path.is_symlink() or manifest_path.is_symlink() or not path.is_file()
    ):
        raise FileNotFoundError(f"Verified stock dataset artifact is missing: {path}")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected or actual != dataset.dataset_sha256:
        raise UnverifiedStockDataset("Verified stock dataset artifact hash mismatch")
    manifest_digest = dataset.metadata.get("snapshot_manifest_sha256")
    if not isinstance(manifest_digest, str) or not manifest_path.is_file():
        raise FileNotFoundError(f"Verified stock snapshot manifest is missing: {manifest_path}")
    raw_manifest = manifest_path.read_bytes()
    if hashlib.sha256(raw_manifest).hexdigest() != manifest_digest:
        raise UnverifiedStockDataset("Verified stock snapshot manifest hash mismatch")
    try:
        manifest = json.loads(
            raw_manifest,
            object_pairs_hook=_unique_json_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite manifest number")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise UnverifiedStockDataset("Verified stock snapshot manifest is invalid") from error
    expected_manifest = _snapshot_manifest_metadata(dataset.metadata, root_path)
    if not isinstance(manifest, dict) or manifest != expected_manifest:
        raise UnverifiedStockDataset("Verified stock snapshot manifest metadata mismatch")
    _validate_first_publication_metadata(manifest)
    return path


def persist_stock_dataset_snapshot(
    db: Session,
    dataset: StockDataset,
    *,
    snapshot_model: Type[Any],
) -> Any:
    """Insert (or verify) the durable snapshot row without committing.

    The jobs package owns the concrete ORM table.  Passing its model explicitly
    avoids a service-level import cycle while making the durable interface
    strict: required identity fields must all be represented by that model.
    """
    if not isinstance(dataset, StockDataset):
        raise UnverifiedStockDataset("Only a verified StockDataset can be persisted")
    verify_stock_dataset_artifact(dataset)
    columns = set(snapshot_model.__table__.columns.keys())
    aliases: dict[str, tuple[str, ...]] = {
        "snapshot_id": ("snapshot_id", "dataset_id", "id"),
        "dataset_sha256": ("dataset_sha256", "data_sha256"),
        "cutoff_date": ("cutoff_date", "cutoff", "cutoff_at"),
        "universe": ("universe", "universe_symbols"),
        "provider": ("provider", "provider_provenance"),
        "feature_config_id": ("feature_config_id",),
        "artifact_path": ("artifact_path", "snapshot_path"),
    }
    optional_aliases: dict[str, tuple[str, ...]] = {
        "horizon_days": ("horizon_days",),
        "artifact_sha256": ("artifact_sha256", "artifact_data_sha256"),
        "row_count": ("row_count",),
        "metadata": ("metadata_json", "snapshot_metadata", "details"),
    }
    values: dict[str, Any] = {}
    for logical, choices in aliases.items():
        field = next((choice for choice in choices if choice in columns), None)
        if field is None:
            raise UnverifiedStockDataset(f"Snapshot model lacks durable {logical} field")
        value = dataset.metadata.get(logical)
        if logical == "universe":
            value = dataset.metadata["universe"]
            if field == "universe" and "provider_provenance" in columns:
                # StockDatasetSnapshot's documented JSON contract.
                value = {"symbols": value}
        elif logical == "provider" and field == "provider_provenance":
            value = {
                "providers_by_symbol": {symbol: dataset.metadata["provider"] for symbol in dataset.metadata["universe"]},
                "verified_by": "stock_dataset.create_stock_dataset",
            }
        elif logical == "cutoff_date":
            cutoff = _date(value)
            value = (
                datetime.combine(cutoff, datetime.min.time(), tzinfo=timezone.utc)
                if field == "cutoff_at" else cutoff
            )
        values[field] = value
    for logical, choices in optional_aliases.items():
        field = next((choice for choice in choices if choice in columns), None)
        if field is not None:
            values[field] = dataset.metadata if logical == "metadata" else dataset.metadata.get(logical)
    existing = db.query(snapshot_model).filter_by(**{next(name for name in aliases["snapshot_id"] if name in columns): dataset.snapshot_id}).one_or_none()
    if existing is not None:
        existing_hash = getattr(existing, next(name for name in aliases["dataset_sha256"] if name in columns))
        if existing_hash != dataset.dataset_sha256:
            raise UnverifiedStockDataset("A durable snapshot identifier cannot be overwritten")
        # Idempotency is safe only when every persisted value is identical.
        # Otherwise a hash collision/incorrect row must be conspicuous, not
        # silently reused by a later job.
        if any(getattr(existing, field) != value for field, value in values.items()):
            raise UnverifiedStockDataset("A durable snapshot row does not match its immutable metadata")
        return existing
    row = snapshot_model(**values)
    db.add(row)
    db.flush()
    return row