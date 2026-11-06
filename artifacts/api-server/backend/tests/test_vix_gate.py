"""The opt-in VIX gate on strategy BUYs: lookup by bar date, modes, fail-open."""
import pandas as pd
import pytest

from app.services.strategies import vix_gate
from app.services.strategies.base import BaseStrategy, StrategySignalResult
from app.services.strategies.regime import RegimeGatedStrategy
from app.services.strategies.registry import get_strategy
from app.services.strategies.vix_gate import VixGatedStrategy, set_vix_history, vix_reading


@pytest.fixture(autouse=True)
def _vix():
    # Mon 2024-03-04 .. Fri 03-15; a 3-day weekend gap after 03-08.
    dates = ["2024-03-04", "2024-03-05", "2024-03-06", "2024-03-07", "2024-03-08",
             "2024-03-11", "2024-03-12", "2024-03-13", "2024-03-14", "2024-03-15"]
    closes = [14.0, 14.5, 15.0, 15.0, 15.4, 16.0, 18.0, 22.0, 26.0, 31.0]
    set_vix_history(pd.DataFrame({"date": dates, "close": closes}))
    yield
    set_vix_history(None)


def _bars(last_date, n=30):
    dates = pd.bdate_range(end=last_date, periods=n)
    return pd.DataFrame({"date": dates.strftime("%Y-%m-%d"), "open": 100.0, "high": 101.0,
                         "low": 99.0, "close": 100.0, "volume": 1_000_000})


class _AlwaysBuy(BaseStrategy):
    name = slug = "always_buy"
    daily_bar_execution = True

    def generate_signal(self, symbol, data):
        return StrategySignalResult("BUY", 0.6, 0.4, 0.7, "test buy", {"k": 1})


def test_the_reading_uses_the_close_dated_on_the_last_bar():
    reading = vix_reading("2024-03-13")
    assert reading["vix"] == 22.0 and reading["vix_date"] == "2024-03-13"
    # 5 sessions back is 03-06 (15.0): +46.67%.
    assert reading["vix_change_pct"] == pytest.approx(46.67)


def test_a_bar_dated_on_a_vix_holiday_uses_the_previous_close():
    assert vix_reading("2024-03-09")["vix_date"] == "2024-03-08"


def test_a_stale_or_missing_reading_is_unavailable():
    assert vix_reading("2024-03-30")["reason"] == "vix_unavailable"
    assert vix_reading("2024-01-02")["reason"] == "vix_unavailable"


@pytest.mark.parametrize("parameters,last_date,allowed", [
    ({"vix_level": 25}, "2024-03-13", True),     # 22 < 25
    ({"vix_level": 25}, "2024-03-14", False),    # 26
    ({"vix_level": 20}, "2024-03-13", False),
])
def test_below_blocks_buys_when_fear_is_high(parameters, last_date, allowed):
    result = VixGatedStrategy(_AlwaysBuy(), "below", parameters).generate_signal("SPY", _bars(last_date))
    assert result.action == ("BUY" if allowed else "HOLD")
    assert result.features["k"] == 1 and result.features["vix_gate"]["allowed"] is allowed
    if not allowed:
        assert result.features["gated_action"] == "BUY" and result.reason.startswith("VIX gate below")


def test_above_buys_only_when_fear_is_elevated():
    gated = VixGatedStrategy(_AlwaysBuy(), "above", {"vix_level": 20})
    assert gated.generate_signal("SPY", _bars("2024-03-08")).action == "HOLD"   # 15.4
    assert gated.generate_signal("SPY", _bars("2024-03-13")).action == "BUY"    # 22


def test_not_rising_blocks_a_vix_spike():
    gated = VixGatedStrategy(_AlwaysBuy(), "not_rising", {"vix_rise_pct": 10, "vix_rise_days": 5})
    # 03-08 has fewer than 5 earlier sessions, so there is no change to judge.
    assert gated.generate_signal("SPY", _bars("2024-03-08")).action == "BUY"
    # 03-11: 16.0 vs 14.0 five sessions earlier is +14.3%.
    assert gated.generate_signal("SPY", _bars("2024-03-11")).action == "HOLD"
    blocked = gated.generate_signal("SPY", _bars("2024-03-13"))
    assert blocked.action == "HOLD" and "VIX up" in blocked.reason


def test_without_data_the_gate_fails_open_and_says_so():
    set_vix_history({})
    result = VixGatedStrategy(_AlwaysBuy(), "below", {}).generate_signal("SPY", _bars("2024-03-13"))
    assert result.action == "BUY" and result.features["vix_gate"]["reason"] == "vix_unavailable"


def test_non_buy_signals_are_never_changed():
    class _Sell(_AlwaysBuy):
        def generate_signal(self, symbol, data):
            return StrategySignalResult("SELL", 0.4, 0.6, 0.7, "test sell", {})

    assert VixGatedStrategy(_Sell(), "above", {"vix_level": 99}).generate_signal("SPY", _bars("2024-03-13")).action == "SELL"


def test_utc_timestamps_from_the_paper_feed_map_to_their_session_date():
    frame = _bars("2024-03-14")
    frame["date"] = pd.to_datetime(frame["date"]).dt.tz_localize("UTC") + pd.Timedelta(hours=4)
    result = VixGatedStrategy(_AlwaysBuy(), "below", {"vix_level": 25}).generate_signal("SPY", frame)
    assert result.features["vix_gate"]["vix_date"] == "2024-03-14" and result.action == "HOLD"


def test_registry_applies_the_gate_only_when_asked_and_stacks_with_adx():
    assert not isinstance(get_strategy("rsi2_pullback", {}), VixGatedStrategy)
    gated = get_strategy("rsi_mean_reversion", {"vix_gate": "below", "regime_gate": "range"})
    assert isinstance(gated, VixGatedStrategy) and isinstance(gated._inner, RegimeGatedStrategy)
    assert gated.slug == "rsi_mean_reversion" and gated.level == vix_gate.DEFAULT_LEVELS["below"]
    with pytest.raises(ValueError, match="vix_gate"):
        get_strategy("rsi2_pullback", {"vix_gate": "calm"})
