from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import math

import pandas as pd
import pytest

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


@pytest.mark.parametrize("closes", [[80], [80, 81], [100, 100], []])
def test_risk_metrics_include_initial_capital(closes):
    prices = pd.DataFrame({
        "date": pd.date_range("2025-01-01", periods=61 + len(closes)),
        "open": 100.0,
        "close": [100.0] * 61 + closes,
    })
    strategy = MagicMock()
    strategy.generate_signal.return_value = SimpleNamespace(action="BUY", confidence=0.9)
    with patch("app.services.backtester.get_strategy", return_value=strategy):
        result = run_backtest("TEST", "test", prices, BacktestConfig(
            risk_per_trade=0.5, stop_loss_pct=0.5, fees_bps=0, slippage_bps=0,
        ))

    # Full investment at 100: an immediate fall to 80 is a 20% drawdown,
    # even if there is only one evaluated bar or the next close recovers.
    assert result["max_drawdown"] == (0.2 if closes and closes[0] == 80 else 0)
    assert len(result["equity_curve"]) == len(closes)
    assert result["total_return"] == (round(closes[-1] / 100 - 1, 4) if closes else 0)
    if closes == [80, 81]:
        daily_returns = [-0.2, 0.0125]
        mean = sum(daily_returns) / 2
        sample_std = math.sqrt(sum((value - mean) ** 2 for value in daily_returns))
        assert result["sharpe_ratio"] == round(mean / sample_std * math.sqrt(252), 4)
    else:
        assert result["sharpe_ratio"] == 0
