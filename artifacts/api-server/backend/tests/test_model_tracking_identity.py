from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import ModelPrediction
from app.services import model_tracking


def _prediction_result():
    return {
        "symbol": "AAPL",
        "prediction_date": "2026-10-02",
        "latest_features": {"momentum": 0.1},
        "predictions": [
            {
                "horizon_days": 1,
                "probability_up": 0.6,
                "probability_down": 0.4,
                "expected_return": 0.01,
                "probabilities_by_model": {"test": 0.6},
            },
            {
                "horizon_days": 5,
                "probability_up": 0.62,
                "probability_down": 0.38,
                "expected_return": 0.02,
                "probabilities_by_model": {"test": 0.62},
            },
        ],
        "walk_forward": [],
    }


def test_repeated_model_refresh_reuses_prediction_identity():
    engine = create_engine("sqlite://", future=True)
    ModelPrediction.__table__.create(engine)
    with Session(engine) as db, patch.object(model_tracking, "trusted_history", return_value=(object(), "database:yfinance")), \
            patch.object(model_tracking, "predict_probabilities", side_effect=[_prediction_result(), _prediction_result()]), \
            patch.object(model_tracking, "write_audit_log"):
        first = model_tracking.run_and_persist_model_predictions(db, "AAPL")
        second = model_tracking.run_and_persist_model_predictions(db, "AAPL")
        rows = db.query(ModelPrediction).all()

    assert len(rows) == 2
    assert first["saved_prediction_ids"] == second["saved_prediction_ids"]
