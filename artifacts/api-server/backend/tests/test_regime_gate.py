"""ADX regime classification and the opt-in regime gate on strategy BUYs."""
import numpy as np
import pandas as pd
import pytest

from app.services.strategies.base import BaseStrategy, StrategySignalResult
from app.services.strategies.regime import RegimeGatedStrategy, classify_regime, wilder_adx
from app.services.strategies.registry import get_strategy


def _frame(close):
    close = pd.Series(close, dtype=float)
    return pd.DataFrame({"date": pd.date_range("2024-01-01", periods=len(close), freq="B"),
                         "open": close, "high": close * 1.005, "low": close * 0.995, "close": close,
                         "volume": 1_000_000})


def _trend(direction, n=200):
    return _frame(100 * np.exp(direction * 0.01 * np.arange(n)))


def _chop(n=200):
    return _frame(100 + 2 * np.sin(np.arange(n) / 2.0))


def test_a_steady_rise_is_a_strong_uptrend():
    state = classify_regime(_trend(+1))
    assert state["regime"] == "trend_up" and state["adx"] > 25 and state["plus_di"] > state["minus_di"]


def test_a_steady_fall_is_a_strong_downtrend():
    assert classify_regime(_trend(-1))["regime"] == "trend_down"


def test_a_sideways_chop_is_a_range():
    state = classify_regime(_chop())
    assert state["regime"] == "range" and state["adx"] < 20


def test_short_history_is_neutral():
    assert classify_regime(_chop(20))["regime"] == "neutral"


def test_adx_matches_a_hand_computed_value_shape():
    values = wilder_adx(_trend(+1))
    assert values["adx"].dropna().between(0, 100).all()


class _AlwaysBuy(BaseStrategy):
    name = slug = "always_buy"
    daily_bar_execution = True

    def generate_signal(self, symbol, data):
        return StrategySignalResult("BUY", 0.6, 0.4, 0.7, "test buy", {"k": 1})


@pytest.mark.parametrize("gate,frame,allowed", [
    ("range", _chop(), True),
    ("range", _trend(+1), False),
    ("not_trending", _trend(-1), False),
    ("not_downtrend", _trend(+1), True),
    ("not_downtrend", _trend(-1), False),
    ("trend_up", _trend(+1), True),
    ("trend_up", _chop(), False),
])
def test_the_gate_only_turns_blocked_buys_into_holds(gate, frame, allowed):
    gated = RegimeGatedStrategy(_AlwaysBuy(), gate, {})
    result = gated.generate_signal("SPY", frame)
    assert result.action == ("BUY" if allowed else "HOLD")
    assert result.features["k"] == 1 and result.features["regime_gate"]["allowed"] is allowed
    assert gated.slug == "always_buy" and gated.daily_bar_execution is True  # attributes pass through


def test_non_buy_signals_are_never_changed():
    class _Sell(_AlwaysBuy):
        def generate_signal(self, symbol, data):
            return StrategySignalResult("SELL", 0.4, 0.6, 0.7, "test sell", {})

    assert RegimeGatedStrategy(_Sell(), "trend_up", {}).generate_signal("SPY", _chop()).action == "SELL"


def test_registry_applies_the_gate_only_when_asked():
    plain = get_strategy("rsi_mean_reversion", {})
    gated = get_strategy("rsi_mean_reversion", {"regime_gate": "range"})
    assert not isinstance(plain, RegimeGatedStrategy)
    assert isinstance(gated, RegimeGatedStrategy) and gated.slug == "rsi_mean_reversion"
    with pytest.raises(ValueError, match="regime_gate"):
        get_strategy("rsi_mean_reversion", {"regime_gate": "sideways"})
