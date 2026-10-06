from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

from app.services.backtester import BacktestConfig, run_backtest


def test_backtest_uses_prior_bar_signal_and_next_open_fill():
    rows = []
    for index in range(63):
        close = 150 if index == 61 else 100
        rows.append(
            {
                "date": f"2026-01-{index + 1:02d}",
                "open": 100,
                "high": max(100, close),
                "low": min(100, close),
                "close": close,
                "volume": 1000,
            }
        )
    prices = pd.DataFrame(rows)
    strategy = MagicMock()
    strategy.generate_signal.return_value = SimpleNamespace(action="BUY", confidence=0.9)

    with patch("app.services.backtester.get_strategy", return_value=strategy):
        result = run_backtest(
            "TEST",
            "test",
            prices,
            BacktestConfig(max_holding_days=1, stop_loss_pct=0.5, take_profit_pct=5),
        )

    assert result["execution_model"] == "next_open_no_lookahead"
    assert result["trades"][0]["entry_price"] == 100.05
    observed_history_ends = [
        str(call.args[1]["date"].iloc[-1]) for call in strategy.generate_signal.call_args_list
    ]
    assert observed_history_ends == ["2026-01-61", "2026-01-62"]
