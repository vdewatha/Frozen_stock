"""Exit autopsy: the post-exit path, the exit flags, and the stop / time-stop experiments."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models import AuditLog, MarketPrice, StockPaperTrial, StockPaperTrialDecision
from app.models.stock_paper import StockPaperAccount, StockPaperFill, StockPaperOrder, StockPaperTrialLot
from app.services import experiment_engine as engine
from app.services import gate_replay
from app.services import loss_diagnosis as ld
from app.services import trade_attribution as ta
from app.services.meta_label import REVERSION_BARRIER
from app.services.strategy_execution_policy import exit_policy_for
from app.services.trade_mistakes import trial_lot_trades
from app.services.triple_barrier import BarrierConfig
from tests.test_experiment_engine import NOW as ENGINE_NOW, _attribution, _lesson, _window, small_universe  # noqa: F401
from tests.test_loss_feedback_loop import _book, _strategy, db  # noqa: F401  (fixture)
from tests.test_meta_label import _walk
from tests.test_paper_trade_scorecard import at

UTC = timezone.utc
# ``_book``: AAPL bought 2026-10-29 14:01 UTC at 100.5, stopped the same session at 96.8; the
# strategy's time rule is 5 sessions, so the planned horizon is 2026-11-05.
ENTRY_SESSION = date(2026, 10, 29)
HORIZON = date(2026, 11, 5)
SESSIONS_AFTER = [date(2026, 10, 30), date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5)]


def _bar(price_date, close, high=None, low=None):
    return {"price_date": price_date, "close": close, "high": high if high is not None else close,
            "low": low if low is not None else close}


def _daily(db, symbol, price_date, close, high=None, low=None):
    db.add(MarketPrice(symbol=symbol, price_date=price_date, open=Decimal(str(close)),
                       high=Decimal(str(high if high is not None else close)),
                       low=Decimal(str(low if low is not None else close)), close=Decimal(str(close)),
                       adjusted_close=Decimal(str(close)), volume=1000, source="test"))


# ------------------------------------------------------------ post-exit path

def test_session_arithmetic():
    assert ta.sessions_after(ENTRY_SESSION, 5) == HORIZON
    assert ta.sessions_after(ENTRY_SESSION, 0) == ENTRY_SESSION
    assert ta.sessions_between(ENTRY_SESSION, HORIZON) == 5
    assert ta.sessions_between(HORIZON, ENTRY_SESSION) == 0
    assert ta.session_date(datetime(2026, 10, 30, 1, 30, tzinfo=UTC)) == date(2026, 10, 29)  # 21:30 ET the day before


def test_post_exit_path_measures_the_exit_to_the_horizon():
    bars = [_bar(d, c, h, l) for d, c, h, l in zip(SESSIONS_AFTER, [97, 99, 101, 100, 102], [98, 100, 103, 101, 102.5],
                                                     [95, 98, 100, 99, 101])]
    path = ta.post_exit_path(bars, 96.8, span_sessions=5, horizon_date=HORIZON)
    assert path["return"] == pytest.approx(102 / 96.8 - 1)
    assert path["mfe"] == pytest.approx(103 / 96.8 - 1) and path["mae"] == pytest.approx(95 / 96.8 - 1)
    assert path["bars"] == 5 and path["horizon_reached"] is True and path["sessions"] == 5
    assert path["horizon_date"] == "2026-11-05" and path["last_bar"] == "2026-11-05"
    # Bars that stop short of the horizon: measured so far, not reached.
    partial = ta.post_exit_path(bars[:3], 96.8, span_sessions=5, horizon_date=HORIZON)
    assert partial["bars"] == 3 and partial["horizon_reached"] is False
    assert partial["return"] == pytest.approx(101 / 96.8 - 1)
    # Bars past the horizon are not part of the path.
    assert ta.post_exit_path(bars + [_bar(date(2026, 11, 6), 150)], 96.8, span_sessions=5, horizon_date=HORIZON)["mfe"] \
        == pytest.approx(103 / 96.8 - 1)
    # Nothing to measure yet, or nothing to measure at all.
    assert ta.post_exit_path([], 96.8, span_sessions=5, horizon_date=HORIZON) is None
    assert ta.post_exit_path(bars, 0, span_sessions=5, horizon_date=HORIZON) is None
    at_horizon = ta.post_exit_path(bars, 96.8, span_sessions=0, horizon_date=HORIZON)
    assert at_horizon == {"return": 0.0, "mfe": 0.0, "mae": 0.0, "bars": 0, "horizon_reached": True, "sessions": 0,
                          "horizon_date": "2026-11-05", "last_bar": None}
    assert set(at_horizon) >= {"return", "mfe", "mae", "bars", "horizon_reached"}


def test_excursion_uses_highs_and_lows_or_the_close():
    path = ta.excursion([_bar(ENTRY_SESSION, 101, 103, 99), {"price_date": HORIZON, "close": 98}], 100.0)
    assert path == {"mfe": pytest.approx(0.03), "mae": pytest.approx(-0.02), "bars": 2}
    assert ta.excursion([], 100.0) is None and ta.excursion([_bar(ENTRY_SESSION, 101)], 0) is None


def test_post_exit_from_daily_bars_waits_for_the_horizon(db):  # noqa: F811
    for day, close in zip(SESSIONS_AFTER, [97, 99, 101, 100, 102]):
        _daily(db, "AAPL", day, close, high=close + 1, low=close - 1)
    db.flush()
    common = dict(entry_session=ENTRY_SESSION, exit_session=ENTRY_SESSION, max_hold=5, exit_price=96.8)
    # The horizon session is not over: nothing yet (a later revision fills it in).
    assert ta.post_exit_from_daily_bars(db, "AAPL", now=datetime(2026, 11, 5, 21, 0, tzinfo=UTC), **common) is None
    path = ta.post_exit_from_daily_bars(db, "AAPL", now=datetime(2026, 11, 6, 12, 0, tzinfo=UTC), **common)
    assert path["bars"] == 5 and path["horizon_reached"] and path["return"] == pytest.approx(102 / 96.8 - 1)
    assert path["mfe"] == pytest.approx(103 / 96.8 - 1)
    # A trade closed at its horizon has no path after the exit.
    at_rule = ta.post_exit_from_daily_bars(db, "AAPL", now=datetime(2026, 11, 6, tzinfo=UTC),
                                           **{**common, "exit_session": HORIZON})
    assert at_rule["bars"] == 0 and at_rule["horizon_reached"] and at_rule["return"] == 0.0
    # No time rule, or no bars for the symbol: nothing to measure.
    assert ta.post_exit_from_daily_bars(db, "AAPL", now=datetime(2026, 11, 6, tzinfo=UTC), **{**common, "max_hold": None}) is None
    assert ta.post_exit_from_daily_bars(db, "MSFT", now=datetime(2026, 11, 6, tzinfo=UTC), **common) is None
    hold = ta.hold_excursion_from_daily_bars(db, "AAPL", entry_session=SESSIONS_AFTER[0], exit_session=SESSIONS_AFTER[2],
                                             entry_price=100.0)
    assert hold == {"mfe": pytest.approx(0.02), "mae": pytest.approx(-0.04), "bars": 3, "source": "daily_bars"}


# ----------------------------------------------------------------- exit flags

def test_premature_stop_needs_a_recovery_to_the_entry_before_the_horizon():
    flags = ld.exit_flags
    # -3% stop: getting back to the entry needs +3.09% from the exit.
    assert flags(gross_return=-0.03, exit_cause="catastrophe_stop", post_exit={"mfe": 0.04}) == ["premature_stop"]
    assert flags(gross_return=-0.03, exit_cause="catastrophe_stop", post_exit={"mfe": 1 / 0.97 - 1}) == ["premature_stop"]
    assert flags(gross_return=-0.03, exit_cause="catastrophe_stop", post_exit={"mfe": 0.02}) == []
    assert flags(gross_return=-0.03, exit_cause="time_stop", post_exit={"mfe": 0.04}) == []
    assert flags(gross_return=-0.03, exit_cause="catastrophe_stop", post_exit=None) == []
    assert flags(gross_return=0.01, exit_cause="catastrophe_stop", post_exit={"mfe": 0.04}) == []


def test_gave_back_gains_and_held_too_long():
    flags = ld.exit_flags
    assert flags(gross_return=0.01, exit_cause="indicator", hold_mfe=0.04) == ["gave_back_gains"]
    assert flags(gross_return=-0.01, exit_cause="catastrophe_stop", hold_mfe=0.02) == ["gave_back_gains"]
    assert flags(gross_return=0.03, exit_cause="indicator", hold_mfe=0.04) == []        # kept more than half
    assert flags(gross_return=0.001, exit_cause="indicator", hold_mfe=0.005) == []      # too small to protect
    # The time stop closed a trade whose best gain had reached the 6% target.
    assert flags(gross_return=0.01, exit_cause="time_stop", hold_mfe=0.07, take_profit_pct=0.06) == \
        ["gave_back_gains", "held_too_long"]
    assert flags(gross_return=0.04, exit_cause="time_stop", hold_mfe=0.07, take_profit_pct=0.06) == ["held_too_long"]
    assert flags(gross_return=0.04, exit_cause="time_stop", hold_mfe=0.05, take_profit_pct=0.06) == []
    assert flags(gross_return=0.04, exit_cause="catastrophe_stop", hold_mfe=0.07, take_profit_pct=0.06) == []
    # A strategy without a target (RSI(2) / IBS) is judged against 3%.
    assert flags(gross_return=0.0, exit_cause="time_stop", hold_mfe=0.035, take_profit_pct=0.0) == \
        ["gave_back_gains", "held_too_long"]
    assert flags(gross_return=0.02, exit_cause="time_stop", hold_mfe=0.02, take_profit_pct=None) == []


def test_diagnose_records_the_exit_evidence_next_to_the_cause():
    diagnosis = ld.diagnose(gross_return=-0.03, is_loss=True, benchmark_return=0.002, exit_cause="catastrophe_stop",
                            post_exit={"return": 0.045, "mfe": 0.05, "mae": -0.01, "bars": 5, "horizon_reached": True},
                            hold_mfe=0.004, take_profit_pct=0.06)
    assert diagnosis["cause"] == "signal_wrong" and diagnosis["contributing"] == ["premature_stop"]
    assert diagnosis["exit_cause"] == "catastrophe_stop" and diagnosis["post_exit_return"] == 0.045
    assert diagnosis["post_exit_mfe"] == 0.05 and diagnosis["hold_mfe"] == 0.004 and diagnosis["take_profit_pct"] == 0.06
    assert diagnosis["version"] == "loss-diagnosis-v2"
    assert {"premature_stop", "gave_back_gains", "held_too_long"} <= set(ld.CONTRIBUTING_PROPOSALS)
    # Without the evidence nothing is flagged and the old shape still holds.
    plain = ld.diagnose(gross_return=-0.03, is_loss=True, benchmark_return=0.002)
    assert plain["contributing"] == [] and plain["post_exit_return"] is None


def test_repeating_failures_carry_the_exit_flags_into_the_lesson():
    rows = [{"strategy_type": "channel_breakout", "symbol": "AAPL", "closed_at": f"2026-11-0{i}T20:00:00+00:00",
             "diagnosis": {"cause": "signal_wrong", "contributing": ["premature_stop"]}} for i in range(1, 5)]
    lesson = ld.repeating_failures(rows)[0]
    assert lesson["cause"] == "signal_wrong" and lesson["contributing"] == {"premature_stop": 4}


# ------------------------------------------------------------ attribution

def test_round_trip_attribution_fills_post_exit_on_a_later_revision(db):  # noqa: F811
    account, *_ = _book(db)
    first = ta.attribute_round_trips(db, account_id=account.id, earnings_lookup=None,
                                     now=datetime(2026, 10, 30, 12, tzinfo=UTC))[0]
    assert first.revision == 1 and first.detail["post_exit"] is None and first.detail["excursion"] is None
    assert first.detail["holding"]["rule_sessions"] == 5
    assert "premature_stop" not in first.detail["diagnosis"]["contributing"]
    # Same evidence again: no new revision.
    assert ta.attribute_round_trips(db, account_id=account.id, earnings_lookup=None,
                                    now=datetime(2026, 10, 30, 13, tzinfo=UTC)) == []
    # The daily bars arrive: the stop was followed by a recovery above the entry before the horizon.
    _daily(db, "AAPL", ENTRY_SESSION, 97, high=101, low=96)
    for day, close in zip(SESSIONS_AFTER, [97, 99, 101, 100, 102]):
        _daily(db, "AAPL", day, close, high=close + 1, low=close - 1)
    db.flush()
    later = ta.attribute_round_trips(db, account_id=account.id, earnings_lookup=None,
                                     now=datetime(2026, 11, 7, 12, tzinfo=UTC))[0]
    assert later.revision == 2 and later.source_key == first.source_key
    post_exit = later.detail["post_exit"]
    assert post_exit["bars"] == 5 and post_exit["horizon_reached"] is True
    assert post_exit["return"] == pytest.approx(102 / 96.8 - 1) and post_exit["mfe"] == pytest.approx(103 / 96.8 - 1)
    assert later.detail["excursion"] == {"mfe": pytest.approx(101 / 100.5 - 1), "mae": pytest.approx(96 / 100.5 - 1),
                                         "bars": 1, "source": "daily_bars"}
    diagnosis = later.detail["diagnosis"]
    assert "premature_stop" in diagnosis["contributing"] and diagnosis["exit_cause"] == "catastrophe_stop"
    assert "premature_stop" in later.detail["mistakes"]["all"]   # the classifier's check fires from the same path
    assert ta.attribution_row_out(later)["post_exit"] == post_exit
    assert ta.attribution_summary(ta.current_attributions(db))["diagnosis"]["contributing"]["premature_stop"] == 1


def _trial_lot(db, *, entry="100", exit_="97", horizon=5, exit_reason="stop"):
    db.add(StockPaperAccount(broker="alpaca_paper", broker_account_id="paper", cash=Decimal("9000"),
                             buying_power=Decimal("9000"), equity=Decimal("10000"), status="reconciled", raw_payload={}))
    db.flush()
    trial = StockPaperTrial(id="00000000-0000-0000-0000-0000000000ab", binding_id=1, actor="test", status="running",
                            policy={"probability_threshold": 0.55}, lineage={}, baseline_equity=Decimal("10000"),
                            peak_equity=Decimal("10000"))
    db.add(trial)
    db.flush()
    when = at(14, 1)
    decision = StockPaperTrialDecision(trial_id=trial.id, symbol="AAPL", bar_timestamp=when, decision_timestamp=when,
                                       action="buy", qualifying=True, lineage={"probability": 0.6})
    db.add(decision)
    db.flush()
    buy = StockPaperOrder(account_id=1, client_order_id="b1", symbol="AAPL", side="buy", quantity=Decimal("1"),
                          status="filled", source="test")
    db.add(buy)
    db.flush()
    lot = StockPaperTrialLot(trial_id=trial.id, symbol="AAPL", entry_decision_id=decision.id, entry_order_id=buy.id,
                             quantity=Decimal("1"), entry_session=when.date().isoformat(),
                             planned_horizon_sessions=horizon, exit_reason=exit_reason)
    db.add(lot)
    db.flush()
    sell = StockPaperOrder(account_id=1, client_order_id="s1", symbol="AAPL", side="sell", quantity=Decimal("1"),
                           status="filled", source="test", trial_lot_id=lot.id)
    db.add(sell)
    db.flush()
    db.add_all([
        StockPaperFill(account_id=1, order_id=buy.id, broker_activity_id="fb1", symbol="AAPL", side="buy",
                       quantity=Decimal("1"), price=Decimal(entry), fee=Decimal("0"), cost_known=True,
                       filled_at=when, raw_payload={}),
        StockPaperFill(account_id=1, order_id=sell.id, broker_activity_id="fs1", symbol="AAPL", side="sell",
                       quantity=Decimal("1"), price=Decimal(exit_), fee=Decimal("0"), cost_known=True,
                       filled_at=when + timedelta(hours=3), raw_payload={}),
    ])
    db.flush()
    return trial, lot


def test_trial_lots_get_the_same_post_exit_path(db):  # noqa: F811
    trial, lot = _trial_lot(db)
    pending = trial_lot_trades(db, trial, now=datetime(2026, 10, 30, tzinfo=UTC))[0]
    assert pending["post_exit"] is None and pending["post_exit_return"] is None
    assert "premature_stop" not in pending["classification"]["mistakes"]
    for day, close in zip(SESSIONS_AFTER, [97, 99, 101, 100, 102]):
        _daily(db, "AAPL", day, close, high=close + 1, low=close - 1)
    db.flush()
    trade = trial_lot_trades(db, trial, now=datetime(2026, 11, 7, tzinfo=UTC))[0]
    assert trade["post_exit"]["horizon_reached"] and trade["post_exit_return"] == pytest.approx(102 / 97 - 1)
    assert "premature_stop" in trade["classification"]["mistakes"]
    row = ta.attribute_trial(db, trial, now=datetime(2026, 11, 7, tzinfo=UTC))[0]
    assert row.detail["post_exit"] == trade["post_exit"] and row.detail["lineage"]["lot_id"] == lot.id


# ------------------------------------------------------------ experiments

def test_exit_lessons_plan_stop_width_and_time_stop_experiments():
    strategies = {"channel_breakout": object(), "rsi2_pullback": object()}
    lessons = [
        _lesson("channel_breakout", "signal_wrong", 4, {"premature_stop": 3}),
        _lesson("rsi2_pullback", "signal_wrong", 3, {"held_too_long": 2, "gave_back_gains": 3}),
    ]
    plan = engine.plan_experiments(lessons, strategies, now=ENGINE_NOW, history=[], cooldown_days=28, max_experiments=5)
    planned = {(p["strategy_type"], p["template"]): p["trigger"] for p in plan["planned"]}
    assert planned == {("channel_breakout", "stop_width"): "premature_stop", ("rsi2_pullback", "time_stop"): "held_too_long"}
    assert plan["untemplated"] == []
    stop, hold = engine.TEMPLATE_BY_NAME["stop_width"], engine.TEMPLATE_BY_NAME["time_stop"]
    assert [stop.label(c) for c in stop.candidates_for("channel_breakout")] == ["stop2", "stop3", "stop5"]
    assert [stop.label(c) for c in stop.candidates_for("ibs_mean_reversion")] == ["stop7", "stop10", "stop15"]
    assert [hold.label(c) for c in hold.candidates_for("macd_momentum")] == ["hold10", "hold20", "hold30"]
    assert [hold.label(c) for c in hold.candidates_for("rsi2_pullback")] == ["hold5", "hold10", "hold15"]
    assert stop.describe({"stop_loss_pct": 0.05}) == "5% broker-side stop (target and time stop unchanged)"
    assert hold.describe({"max_holding_sessions": 10}) == "time stop after 10 sessions (stop and target unchanged)"
    assert stop.governance_action == hold.governance_action == "exit_policy_update"


def test_replay_uses_the_setup_exits_and_matches_the_fixed_barrier_when_equal():
    assert gate_replay.barrier_for("channel_breakout", {}) == gate_replay.BRACKET
    assert gate_replay.barrier_for("rsi2_pullback", {}) == REVERSION_BARRIER
    assert gate_replay.barrier_for("channel_breakout", {"stop_loss_pct": 0.05, "max_holding_sessions": 30}) == \
        BarrierConfig(take_profit_pct=0.06, stop_loss_pct=0.05, max_holding_days=30)
    assert gate_replay.barrier_for("ibs_mean_reversion", {"stop_loss_pct": "bad", "max_holding_sessions": 0}) == REVERSION_BARRIER
    parsed = gate_replay.setup_from_parameters({"stop_loss_pct": 0.05, "take_profit_pct": 0, "max_holding_sessions": "15"})
    assert {key: parsed[key] for key in gate_replay.EXIT_KEYS} == {"stop_loss_pct": 0.05, "take_profit_pct": 0.0,
                                                                   "max_holding_sessions": 15}
    frames = {"SPY": _walk(rows=700, seed=11), "QQQ": _walk(rows=700, seed=12)}
    from app.services import meta_label_primary as primary
    replays = {s: primary.replay_signals(f) for s, f in frames.items()}
    for strategy, current in [("channel_breakout", engine.current_setup({}, "channel_breakout")),
                              ("ibs_mean_reversion", engine.current_setup({}, "ibs_mean_reversion"))]:
        fixed = gate_replay.simulate(strategy, frames, frames["SPY"], {}, replays)
        same = gate_replay.simulate(strategy, frames, frames["SPY"], current, replays)
        pd.testing.assert_frame_equal(fixed.reset_index(drop=True), same.reset_index(drop=True))
        assert len(fixed) > 0
    wider = gate_replay.simulate("channel_breakout", frames, frames["SPY"], {"stop_loss_pct": 0.05}, replays)
    assert set(wider["stop_loss_pct"]) == {0.05}
    shorter = gate_replay.simulate("channel_breakout", frames, frames["SPY"], {"max_holding_sessions": 5}, replays)
    assert shorter["holding_days"].max() <= 5


def _fake_exit_replay(monkeypatch, scores, key):
    """``scores[label] = (expectancy, drawdown, trades)`` keyed by the setup's exit label; the current value is ``baseline``."""
    template = engine.TEMPLATE_BY_NAME["stop_width" if key == "stop_loss_pct" else "time_stop"]

    def simulate(strategy, frames, spy, setup, replays, vix=None):
        return pd.DataFrame({"value": [setup[key]]})

    def window_rows(trades, frames):
        label = template.label({key: trades["value"].iloc[0]})
        label = label if label in scores else "baseline"
        return {w: _window(*scores[label]) for w in engine.WINDOWS}

    monkeypatch.setattr(gate_replay, "window_rows", window_rows)
    return simulate


