"""Unit coverage for the verified daily-stock snapshot/training boundary."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models import Asset, MarketPrice, StockDatasetSnapshot
from app.services.feature_pipeline import feature_config_id
from app.services.stock_dataset import (
    StockDataset,
    UnverifiedStockDataset,
    _dataset_bytes,
    create_stock_dataset,
    persist_stock_dataset_snapshot,
    publish_stock_dataset,
    verify_stock_dataset_artifact,
)
from app.services.stock_training import (
    TrainingCancelled,
    prepare_stock_frame,
    purged_expanding_walkforward,
    train_stock_model,
)
import app.services.stock_training as stock_training


def _dataset(periods: int = 680) -> StockDataset:
    days = pd.bdate_range("2021-01-01", periods=periods, tz="UTC")
    rows = []
    for symbol, phase in (("AAA", 0.0), ("BBB", 0.8)):
        signal = np.arange(periods) / 5 + phase
        close = 100 + np.arange(periods) * 0.04 + np.sin(signal) * 4
        for day, value in zip(days, close):
            rows.append(
                {
                    "symbol": symbol,
                    "date": day.date().isoformat(),
                    "open": value * 0.998,
                    "high": value * 1.02,
                    "low": value * 0.98,
                    "close": value,
                    "volume": 1_000_000,
                    "raw_close": value,
                    "adjustment_factor": 1.0,
                    "provider": "yfinance",
                    "imported_at": "2024-01-01T00:00:00+00:00",
                }
            )
    observations = pd.DataFrame(rows).sort_values(["symbol", "date"]).reset_index(drop=True)
    digest = hashlib.sha256(_dataset_bytes(observations)).hexdigest()
    universe = ["AAA", "BBB"]
    metadata = {
        "snapshot_id": "a" * 64,
        "identity_sha256": "a" * 64,
        "dataset_sha256": digest,
        "provider": "yfinance",
        "universe": universe,
        "universe_sha256": hashlib.sha256(
            json.dumps(universe, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "cutoff_date": observations.date.max(),
        "feature_config_id": feature_config_id(),
        "horizon_days": 5,
    }
    return StockDataset("a" * 64, digest, "a" * 64, observations, metadata)


def test_unpublished_or_missing_dataset_artifact_fails_closed():
    dataset = _dataset()
    with pytest.raises(FileNotFoundError, match="artifact reference"):
        prepare_stock_frame(dataset, fee_rate=0.0005, slippage_rate=0.0005)
    with tempfile.TemporaryDirectory() as root:
        published = publish_stock_dataset(dataset, Path(root))
        path = verify_stock_dataset_artifact(published)
        path.unlink()
        with pytest.raises(FileNotFoundError, match="artifact is missing"):
            prepare_stock_frame(published, fee_rate=0.0005, slippage_rate=0.0005)


def test_tampered_snapshot_manifest_is_rejected_before_training():
    with tempfile.TemporaryDirectory() as root:
        published = publish_stock_dataset(_dataset(), Path(root))
        (Path(published.metadata["artifact_path"]) / "snapshot.json").write_text("{}")
        with pytest.raises(UnverifiedStockDataset, match="manifest hash mismatch"):
            verify_stock_dataset_artifact(published)


def test_training_is_immutable_purged_and_reports_cost_aware_losses():
    with tempfile.TemporaryDirectory() as root:
        dataset = publish_stock_dataset(_dataset(), Path(root) / "snapshots")
        prepared = prepare_stock_frame(dataset, fee_rate=0.0005, slippage_rate=0.0005)
        development, final, folds = purged_expanding_walkforward(
            prepared, horizon_days=5, folds=3, embargo_days=1
        )
        assert not development.empty and not final.empty
        for split in folds:
            assert (split.train.label_end < split.validation.date.min() - pd.Timedelta(days=1)).all()
        manifest = train_stock_model(dataset, Path(root) / "models", folds=3, embargo_days=1)
        directory = Path(root) / "models" / manifest["run_id"]
        assert manifest["selected_model"] in {"logistic_regression", "random_forest"}
        assert manifest["final_holdout_rows"] > 0
        metrics = manifest["final_holdout_metrics"]["cost_aware_nonoverlapping_returns"]
        assert metrics["non_overlapping"] is True
        assert metrics["trade_returns_include_losses"] is True
        assert metrics["sample_count"] == metrics["wins"] + metrics["losses"]
        for name, digest in manifest["files"].items():
            assert hashlib.sha256((directory / name).read_bytes()).hexdigest() == digest
        # A worker crash after atomic publication but before DB registration is
        # safe to retry: the exact complete artifact is adopted, not replaced.
        assert train_stock_model(dataset, Path(root) / "models", folds=3, embargo_days=1) == manifest
        with pytest.raises(TrainingCancelled):
            train_stock_model(
                dataset, Path(root) / "models", folds=3, embargo_days=1,
                cancel_requested=lambda: True,
            )


def test_model_identity_is_independent_of_snapshot_filesystem_location():
    with tempfile.TemporaryDirectory() as root:
        dataset = _dataset()
        left = publish_stock_dataset(dataset, Path(root) / "left-snapshots")
        right = publish_stock_dataset(dataset, Path(root) / "right-snapshots")
        left_manifest = train_stock_model(left, Path(root) / "left-models")
        right_manifest = train_stock_model(right, Path(root) / "right-models")
        assert left_manifest["run_id"] == right_manifest["run_id"]
        assert "dataset_artifact_path" not in left_manifest
        assert "dataset_artifact_path" not in right_manifest


def test_tampered_complete_model_artifact_cannot_be_adopted():
    with tempfile.TemporaryDirectory() as root:
        dataset = publish_stock_dataset(_dataset(), Path(root) / "snapshots")
        manifest = train_stock_model(dataset, Path(root) / "models")
        artifact = Path(root) / "models" / manifest["run_id"] / "selected_model.joblib"
        artifact.write_bytes(b"tampered")
        with pytest.raises(ValueError, match="file digest mismatch"):
            train_stock_model(dataset, Path(root) / "models")


def test_completed_model_retry_adopts_before_any_second_fit_or_holdout_evaluation(monkeypatch):
    with tempfile.TemporaryDirectory() as root:
        dataset = publish_stock_dataset(_dataset(), Path(root) / "snapshots")
        output = Path(root) / "models"
        first = train_stock_model(dataset, output)
        assert first["final_holdout_evaluation_count"] == 1

        # The first immutable publication has already used its reserved final
        # holdout. A retry must adopt it from the pre-fit identity contract;
        # any invocation here would be a second fit/evaluation attempt.
        fit_invocations = 1

        def fail_on_second_fit(*args, **kwargs):
            nonlocal fit_invocations
            fit_invocations += 1
            if fit_invocations == 2:
                raise AssertionError("retry attempted a second model fit")
            raise AssertionError("retry unexpectedly fit another model")

        monkeypatch.setattr(stock_training, "_fit_probability", fail_on_second_fit)
        assert stock_training.train_stock_model(dataset, output) == first
        assert fit_invocations == 1


def test_orphaned_complete_snapshot_is_validated_and_adopted():
    with tempfile.TemporaryDirectory() as root:
        dataset = _dataset()
        first = publish_stock_dataset(dataset, Path(root) / "snapshots")
        # Simulates a crash/DB rollback after immutable file publication.
        adopted = publish_stock_dataset(dataset, Path(root) / "snapshots")
        assert adopted.metadata == first.metadata


def test_cancellation_prevents_artifact_publication():
    with tempfile.TemporaryDirectory() as root:
        dataset = publish_stock_dataset(_dataset(), Path(root) / "snapshots")
        with pytest.raises(TrainingCancelled):
            train_stock_model(dataset, Path(root) / "models", cancel_requested=lambda: True)
        assert not (Path(root) / "models").exists()


def test_nonverified_provider_cannot_enter_training():
    dataset = _dataset()
    untrusted = StockDataset(
        dataset.snapshot_id, dataset.dataset_sha256, dataset.identity_sha256, dataset.observations,
        dataset.metadata | {"provider": "fixture"},
    )
    with tempfile.TemporaryDirectory() as root:
        # The publisher only freezes bytes. It does not turn fixture
        # provenance into verified provenance; the trainer still rejects it.
        published = publish_stock_dataset(untrusted, Path(root))
        with pytest.raises(UnverifiedStockDataset, match="provider"):
            prepare_stock_frame(published, fee_rate=0.0005, slippage_rate=0.0005)


def test_database_snapshot_is_provenance_bound_and_references_file_not_blob():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        db.add(Asset(symbol="AAA", name="AAA", asset_type="stock", is_active=True))
        for offset, day in enumerate(pd.bdate_range("2024-01-01", periods=80)):
            price = Decimal(str(100 + offset))
            db.add(
                MarketPrice(
                    symbol="AAA", price_date=day.date(), open=price, high=price + 1,
                    low=price - 1, close=price, adjusted_close=price, volume=1000,
                    source="yfinance", imported_at=datetime.now(timezone.utc),
                )
            )
        db.commit()
        dataset = create_stock_dataset(
            db, cutoff=date(2024, 5, 1), universe=["AAA"], horizon_days=5, provider="yfinance"
        )
        assert dataset.metadata["temporal_semantics"]["point_in_time_verified"] is False
        assert dataset.metadata["capture_vintage"]["latest_imported_at"]
        with tempfile.TemporaryDirectory() as root:
            dataset = publish_stock_dataset(dataset, Path(root))
            row = persist_stock_dataset_snapshot(db, dataset, snapshot_model=StockDatasetSnapshot)
            assert row.snapshot_id == dataset.snapshot_id
            assert row.dataset_sha256 == dataset.dataset_sha256
            assert row.artifact_path == dataset.metadata["artifact_path"]
            assert row.metadata_json == dataset.metadata
            assert not hasattr(row, "dataset_bytes")
            assert Path(row.artifact_path, "dataset.csv").is_file()
    finally:
        db.close()
        engine.dispose()