"""Crash-retry coverage for first-publication stock snapshot policy metadata."""
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.base import Base
from app.models import Asset, MarketPrice, StockDatasetSnapshot
from app.services.stock_training_jobs import create_stock_training_job


def test_rollback_retry_adopts_original_capture_policy_despite_new_wall_clock():
    """A retry must inherit policy metadata from the immutable first publish."""
    first_capture = datetime(2025, 1, 1, 9, tzinfo=timezone.utc)
    retry_capture = datetime(2025, 1, 2, 9, tzinfo=timezone.utc)
    cutoff = date(2025, 1, 1)
    with TemporaryDirectory() as artifacts:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        with Session(engine) as db, patch.object(settings, "stock_training_artifact_root", artifacts):
            db.add(Asset(symbol="SPY", name="S&P 500", asset_type="stock", is_active=True))
            for index, stamp in enumerate(pd.bdate_range(end=cutoff, periods=80)):
                close = 100 + index * .1
                db.add(MarketPrice(
                    symbol="SPY", price_date=stamp.date(), open=close, high=close * 1.01,
                    low=close * .99, close=close, adjusted_close=close, volume=1_000_000,
                    source="yfinance", imported_at=datetime(2024, 12, 31, tzinfo=timezone.utc),
                ))
            db.commit()

            # Publication has completed, but the surrounding transaction
            # crashes before its snapshot/job rows are durable.
            with patch(
                "app.services.stock_training_jobs.persist_stock_dataset_snapshot",
                side_effect=RuntimeError("simulate database rollback after publish"),
            ), patch("app.services.stock_training_jobs._now", return_value=first_capture):
                with pytest.raises(RuntimeError, match="database rollback"):
                    create_stock_training_job(
                        db, symbols=["SPY"], cutoff_at=cutoff, horizon_bars=5,
                        provider="yfinance", actor="researcher",
                    )
            db.rollback()

            snapshot_dirs = list((Path(artifacts) / "snapshots").iterdir())
            assert len(snapshot_dirs) == 1
            assert (snapshot_dirs[0] / "snapshot.json").is_file()

            # This fresh request has a later capture date and would calculate
            # different eligibility. Adoption must preserve the original
            # immutable first-publication policy instead.
            with patch("app.services.stock_training_jobs._now", return_value=retry_capture):
                job, duplicate = create_stock_training_job(
                    db, symbols=["SPY"], cutoff_at=cutoff, horizon_bars=5,
                    provider="yfinance", actor="researcher",
                )
            assert not duplicate
            snapshot = db.get(StockDatasetSnapshot, job.snapshot_id)
            assert snapshot is not None
            assert snapshot.metadata_json["captured_at"] == first_capture.isoformat()
            assert snapshot.metadata_json["binding_eligible"] is True
            assert snapshot.metadata_json["binding_eligibility_reason"].startswith("Current-cutoff")