def _repeat_exit_losses(db, strategy_type, flag, count=4, when=ENGINE_NOW):
    for i in range(count):
        _attribution(db, strategy_type=strategy_type, cause="signal_wrong", net_return=-0.02,
                     closed_at=when - timedelta(days=10 - i), contributing=[flag])
    _attribution(db, strategy_type=strategy_type, cause="edge", net_return=0.03, closed_at=when - timedelta(days=4))


def test_adopting_a_wider_stop_writes_the_exit_policy_and_can_be_reverted(db, monkeypatch, small_universe):  # noqa: F811
    monkeypatch.setattr(settings, "experiment_verdict_min_trades", 5)
    row = _strategy(db, "channel_breakout", {"lookback": 55})
    _repeat_exit_losses(db, "channel_breakout", "premature_stop")
    simulate = _fake_exit_replay(monkeypatch, {"baseline": (5.0, 0.20, 200), "stop5": (12.0, 0.18, 190),
                                               "stop2": (2.0, 0.15, 220)}, "stop_loss_pct")
    result = engine.run_experiment_engine(db, now=ENGINE_NOW, simulate=simulate, **small_universe)
    experiment = result["experiments"][0]
    assert experiment["template"] == "stop_width" and experiment["trigger"] == "premature_stop"
    assert experiment["adopted"] == "stop5" and experiment["applied"]
    payload = db.scalar(select(AuditLog).where(AuditLog.action == engine.ACTION_EXPERIMENT)).payload
    assert set(payload["setups"]) == {"baseline", "stop2", "stop5"}          # the current 3% is the baseline
    assert payload["baseline_setup"]["stop_loss_pct"] == 0.03 and payload["setups"]["stop5"]["take_profit_pct"] == 0.06
    db.refresh(row)
    assert row.parameters == {"lookback": 55, "stop_loss_pct": 0.05}
    policy = exit_policy_for(row)
    assert (policy.stop_loss_pct, policy.take_profit_pct, policy.max_holding_sessions) == (0.05, 0.06, 20)
    governance = db.scalars(select(AuditLog).where(AuditLog.action == "exit_policy_update")).all()
    assert len(governance) == 1 and governance[0].payload["source"] == "experiment_engine"
    assert "keep the legs they were opened with" in governance[0].message
    change = engine.adopted_changes(db)[0]
    assert change["before"] == {"stop_loss_pct": None} and change["after"] == {"stop_loss_pct": 0.05}
    assert change["key"] == "stop_loss_pct" and change["description"] == "5% broker-side stop (target and time stop unchanged)"
    ledger = engine.experiment_ledger(db)
    stop_template = next(t for t in ledger["templates"] if t["name"] == "stop_width")
    assert stop_template["kind"] == "exit" and stop_template["reversion_candidates"] == ["stop7", "stop10", "stop15"]
    # Forward evidence says the wider stop is not helping: the row goes back to the class default.
    for i in range(5):
        _attribution(db, strategy_type="channel_breakout", cause="signal_wrong", net_return=-0.05,
                     closed_at=ENGINE_NOW + timedelta(days=2 + i), opened_at=ENGINE_NOW + timedelta(days=1 + i),
                     contributing=["premature_stop"])
    verdict = engine.review_changes(db, now=ENGINE_NOW + timedelta(days=8))["reviewed"][0]
    assert verdict["verdict"] == "not_helping" and verdict["reverted"]
    db.refresh(row)
    assert row.parameters == {"lookback": 55} and exit_policy_for(row).stop_loss_pct == 0.03
    assert db.scalars(select(AuditLog).where(AuditLog.action == "exit_policy_update")).all()[-1].payload["reverted_change"] \
        == change["change_id"]


