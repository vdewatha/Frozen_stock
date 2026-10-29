"""Daily-bar strategies (RSI(2), IBS) may open positions only early in the session.

The backtester fills their entries at the next session's open, so paper BUYs
are restricted to [open + start, open + end).  Every test uses fakes; nothing
reaches a broker.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401  register all tables
from app.core.config import settings
from app.db.base import Base
from app.models import Strategy, StrategySignal
from app.services import stock_paper_signal_batch as batch
from app.services.strategies.base import StrategySignalResult
from app.services.strategy_execution_policy import daily_bar_entry_window, gate_daily_bar_entry
from tests.test_stock_paper_signal_batch import _DataDouble

UTC = timezone.utc
# Thursday 2026-10-29: regular session 09:30-16:00 ET = 13:30-20:00 UTC.
OPEN = datetime(2026, 10, 29, 13, 30, tzinfo=UTC)
RSI2_PARAMS = {"symbol_scope": ["SPY", "QQQ", "IWM", "DIA"]}


def _buy(reason="always buy"):
    return StrategySignalResult(action="BUY", probability_up=0.7, probability_down=0.3,
                                confidence=0.8, reason=reason, features={"rsi": 3.0})


# ----------------------------------------------------------------- window

def test_window_defaults_to_open_plus_5_to_45_minutes():
    assert daily_bar_entry_window(OPEN + timedelta(hours=3)) == (
        OPEN + timedelta(minutes=5), OPEN + timedelta(minutes=45))


def test_no_window_on_a_holiday():
    assert daily_bar_entry_window(datetime(2026, 11, 26, 15, 0, tzinfo=UTC)) is None  # Thanksgiving


def test_window_is_clamped_to_an_early_close():
    # Day after Thanksgiving closes at 13:00 ET (18:00 UTC).
    early = datetime(2026, 11, 27, 15, 0, tzinfo=UTC)
    with patch.object(settings, "paper_daily_bar_entry_window_end_minutes", 300):
        start, end = daily_bar_entry_window(early)
    assert start == datetime(2026, 11, 27, 14, 35, tzinfo=UTC)
    assert end == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)


def test_empty_configured_window_disables_entries():
    with patch.object(settings, "paper_daily_bar_entry_window_start_minutes", 30), \
         patch.object(settings, "paper_daily_bar_entry_window_end_minutes", 30):
        assert daily_bar_entry_window(OPEN + timedelta(minutes=30)) is None
        gated = gate_daily_bar_entry(_buy(), OPEN + timedelta(minutes=30))
    assert gated.action == "HOLD"
    assert gated.features["entry_timing"]["withheld_action"] == "BUY"


# ------------------------------------------------------------------- gate

def test_buy_inside_window_is_kept_and_records_timing():
    gated = gate_daily_bar_entry(_buy(), OPEN + timedelta(minutes=20))
    assert gated.action == "BUY"
    assert gated.confidence == 0.8
    timing = gated.features["entry_timing"]
    assert timing["in_window"] is True
    assert timing["backtest_execution_model"] == "next_open_no_lookahead"
    assert timing["window_start"].startswith("2026-10-29T09:35")
    assert timing["window_end"].startswith("2026-10-29T10:15")
    assert timing["evaluated_at"].startswith("2026-10-29T09:50")
    assert "withheld_action" not in timing
    assert gated.features["rsi"] == 3.0


@pytest.mark.parametrize("minutes", [0, 4, 45, 120, 385])
def test_buy_before_or_after_window_is_withheld(minutes):
    gated = gate_daily_bar_entry(_buy(), OPEN + timedelta(minutes=minutes))
    assert gated.action == "HOLD"
    assert gated.confidence < 0.55
    assert gated.features["entry_timing"]["in_window"] is False
    assert gated.features["entry_timing"]["withheld_action"] == "BUY"
    assert gated.features["rsi"] == 3.0
    assert "BUY withheld" in gated.reason


@pytest.mark.parametrize("action", ["HOLD", "SELL"])
def test_non_buy_signals_are_never_changed(action):
    signal = StrategySignalResult(action=action, probability_up=0.4, probability_down=0.6,
                                  confidence=0.7, reason="x", features={})
    gated = gate_daily_bar_entry(signal, OPEN + timedelta(hours=4))
    assert gated.action == action and gated.confidence == 0.7
    assert gated.features["entry_timing"]["in_window"] is False


# ------------------------------------------------------------------ batch

class _AlwaysBuy:
    def __init__(self, *_args, **_kwargs):
        pass

    def generate_signal(self, symbol, data):
        return _buy()


def _run_batch(now):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    batch.clear_signal_cache()
    data = _DataDouble(["SPY"])
    try:
        with Session(engine) as db:
            db.add(Strategy(name="RSI(2) Pullback", strategy_type="rsi2_pullback", parameters=dict(RSI2_PARAMS),
                            is_active=True, current_status="paper_trading_candidate"))
            db.add(Strategy(name="IBS", strategy_type="ibs_mean_reversion", parameters=dict(RSI2_PARAMS),
                            is_active=True, current_status="paper_trading_candidate"))
            db.add(Strategy(name="MA", strategy_type="moving_average_crossover", parameters={},
                            is_active=True, current_status="paper_trading_candidate"))
            db.commit()
            stack = [*data.patches(), patch("app.services.stock_paper_ledger.get_strategy", _AlwaysBuy)]
            for item in stack:
                item.start()
            try:
                outcomes, _stats = batch.generate_cycle_signals(
                    db, ["SPY"], db.query(Strategy).order_by(Strategy.id).all(), now=now)
            finally:
                for item in stack:
                    item.stop()
            features = {
                row.strategy_id: row.features for row in db.query(StrategySignal).all()
            }
            ids = {row.strategy_type: row.id for row in db.query(Strategy).all()}
        return outcomes, {slug: features[ids[slug]] for slug in ids}
    finally:
        batch.clear_signal_cache()
        engine.dispose()


def test_batch_allows_daily_bar_buys_inside_window():
    outcomes, features = _run_batch(OPEN + timedelta(minutes=10))
    assert outcomes[("SPY", "rsi2_pullback")]["signal_action"] == "BUY"
    assert outcomes[("SPY", "ibs_mean_reversion")]["signal_action"] == "BUY"
    assert features["rsi2_pullback"]["entry_timing"]["in_window"] is True
    assert features["rsi2_pullback"]["entry_timing"]["signal_bar"] is not None


@pytest.mark.parametrize("minutes", [2, 60])
def test_batch_withholds_daily_bar_buys_outside_window_only(minutes):
    outcomes, features = _run_batch(OPEN + timedelta(minutes=minutes))
    assert outcomes[("SPY", "rsi2_pullback")]["signal_action"] == "HOLD"
    assert outcomes[("SPY", "ibs_mean_reversion")]["signal_action"] == "HOLD"
    assert features["rsi2_pullback"]["entry_timing"]["withheld_action"] == "BUY"
    # Other strategies are unaffected and carry no entry-timing record.
    assert outcomes[("SPY", "moving_average_crossover")]["signal_action"] == "BUY"
    assert "entry_timing" not in features["moving_average_crossover"]


def test_withheld_buy_does_not_poison_the_signal_cache():
    _run_batch(OPEN + timedelta(minutes=60))
    outcomes, _features = _run_batch(OPEN + timedelta(minutes=10))
    assert outcomes[("SPY", "rsi2_pullback")]["signal_action"] == "BUY"
