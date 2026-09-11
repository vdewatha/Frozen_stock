"""One-time final-holdout consumption tests."""
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.base import Base
from app.models import Asset, MarketPrice, StockHoldoutConsumption, StockTrainingJob
import app.services.stock_training as stock_training
import app.services.stock_training_jobs as stock_training_jobs


def _seed_job(db: Session):
    db.add(Asset(symbol="SPY", name="S&P 500", asset_type="stock", is_active=True))
    imported_at = datetime.now(timezone.utc)
    for index, stamp in enumerate(pd.bdate_range(end=date.today(), periods=680)):
        close = 100 + index * .03 + (index % 11 - 5) * .4
        db.add(MarketPrice(
            symbol="SPY", price_date=stamp.date(), open=close * .998, high=close * 1.01,
            low=close * .99, close=close, adjusted_close=close, volume=1_000_000 + index,
            source="yfinance", imported_at=imported_at,
        ))
    db.commit()
    job, duplicate = stock_training_jobs.create_stock_training_job(
        db, symbols=["SPY"], cutoff_at=date.today(), horizon_bars=5,
        provider="yfinance", actor="researcher",
    )
    assert not duplicate
    db.commit()
    return job


def test_crash_after_final_scoring_burns_reservation_and_retry_never_rescores():
    with TemporaryDirectory() as artifacts:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        with Session(engine) as db, patch.object(settings, "stock_training_artifact_root", artifacts):
            job = _seed_job(db)
            real_train = stock_training_jobs.train_stock_model

            def crash_after_final_score(*args, **kwargs):
                def injected_crash():
                    raise RuntimeError("injected crash after final holdout scoring")

                return real_train(*args, after_holdout_scored=injected_crash, **kwargs)

            # The consumption record commits before this injected post-score
            # crash, while no immutable model directory has yet been renamed.
            with patch.object(stock_training_jobs, "train_stock_model", side_effect=crash_after_final_score):
                first = stock_training_jobs.run_stock_training_job(db, job.id)
            assert first == {"status": "failed", "job_id": job.id, "failure_code": "RuntimeError"}
            consumed = db.scalar(
                select(StockHoldoutConsumption).where(
                    StockHoldoutConsumption.reservation_id == job.holdout_reservation_id
                )
            )
            assert consumed is not None and consumed.attempt == 1 and consumed.job_id == job.id
            assert not list((Path(artifacts) / "models").glob("*/manifest.json"))

            # Simulate a deliberate retry of the failed delivery. The callback
            # sees the durable consumption row before the final fit. There are
            # seven pre-holdout fits (six walk-forward plus calibration); an
            # eighth would be a forbidden second final-holdout score.
            row = db.get(StockTrainingJob, job.id)
            row.status, row.failure_code, row.failure_detail = "queued", None, None
            db.commit()
            original_fit = stock_training._fit_probability
            fit_calls = 0

            def reject_second_final_holdout(*args, **kwargs):
                nonlocal fit_calls
                fit_calls += 1
                if fit_calls > 7:
                    raise AssertionError("retry attempted a second final holdout score")
                return original_fit(*args, **kwargs)

            with patch.object(stock_training, "_fit_probability", side_effect=reject_second_final_holdout):
                retry = stock_training_jobs.run_stock_training_job(db, job.id)
            assert retry == {
                "status": "failed", "job_id": job.id,
                "failure_code": "ambiguous_holdout_consumption",
            }
            assert fit_calls == 7
            assert db.query(StockHoldoutConsumption).count() == 1
            assert db.get(StockTrainingJob, job.id).holdout_reservation_id == job.holdout_reservation_id