def test_reversion_time_stop_experiment_uses_its_own_candidates(db, monkeypatch, small_universe):  # noqa: F811
    row = _strategy(db, "rsi2_pullback", {"stop_loss_pct": 0.10, "take_profit_pct": 0, "max_holding_sessions": 10})
    _repeat_exit_losses(db, "rsi2_pullback", "held_too_long")
    simulate = _fake_exit_replay(monkeypatch, {"baseline": (5.0, 0.20, 200), "hold5": (9.0, 0.15, 210),
                                               "hold15": (4.0, 0.25, 180)}, "max_holding_sessions")
    result = engine.run_experiment_engine(db, now=ENGINE_NOW, simulate=simulate, **small_universe)
    experiment = result["experiments"][0]
    assert experiment["template"] == "time_stop" and experiment["adopted"] == "hold5"
    payload = db.scalar(select(AuditLog).where(AuditLog.action == engine.ACTION_EXPERIMENT)).payload
    assert set(payload["setups"]) == {"baseline", "hold5", "hold15"}
    assert payload["baseline_setup"]["max_holding_sessions"] == 10 and payload["baseline_setup"]["stop_loss_pct"] == 0.10
    db.refresh(row)
    assert row.parameters["max_holding_sessions"] == 5 and row.parameters["stop_loss_pct"] == 0.10
    assert exit_policy_for(row).max_holding_sessions == 5 and exit_policy_for(row).indicator_exit is True
    assert engine.adopted_changes(db)[0]["before"] == {"max_holding_sessions": 10}


def test_exit_experiments_never_touch_the_decision_rule():
    assert engine.RULE.startswith("Adopt a candidate only if, in every window")
    rows = {"baseline": {w: _window(5.0, 0.2) for w in engine.WINDOWS},
            "stop5": {w: _window(8.0, 0.2) for w in engine.WINDOWS}}
    assert engine.decide(rows)["adopted"] == "stop5"
