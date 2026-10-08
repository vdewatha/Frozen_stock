from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.services import model_tracking
from app.tasks import jobs


def test_model_fitting_runs_outside_historical_read_transaction():
    engine = create_engine("sqlite://")
    prices = object()

    def read_history(db, symbol, limit, minimum):
        assert (symbol, limit, minimum) == ("SPY", 420, 140)
        db.execute(text("SELECT 1"))
        assert db.in_transaction()
        return prices, "database:yfinance"

    with Session(engine) as db:
        def fit(symbol, frame, source):
            assert not db.in_transaction()
            assert (symbol, frame, source) == ("SPY", prices, "database:yfinance")
            return {"prediction_date": None}

        with patch.object(model_tracking, "trusted_history", side_effect=read_history), \
                patch.object(model_tracking, "predict_probabilities", side_effect=fit) as predictor:
            result = model_tracking.run_and_persist_model_predictions(db, "spy")
        predictor.assert_called_once()
        assert result["saved_prediction_ids"] == []
        assert result["saved_validation_fold_ids"] == []
    engine.dispose()


def test_failed_read_transaction_commit_prevents_model_fitting():
    db = MagicMock()
    db.commit.side_effect = RuntimeError("read transaction could not close")
    with patch.object(model_tracking, "trusted_history", return_value=(object(), "database:yfinance")), \
            patch.object(model_tracking, "predict_probabilities") as predictor, \
            pytest.raises(RuntimeError, match="read transaction could not close"):
        model_tracking.run_and_persist_model_predictions(db, "SPY")
    predictor.assert_not_called()
    db.add.assert_not_called()


@pytest.mark.parametrize("strategy,budget,expected", [
    ("model_predictive_long", 99, 1),
    ("model_predictive_long", 3, 1),
    ("model_predictive_long", 1, 1),
    ("model_predictive_long", 0, 1),
    ("model_predictive_long", -1, 1),
    ("moving_average_crossover", 99, 3),
    ("moving_average_crossover", 2, 2),
    ("moving_average_crossover", 0, 1),
])
def test_learning_scope_caps_candidate_budget_without_promotions(strategy, budget, expected):
    db = MagicMock()
    experiment_result = {
        "symbol": "SPY", "strategy": strategy, "source": "database:yfinance",
        "baseline_score": 0.5, "experiments": [], "applied_parameters": None,
    }
    with patch.object(jobs, "_run_job", side_effect=lambda name, work: work(db)), \
            patch.object(jobs, "_claim_learning_dispatch", return_value=(None, "claimed")), \
            patch.object(jobs, "run_strategy_experiments", return_value=experiment_result) as experiments:
        result = jobs.strategy_learning_scope_job.run("SPY", strategy, max_candidates=budget)
    experiments.assert_called_once_with(
        db, symbol="SPY", strategy_slug=strategy,
        max_candidates=expected, apply_promotions=False,
    )
    assert result["paper_only"] is True
    assert result["applied_parameters"] is None
