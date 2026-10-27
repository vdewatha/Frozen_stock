from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import math

import pandas as pd
import pytest

from app.services.backtester import BacktestConfig, downside_deviation, run_backtest


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
        # Zero-cost arithmetic check: the flat model with zero bps is costless.
        result = run_backtest("TEST", "test", prices, BacktestConfig(
            risk_per_trade=0.5, stop_loss_pct=0.5, fees_bps=0, slippage_bps=0, cost_model="flat",
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


def test_backtest_realizes_position_at_end_of_history():
    prices = pd.DataFrame({
        "date": pd.date_range("2025-01-01", periods=62),
        "open": 100.0,
        "close": [100.0] * 61 + [110.0],
    })
    strategy = MagicMock()
    strategy.generate_signal.return_value = SimpleNamespace(action="BUY", confidence=0.9)

    with patch("app.services.backtester.get_strategy", return_value=strategy):
        # The final bar's 100 -> 110 range now crosses the default 6% target
        # intrabar, so widen the target to keep this an end-of-history case.
        result = run_backtest("MSFT", "test", prices, BacktestConfig(
            risk_per_trade=0.5, stop_loss_pct=0.5, take_profit_pct=0.5, fees_bps=0, slippage_bps=0,
            cost_model="flat",
        ))

    assert len(result["trades"]) == 1
    assert result["trades"][0]["exit_reason"] == "end_of_history"
    assert result["trades"][0]["exit_date"].startswith("2025-03-03")
    assert result["trades"][0]["profit_loss"] > 0
    assert result["win_rate"] == 1


def test_sortino_uses_downside_rms_over_all_periods():
    returns = pd.Series([-0.10, 0.02, 0.03])
    expected_downside = math.sqrt((0.10 ** 2) / 3)
    assert downside_deviation(returns) == pytest.approx(expected_downside)


# --- Intrabar exits, cost realism, baselines and statistical confidence ---

def _frame(tail_bars, *, warmup=61, volume=None):
    """61 flat warm-up bars at 100, then explicit (open, high, low, close) bars."""
    rows = [(100.0, 100.0, 100.0, 100.0)] * warmup + list(tail_bars)
    frame = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    frame.insert(0, "date", pd.date_range("2025-01-01", periods=len(rows)).strftime("%Y-%m-%d"))
    if volume is not None:
        frame["volume"] = volume
    return frame


def _run(prices, *, signals=None, benchmark_prices=None, symbol="TEST", **config):
    strategy = MagicMock()
    if signals is None:
        strategy.generate_signal.return_value = SimpleNamespace(action="BUY", confidence=0.9)
    else:
        strategy.generate_signal.side_effect = [SimpleNamespace(action=a, confidence=0.9) for a in signals]
    defaults = dict(risk_per_trade=0.01, stop_loss_pct=0.05, take_profit_pct=0.10,
                    fees_bps=0, slippage_bps=0, cost_model="flat", max_holding_days=50)
    defaults.update(config)
    with patch("app.services.backtester.get_strategy", return_value=strategy):
        return run_backtest(symbol, "test", prices, BacktestConfig(**defaults), benchmark_prices=benchmark_prices)


def test_intrabar_low_triggers_stop_at_stop_level():
    # Entered at 100 on bar 61; bar 62 opens 99 (above the 95 stop) and trades down to 90.
    prices = _frame([(100, 100, 100, 100), (99, 99, 90, 98), (98, 98, 98, 98)])
    result = _run(prices, signals=["BUY", "HOLD", "HOLD"])
    trade = result["trades"][0]
    assert trade["exit_reason"] == "stop_loss_intrabar"
    assert trade["exit_price"] == 95.0
    assert trade["exit_date"] == prices.iloc[62]["date"]


def test_gap_through_stop_fills_at_open_not_stop():
    prices = _frame([(100, 100, 100, 100), (90, 92, 88, 91)])
    result = _run(prices, signals=["BUY", "HOLD"])
    trade = result["trades"][0]
    assert trade["exit_reason"] == "stop_loss_gap"
    assert trade["exit_price"] == 90.0


def test_bar_touching_stop_and_target_assumes_stop_first():
    prices = _frame([(100, 100, 100, 100), (100, 115, 90, 112)])
    result = _run(prices, signals=["BUY", "HOLD"])
    assert result["trades"][0]["exit_reason"] == "stop_loss_intrabar"
    assert result["trades"][0]["exit_price"] == 95.0
    assert result["trades"][0]["profit_loss"] < 0


def test_intrabar_high_triggers_take_profit_and_entry_bar_is_checked():
    # Target reached on the entry bar itself (after the open fill).
    prices = _frame([(100, 111, 99, 108), (108, 108, 108, 108)])
    result = _run(prices, signals=["BUY", "HOLD"])
    trade = result["trades"][0]
    assert trade["exit_reason"] == "take_profit_intrabar"
    assert trade["exit_price"] == 110.0
    assert trade["exit_date"] == prices.iloc[61]["date"]


def test_volatility_scaled_costs_exceed_flat_costs_on_volatile_history():
    closes = [100.0 * (1.03 if i % 2 else 0.97) for i in range(61)]
    warm = [(c, c * 1.01, c * 0.99, c) for c in closes]
    rows = warm + [(100, 100, 100, 100)] * 3
    prices = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    prices.insert(0, "date", pd.date_range("2025-01-01", periods=len(rows)).strftime("%Y-%m-%d"))
    prices["volume"] = 1_000_000
    flat = _run(prices, signals=["BUY", "HOLD", "HOLD"], slippage_bps=5)
    scaled = _run(prices, signals=["BUY", "HOLD", "HOLD"], slippage_bps=5, cost_model="volatility_scaled")
    assert flat["trades"][0]["entry_price"] == 100.05
    assert scaled["trades"][0]["entry_price"] > 100.2
    assert scaled["total_return"] < flat["total_return"]
    assert scaled["cost_model"]["name"] == "volatility_scaled"


def test_participation_cap_limits_entry_size_to_prior_volume():
    prices = _frame([(100, 100, 100, 100)] * 3, volume=1000)
    result = _run(prices, signals=["BUY", "HOLD", "HOLD"], cost_model="volatility_scaled", risk_per_trade=0.5)
    # Uncapped sizing would buy 10,000 shares; 1% of 1,000 average volume is 10.
    assert result["trades"][0]["quantity"] == 10
    assert result["cost_model"]["participation_capped_entries"] == 1


def test_end_of_history_liquidation_costs_reach_total_return():
    prices = _frame([(100, 100, 100, 100)] * 2)
    result = _run(prices, slippage_bps=50, risk_per_trade=0.5, stop_loss_pct=0.5, take_profit_pct=1)
    trade = result["trades"][0]
    assert trade["exit_reason"] == "end_of_history"
    assert result["equity_curve"][-1]["value"] == round(100_000 + trade["profit_loss"], 2)
    assert result["total_return"] < 0


def test_passive_baselines_reported_with_excess_return():
    # Steady rise: buy-and-hold makes money; a strategy that never buys does not.
    rising = [(100 + i, 100 + i, 100 + i, 100 + i) for i in range(1, 31)]
    prices = _frame(rising)
    result = _run(prices, signals=["HOLD"] * 30)
    assert result["number_of_trades"] == 0
    bh = result["baselines"]["buy_and_hold"]
    assert bh["total_return"] == round(130 / 101 - 1, 4)
    assert result["baselines"]["cash"]["total_return"] == 0
    assert result["excess_return"]["vs_cash"] == 0
    assert result["excess_return"]["vs_buy_and_hold"] == -bh["total_return"]
    assert result["beats_baselines"] == {
        "cash": False, "buy_and_hold": False, "exposure_matched_buy_and_hold": False, "spy_buy_and_hold": None,
    }
    assert result["baselines"]["spy_buy_and_hold"]["status"] == "unavailable"
    assert "Strategy does not beat holding cash after costs." in result["rejection_reasons"]


def test_exposure_matched_and_spy_baselines():
    rising = [(100 + i, 100 + i, 100 + i, 100 + i) for i in range(1, 31)]
    prices = _frame(rising)
    spy = _frame([(200, 200, 200, 200)] * 30)
    result = _run(prices, benchmark_prices=spy, risk_per_trade=0.5, stop_loss_pct=0.5, take_profit_pct=5)
    matched = result["baselines"]["exposure_matched_buy_and_hold"]
    assert 0.9 < matched["exposure"] <= 1
    assert result["baselines"]["spy_buy_and_hold"]["total_return"] == 0
    assert result["excess_return"]["vs_spy_buy_and_hold"] == result["total_return"]
    assert result["beats_baselines"]["spy_buy_and_hold"] is True
    # Symbol SPY uses its own buy-and-hold as the market baseline.
    own = _run(prices, symbol="SPY", signals=["HOLD"] * 30)
    assert own["baselines"]["spy_buy_and_hold"]["total_return"] == own["baselines"]["buy_and_hold"]["total_return"]


def test_bootstrap_confidence_reported_and_small_samples_rejected():
    small = _run(_frame([(100, 100, 100, 100)] * 5), signals=["HOLD"] * 5)
    assert small["statistics"]["bootstrap"]["status"] == "insufficient_observations"
    assert "Too few return observations for a bootstrap confidence interval." in small["rejection_reasons"]

    rng = __import__("numpy").random.default_rng(0)
    closes = 100 * (1 + rng.normal(0, 0.01, 200)).cumprod()
    noisy = _frame([(c, c, c, c) for c in closes])
    result = _run(noisy, risk_per_trade=0.5, stop_loss_pct=0.9, take_profit_pct=9)
    boot = result["statistics"]["bootstrap"]
    assert boot["status"] == "ok"
    low, high = boot["mean_return_ci"]
    assert low < result["statistics"]["mean_period_return"] < high
    assert boot["sharpe_ci"][0] < boot["sharpe_ci"][1]
    # Pure noise must not be accepted as evidence of profitability.
    if low <= 0:
        assert any("bootstrap confidence interval" in reason for reason in result["rejection_reasons"])
    assert result["statistics"]["probabilistic_sharpe"]["trials"] == 1
