from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

from app.services import model_tracking


def test_realization_history_is_cached_per_provider():
    predictions = [
        SimpleNamespace(id=1, symbol="AAPL", source="database:yfinance", prediction_date=date(2026, 1, 2), horizon_days=1,
                         probability_up=0.6, is_realized=False),
        SimpleNamespace(id=2, symbol="AAPL", source="database:alpaca_iex_daily", prediction_date=date(2026, 1, 2), horizon_days=1,
                         probability_up=0.6, is_realized=False),
    ]
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = predictions
    histories = {
        "yfinance": pd.DataFrame([{"date": "2026-01-02", "close": 100}, {"date": "2026-01-03", "close": 110}]),
        "alpaca_iex_daily": pd.DataFrame([{"date": "2026-01-02", "close": 100}, {"date": "2026-01-03", "close": 90}]),
    }

    def history(*args, provider_source, **kwargs):
        return histories[provider_source], provider_source

    with patch.object(model_tracking, "trusted_history", side_effect=history):
        result = model_tracking.score_realized_predictions(db)

    assert result["scored"] == 2
    assert predictions[0].realized_up is True
    assert predictions[1].realized_up is False
