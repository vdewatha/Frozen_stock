from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models import (
    MarketPrice,
    StockPaperTrial,
    StockPaperTrialDecision,
    StockPaperTrialOutcome,
)
from app.services.feature_pipeline import feature_config_id
from app.services.stock_accuracy import (
    build_accuracy_evidence,
    market_price_vintage,
    resolve_trial_outcomes,
)


class TestPointInTimeAccuracy:
    def setup_method(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)

    def teardown_method(self):
        self.engine.dispose()

    def _price(self, db, day, close, imported_at, symbol="SPY"):
        row = MarketPrice(
            symbol=symbol,
            price_date=day,
            open=Decimal(str(close)),
            high=Decimal(str(close)),
            low=Decimal(str(close)),
            close=Decimal(str(close)),
            adjusted_close=Decimal(str(close)),
            volume=100,
            source="yfinance",
            imported_at=imported_at,
        )
        db.add(row)
        db.flush()
        return row

    def _trial_and_decision(
        self, db, *, split=None, trial_id="trial-accuracy", symbol="SPY"
    ):
        feature = self._price(
            db, date(2025, 1, 2), 100,
            datetime(2025, 1, 2, 20, tzinfo=timezone.utc),
            symbol=symbol,
        )
        version = market_price_vintage([feature])
        trial = StockPaperTrial(
            id=trial_id,
            binding_id=1,
            actor="test",
            status="running",
            policy={"label_horizon_sessions": 2},
            lineage={
                "model_run_id": "run-1",
                "snapshot_id": "snapshot-1",
                "dataset_sha256": "d" * 64,
                "feature_config_id": feature_config_id(),
                "universe": [symbol],
                "cost_assumptions": {"commission_rate": "0.001"},
            },
        )
        db.add(trial)
        db.flush()
        lineage = {
            **trial.lineage,
            "feature_timestamp": "2025-01-02T21:00:00+00:00",
            "decision_cutoff_timestamp": "2025-01-02T22:00:00+00:00",
            "feature_data_version": version,
            "feature_provider": "yfinance",
            "bar_exchange_timestamp": "2025-01-02T20:59:00+00:00",
            "probability": 0.8,
            "features": {"signal": 1.0},
        }
        if split:
            lineage["evaluation_split"] = split
        decision = StockPaperTrialDecision(
            trial_id=trial.id,
            symbol=symbol,
            bar_timestamp=datetime(2025, 1, 2, 20, tzinfo=timezone.utc),
            decision_timestamp=datetime(2025, 1, 2, 22, tzinfo=timezone.utc),
            action="buy",
            qualifying=True,
            lineage=lineage,
        )
        db.add(decision)
        db.flush()
        return trial, decision, feature

    def test_outcome_is_idempotent_and_labels_are_separate_from_holdout(self):
        with Session(self.engine) as db:
            trial, decision, _ = self._trial_and_decision(db)
            self._price(db, date(2025, 1, 3), 101, datetime(2025, 1, 3, 22))
            self._price(db, date(2025, 1, 4), 102, datetime(2025, 1, 4, 22))
            first = resolve_trial_outcomes(
                db, trial.id, as_of=datetime(2025, 1, 5, tzinfo=timezone.utc)
            )
            second = resolve_trial_outcomes(
                db, trial.id, as_of=datetime(2025, 1, 5, tzinfo=timezone.utc)
            )
            assert len(first) == len(second) == 1
            assert db.query(StockPaperTrialOutcome).count() == 1
            assert first[0].label_status == "resolved"
            assert first[0].label_lineage["evaluation_population"] == "forward_paper_outcome"

            holdout_trial, _, _ = self._trial_and_decision(
                db, split="holdout", trial_id="trial-holdout", symbol="QQQ"
            )
            holdout = resolve_trial_outcomes(db, holdout_trial.id, as_of=datetime(2025, 1, 5, tzinfo=timezone.utc))
            assert holdout[0].label_status == "blocked"
            assert holdout[0].reason == "non_live_label_population"

    def test_restatement_removes_previous_accuracy_credit(self):
        with Session(self.engine) as db:
            trial, _, feature = self._trial_and_decision(db)
            self._price(db, date(2025, 1, 3), 101, datetime(2025, 1, 3, 22))
            self._price(db, date(2025, 1, 4), 102, datetime(2025, 1, 4, 22))
            as_of = datetime(2025, 1, 5, tzinfo=timezone.utc)
            before = build_accuracy_evidence(db, trial, as_of=as_of)
            assert before["window"]["sample_count"] == 1
            feature.adjusted_close = Decimal("99")
            db.flush()
            after = build_accuracy_evidence(db, trial, as_of=as_of)
            assert after["window"]["sample_count"] == 0
            assert after["classification"] == "blocked"
            assert "feature_data_restated" in after["data_health"]["reasons"]

    def test_missing_future_label_is_unknown_not_a_negative(self):
        with Session(self.engine) as db:
            trial, _, _ = self._trial_and_decision(db)
            report = build_accuracy_evidence(
                db, trial, as_of=datetime(2025, 1, 3, tzinfo=timezone.utc)
            )
            assert report["window"]["sample_count"] == 0
            assert report["classification"] == "insufficient"
            assert report["data_health"]["unknown_labels"] == 1