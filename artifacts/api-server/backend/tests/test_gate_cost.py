"""Gate cost: the trades each entry gate stopped, scored under the strategy's tested exits."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.core.config import settings
from app.core.security import required_role
from app.models import AuditLog, IntradayBar
from app.services import experiment_engine as engine
from app.services import gate_cost as gc
from app.services.audit import write_audit_log
from app.services.strategies.base import ExitPolicy
from app.services.strategy_execution_policy import exit_policy_for
from app.tasks.celery_app import celery_app
from tests.test_experiment_engine import _fake_replay, small_universe  # noqa: F401  (fixture)
from tests.test_loss_feedback_loop import _strategy, db  # noqa: F401  (fixture)

UTC = timezone.utc
NOW = datetime(2026, 11, 6, 22, 30, tzinfo=UTC)  # Friday after the close
BRACKET = ExitPolicy(stop_loss_pct=0.03, take_profit_pct=0.06, max_holding_sessions=20)
REVERSION = ExitPolicy(stop_loss_pct=0.10, take_profit_pct=0.0, max_holding_sessions=10, indicator_exit=True)


def _frame(closes, start="2026-08-03"):
    closes = np.asarray(closes, dtype=float)
    dates = pd.bdate_range(start, periods=len(closes)).strftime("%Y-%m-%d")
    return pd.DataFrame({"date": dates, "open": closes, "high": closes * 1.005, "low": closes * 0.995,
                         "close": closes, "volume": np.full(len(closes), 2_000_000.0)})


def _trade(session, **extra):
    return {"symbol": "AAPL", "strategy": "channel_breakout", "session": session, "gate": "market_gate", **extra}


# ------------------------------------------------------------------ scoring

def test_blocked_trade_is_scored_from_the_next_open_under_the_tested_exits():
    rising = _frame(100 + np.arange(40) * 0.5)
    scored = gc.score_trade(_trade(rising["date"][5]), rising, BRACKET)
    assert scored["status"] == "scored"
    assert scored["entry_date"] == rising["date"][6] and scored["decision_date"] == rising["date"][5]
    assert scored["barrier"] == "take_profit" and scored["win"]
    assert 0.055 < scored["net_return"] < scored["gross_return"] <= 0.061  # costs on both sides
    assert scored["exit_plan"] == {"stop_loss_pct": 0.03, "take_profit_pct": 0.06, "max_holding_sessions": 20,
                                   "indicator_exit": False}
    falling = _frame(100 - np.arange(40) * 0.5)
    lost = gc.score_trade(_trade(falling["date"][5]), falling, BRACKET)
    assert lost["status"] == "scored" and lost["barrier"] == "stop_loss" and not lost["win"]
    assert lost["net_return"] < -0.03
    flat = _frame(np.full(40, 100.0))
    timed = gc.score_trade(_trade(flat["date"][5]), flat, BRACKET)
    assert timed["barrier"] == "vertical" and timed["holding_sessions"] == 20 and timed["net_return"] < 0


def test_reversion_strategies_use_their_own_exit_plan_and_indicator_exit():
    from app.services import meta_label_primary as primary

    frame = _frame(100 + np.sin(np.arange(60) / 2.0) * 3)
    signals = primary.replay_exit_signals(frame, "rsi2_pullback")
    scored = gc.score_trade(_trade(frame["date"][20], strategy="rsi2_pullback"), frame, REVERSION, exit_signals=signals)
    assert scored["status"] == "scored"
    assert scored["exit_plan"] == {"stop_loss_pct": 0.10, "take_profit_pct": 0.0, "max_holding_sessions": 10,
                                   "indicator_exit": True}
    assert scored["barrier"] in {"indicator", "vertical", "stop_loss"} and scored["holding_sessions"] <= 10
    # The barriers come from the strategy rows' own exit policies.
    assert gc.barrier_for(exit_policy_for(_Row("channel_breakout"))).max_holding_days == 20
    reversion = gc.barrier_for(exit_policy_for(_Row("rsi2_pullback")))
    assert (reversion.stop_loss_pct, reversion.take_profit_pct, reversion.max_holding_days) == (0.10, 0.0, 10)
    # A policy leg of None falls back to the risk-rule bracket; no plan at all is unscorable.
    default = gc.barrier_for(ExitPolicy())
    assert (default.stop_loss_pct, default.take_profit_pct, default.max_holding_days) == (0.03, 0.03, 20)
    assert gc.score_trade(_trade(frame["date"][20]), frame, None)["status"] == "unscorable"


class _Row:
    def __init__(self, strategy_type, parameters=None):
        self.strategy_type, self.parameters = strategy_type, parameters or {}


def test_intraday_opening_print_replaces_the_daily_open_when_known():
    frame = _frame(100 + np.arange(40) * 0.5)
    daily = gc.score_trade(_trade(frame["date"][5]), frame, BRACKET)
    intraday = gc.score_trade(_trade(frame["date"][5]), frame, BRACKET, entry_open=101.0)
    assert daily["entry_source"] == "daily_open" and intraday["entry_source"] == "intraday_open"
    assert intraday["entry_price"] < daily["entry_price"] and intraday["net_return"] > daily["net_return"]


def test_open_trades_are_split_from_scored_ones():
    frame = _frame(np.full(40, 100.0))
    still_open = gc.score_trade(_trade(frame["date"][30]), frame, BRACKET)
    assert still_open["status"] == "open" and still_open["sessions_elapsed"] == 8
    assert still_open["entry_date"] == frame["date"][31]
    last = gc.score_trade(_trade(frame["date"][39]), frame, BRACKET)
    assert last["status"] == "open" and "next open" in last["why"]
    missing_bar = gc.score_trade(_trade("2026-11-30"), frame, BRACKET)
    assert missing_bar["status"] == "open" and "not stored" in missing_bar["why"]
    assert gc.score_trade(_trade("2026-01-05"), frame, BRACKET)["status"] == "unscorable"
    assert gc.score_trade(_trade(frame["date"][5]), None, BRACKET)["status"] == "unscorable"
    trades = [gc.score_trade(_trade(frame["date"][d]), frame, BRACKET) for d in (5, 10, 30, 39)]
    totals = gc.summarise(trades)
    assert (totals["blocked"], totals["scored"], totals["open"], totals["unscorable"]) == (4, 2, 2, 0)


# ------------------------------------------------------------------- dedup

def _outcome_row(db, at, outcomes):
    row = write_audit_log(db, event_type="stock_paper", action=gc.BUY_OUTCOME_ACTION, status="recorded",
                          message="BUY signal outcomes changed",
                          payload={"observed_at": at.isoformat(), "outcomes": outcomes, "paper_only": True})
    row.created_at = at.astimezone(UTC).replace(tzinfo=None)
    db.flush()
    return row


def test_gate_names_come_from_the_recorded_reason():
    assert gc.gate_for_reason("entry protection market_gate: SPY 650.0 is at or below its 20-day average; x") == "market_gate"
    assert gc.gate_for_reason("entry protection stop_cooldown: AAPL stopped out; no re-entry for 10 sessions") == "stop_cooldown"
    assert gc.gate_for_reason("VIX gate below: VIX 27.0 >= 25 blocks entry. Breakout above the channel") == "vix_gate"
    assert gc.gate_for_reason("Regime gate range: trend_up market (ADX 31.2) blocks entry. RSI oversold") == "regime_gate"
    assert gc.gate_for_reason("earnings on 2026-11-12 fall within the 10-session hold window") == "earnings_blackout"
    assert gc.gate_for_reason("CPI on 2026-11-10 at 08:30 ET; an overnight position would be held through it") == "announcement_blackout"
    assert gc.gate_for_reason("Fed decision at 14:00 ET today; no new BUYs until 15:00 ET") == "announcement_blackout"
    assert gc.gate_for_reason("one position per symbol: rsi2_pullback already holds or is buying AAPL") == "one_position_per_symbol"
    assert gc.gate_for_reason("sector group tech already has 3 open or pending positions (cap 3)") == "sector_cap"
    assert gc.gate_for_reason("something new", "blocked") == "other"
    assert gc.gate_for_reason("filled", "submitted") is None
    assert gc.gate_for_reason("ValueError: boom", "failed") is None
    assert gc.gate_for_reason(None) is None


def test_blocked_buys_dedup_to_one_per_symbol_strategy_session_and_gate(db):  # noqa: F811
    monday = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
    gate = {"symbol": "AAPL", "strategy": "channel_breakout", "status": "skipped",
            "reason": "entry protection market_gate: SPY is at or below its 20-day average"}
    _outcome_row(db, monday, [gate, {"symbol": "SPY", "strategy": "ibs_mean_reversion", "status": "submitted",
                                     "reason": None}])
    _outcome_row(db, monday + timedelta(minutes=30), [gate, {"symbol": "MSFT", "strategy": "channel_breakout",
                                                             "status": "failed", "reason": "RuntimeError: x"}])
    # Same symbol and strategy, a different gate the same session: a second trade.
    _outcome_row(db, monday + timedelta(hours=1), [gate | {"reason": "earnings on 2026-11-05 fall within the 10-session hold window"}])
    # A session later: counted again.
    _outcome_row(db, monday + timedelta(days=1), [gate])
    # An audit row created after the session (no observed_at) dates from its created_at.
    row = write_audit_log(db, event_type="stock_paper", action=gc.BUY_OUTCOME_ACTION, status="recorded",
                          message="x", payload={"outcomes": [gate | {"symbol": "nvda"}]})
    row.created_at = datetime(2026, 11, 4, 16, 0)
    db.flush()
    trades = gc.blocked_trades(gc._outcome_rows(db, monday - timedelta(days=1), NOW))
    assert [(t["symbol"], t["session"], t["gate"]) for t in trades] == [
        ("AAPL", "2026-11-02", "market_gate"), ("AAPL", "2026-11-02", "earnings_blackout"),
        ("AAPL", "2026-11-03", "market_gate"), ("NVDA", "2026-11-04", "market_gate")]
    assert trades[0]["reason"].startswith("entry protection market_gate")


def test_session_window_counts_nyse_sessions():
    assert gc.sessions_back(date(2026, 11, 6), 0) == date(2026, 11, 6)
    assert gc.sessions_back(date(2026, 11, 6), 5) == date(2026, 10, 30)
    assert gc.sessions_back(date(2026, 9, 8), 1) == date(2026, 9, 4)   # Labor Day skipped
    assert gc.last_completed_session(datetime(2026, 11, 6, 18, 0, tzinfo=UTC)) == date(2026, 11, 5)  # 13:00 ET
    assert gc.last_completed_session(NOW) == date(2026, 11, 6)
    assert gc.last_completed_session(datetime(2026, 11, 8, 12, 0, tzinfo=UTC)) == date(2026, 11, 6)  # Sunday


# ----------------------------------------------------------------- verdicts

def _scored(net_returns, gate="market_gate", strategy="channel_breakout"):
    return [{"symbol": "AAPL", "strategy": strategy, "session": f"2026-10-{1 + i % 28:02d}", "gate": gate,
             "status": "scored", "net_return": float(value), "win": value > 0} for i, value in enumerate(net_returns)]


def test_costly_and_earning_its_keep_need_thirty_scored_trades():
    winners = _scored([0.02] * 20 + [-0.01] * 10)
    losers = _scored([-0.02] * 20 + [0.01] * 10)
    assert gc.summarise(winners)["verdict"] == gc.COSTLY
    assert gc.summarise(losers)["verdict"] == gc.EARNING
    early = gc.summarise(winners[:29])
    assert early["verdict"] == gc.TOO_EARLY and early["evidence"] == "1 more scored trades before judging"
    assert gc.summarise(winners[:29] + [{**winners[0], "status": "open"}] * 5)["verdict"] == gc.TOO_EARLY
    assert gc.summarise(winners[:10], min_scored=10)["verdict"] == gc.COSTLY
    summary = gc.summarise(winners)
    assert summary["mean_net_bps"] == 100.0 and summary["win_rate"] == pytest.approx(0.6667)
    assert summary["forfeited_profit_bps"] == 4000.0 and summary["avoided_losses_bps"] == 1000.0
    assert summary["net_bps"] == 3000.0 and summary["net_profit_forfeited"] == 3000.0
    assert gc.verdict(30, 0.0) == gc.NEUTRAL and gc.verdict(30, None) == gc.TOO_EARLY


def test_aggregate_reports_per_gate_and_per_strategy():
    trades = _scored([0.02] * 30) + _scored([-0.03] * 30, strategy="rsi2_pullback") + _scored([0.01] * 3, gate="vix_gate")
    totals = gc.aggregate(trades)
    gates = {item["gate"]: item for item in totals["gates"]}
    # Across strategies the gate avoids more than it forfeits; per strategy the picture differs.
    assert gates["market_gate"]["blocked"] == 60 and gates["market_gate"]["verdict"] == gc.EARNING
    assert gates["vix_gate"]["verdict"] == gc.TOO_EARLY
    pairs = {(item["gate"], item["strategy_type"]): item["verdict"] for item in totals["by_strategy"]}
    assert pairs == {("market_gate", "channel_breakout"): gc.COSTLY, ("market_gate", "rsi2_pullback"): gc.EARNING,
                     ("vix_gate", "channel_breakout"): gc.TOO_EARLY}


# ---------------------------------------------------------- stored report

def _seed_blocks(db, frame, count, *, gate_reason, strategy="channel_breakout", symbol="AAPL", first_bar=5):
    for i in range(count):
        session = date.fromisoformat(frame["date"][first_bar + i])
        at = datetime(session.year, session.month, session.day, 15, 0, tzinfo=UTC)
        _outcome_row(db, at, [{"symbol": symbol, "strategy": strategy, "status": "skipped", "reason": gate_reason}])


def test_report_is_recorded_once_per_session_and_feeds_the_views(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "gate_cost_min_scored", 5)
    monkeypatch.setattr(settings, "gate_cost_lookback_sessions", 60)
    _strategy(db, "channel_breakout")
    _strategy(db, "rsi2_pullback")
    rising = _frame(100 + np.arange(70) * 0.6, start="2026-08-03")   # ends 2026-11-06
    assert rising["date"].iloc[-1] == "2026-11-06"
    _seed_blocks(db, rising, 8, gate_reason="entry protection market_gate: SPY is below its 20-day average",
                 first_bar=28)
    _seed_blocks(db, rising, 3, gate_reason="earnings on 2026-11-20 fall within the 10-session hold window",
                 symbol="MSFT", first_bar=62)   # 62-64: still inside the 20-session horizon
    _seed_blocks(db, rising, 1, gate_reason="earnings on 2026-09-20 fall within the 10-session hold window",
                 strategy="rsi2_pullback", symbol="MSFT", first_bar=28)
    frames = {"AAPL": rising, "MSFT": rising}
    kwargs = {"loader": frames.get, "opens": lambda symbol, session: None}

    result = gc.record_gate_costs(db, now=NOW, **kwargs)
    assert result["recorded"] and result["as_of"] == "2026-11-06"
    assert result["blocked"] == 12 and result["scored"] == 9 and result["open"] == 3
    assert result["costly"] == ["market_gate/channel_breakout"]
    again = gc.record_gate_costs(db, now=NOW + timedelta(hours=1), **kwargs)
    assert not again["recorded"] and "already recorded" in again["reason"]
    assert db.query(AuditLog).filter(AuditLog.action == gc.ACTION).count() == 1

    latest = gc.latest_gate_costs(db)
    assert latest["window"] == {"sessions": 60, "first_session": "2026-08-14", "last_session": "2026-11-06"}
    assert latest["totals"]["scored"] == 9 and latest["totals"]["open"] == 3
    pairs = {(item["gate"], item["strategy_type"]): item for item in latest["by_strategy"]}
    market = pairs[("market_gate", "channel_breakout")]
    assert market["verdict"] == gc.COSTLY and market["win_rate"] == 1.0 and market["net_profit_forfeited"] > 0
    assert pairs[("earnings_blackout", "channel_breakout")]["open"] == 3
    assert pairs[("earnings_blackout", "rsi2_pullback")]["verdict"] == gc.TOO_EARLY
    assert len(latest["trades"]) == 12 and latest["trades"][0]["status"] == "scored"
    assert latest["trades"][-1]["exit_plan"]["indicator_exit"] is True   # RSI(2) row's own plan

    view = gc.gate_costs_view(db)
    assert view["latest"]["as_of"] == "2026-11-06" and view["min_scored"] == 5 and view["paper_only"]
    board = gc.scoreboard_gate_costs(db)
    assert board["status"] == "ok" and board["costly"][0]["gate"] == "market_gate"
    assert "9 scored, 3 still open; 1 gate/strategy pairs are costly" in board["detail"]
    assert "trades" not in board

    from app.services.learning_scoreboard import build_learning_scoreboard

    monkeypatch.setattr("app.services.stock_paper_ledger.active_paper_broker_name", lambda: "alpaca_paper")
    section = build_learning_scoreboard(db, now=NOW)["gate_costs"]
    assert section["as_of"] == "2026-11-06" and section["by_strategy"][0]["verdict"] == gc.COSTLY


def test_scoreboard_waits_before_the_first_report(db):  # noqa: F811
    section = gc.scoreboard_gate_costs(db)
    assert section["status"] == "waiting" and section["costly"] == [] and gc.gate_costs_view(db)["latest"] is None


def test_intraday_bars_supply_the_opening_print(db):  # noqa: F811
    opened = datetime(2026, 11, 5, 14, 30, tzinfo=UTC)   # 09:30 ET
    db.add(IntradayBar(symbol="AAPL", timeframe="1m", opened_at=opened + timedelta(minutes=1), open=101.25,
                       high=101.5, low=101.0, close=101.4, volume=1000, exchange_timestamp=opened))
    db.flush()
    assert gc.intraday_open(db, "AAPL", date(2026, 11, 5)) == 101.25
    assert gc.intraday_open(db, "AAPL", date(2026, 11, 4)) is None
    assert gc.intraday_open(db, "AAPL", date(2026, 11, 8)) is None   # Sunday


# -------------------------------------------------------------- engine hook

def _report(pairs):
    return {"as_of": "2026-11-06", "by_strategy": [
        {"gate": gate, "strategy_type": strategy, "verdict": verdict, "scored": scored, "blocked": scored + 2,
         "mean_net_bps": mean, "net_bps": mean * scored} for gate, strategy, verdict, scored, mean in pairs]}


def test_costly_gates_become_lessons_that_plan_the_matching_template():
    lessons = gc.costly_gate_lessons(None, report=_report([
        ("market_gate", "channel_breakout", gc.COSTLY, 40, 25.0),
        ("vix_gate", "channel_breakout", gc.COSTLY, 31, 12.0),
        ("earnings_blackout", "rsi2_pullback", gc.COSTLY, 35, 8.0),
        ("stop_cooldown", "rsi2_pullback", gc.COSTLY, 50, 30.0),       # no template: not a lesson
        ("market_gate", "rsi2_pullback", gc.EARNING, 60, -20.0),
        ("market_gate", "ensemble", gc.TOO_EARLY, 10, 40.0),
    ]))
    assert [(l["strategy_type"], l["cause"], l["count"]) for l in lessons] == [
        ("channel_breakout", "costly_market_gate", 40), ("rsi2_pullback", "costly_earnings_blackout", 35),
        ("channel_breakout", "costly_vix_gate", 31)]
    assert "forfeits more than it saves" in lessons[0]["lesson"] and lessons[0]["auto_apply"] is False
    assert [t.name for t in engine.templates_for({"costly_market_gate"})] == ["market_gate"]
    assert [t.name for t in engine.templates_for({"costly_vix_gate"})] == ["vix_gate"]
    assert [t.name for t in engine.templates_for({"costly_earnings_blackout"})] == ["earnings_blackout"]
    strategies = {"channel_breakout": object(), "rsi2_pullback": object()}
    plan = engine.plan_experiments(lessons, strategies, now=NOW, history=[], cooldown_days=28, max_experiments=4)
    assert [(p["strategy_type"], p["template"], p["trigger"]) for p in plan["planned"]] == [
        ("channel_breakout", "market_gate", "costly_market_gate"),
        ("rsi2_pullback", "earnings_blackout", "costly_earnings_blackout"),
        ("channel_breakout", "vix_gate", "costly_vix_gate")]
    assert plan["untemplated"] == []
    # "off" is a candidate of every template the costly gates plan.
    for name in ("market_gate", "vix_gate", "earnings_blackout"):
        template = engine.TEMPLATE_BY_NAME[name]
        assert template.label(template.candidates[0]) == "off"
    assert engine.RULE.startswith("Adopt a candidate only if")


def test_engine_run_turns_a_costly_gate_off_under_the_unchanged_rule(db, monkeypatch, small_universe):  # noqa: F811
    row = _strategy(db, "channel_breakout", {"market_gate_sma": 20})
    write_audit_log(db, event_type="stock_paper", action=gc.ACTION, status="recorded", message="x",
                    payload={"report": _report([("market_gate", "channel_breakout", gc.COSTLY, 40, 25.0)])})
    db.flush()
    # No repeating loss lesson at all: the costly gate is the only lesson, and "off" beats the current sma20.
    simulate = _fake_replay(monkeypatch, {"baseline": (5.0, 0.20, 200), "off": (12.0, 0.20, 260),
                                          "sma10": (4.0, 0.20, 220), "sma50": (6.0, 0.30, 100)},
                            baseline_label="sma20")
    result = engine.run_experiment_engine(db, now=NOW, simulate=simulate, **small_universe)
    assert result["status"] == "complete" and result["lessons"] == 1 and result["planned"] == 1
    assert result["gate_cost_lessons"] == [{"strategy_type": "channel_breakout", "cause": "costly_market_gate",
                                            "gate": "market_gate", "count": 40, "mean_net_bps": 25.0}]
    experiment = result["experiments"][0]
    assert experiment["template"] == "market_gate" and experiment["trigger"] == "costly_market_gate"
    assert experiment["adopted"] == "off" and experiment["applied"]
    db.refresh(row)
    assert row.parameters["market_gate_sma"] == 0   # the gate is off; its value is the engine's, not the seed's
    ledger = engine.experiment_ledger(db)
    assert ledger["experiments"][0]["decision"]["rule"] == engine.RULE
    assert ledger["changes"][0]["trigger"] == "costly_market_gate"


def test_engine_run_survives_an_unreadable_gate_cost_ledger(db, monkeypatch, small_universe):  # noqa: F811
    _strategy(db, "channel_breakout")
    monkeypatch.setattr(gc, "costly_gate_lessons", lambda _db: (_ for _ in ()).throw(RuntimeError("boom")))
    result = engine.run_experiment_engine(db, now=NOW, **small_universe)
    assert result["status"] == "complete" and result["lessons"] == 0 and result["gate_cost_lessons"] == []


# ------------------------------------------------------- recording and wiring

def test_strategy_gated_holds_are_recorded_as_withheld_buys(db):  # noqa: F811
    from app.models import StrategySignal
    from app.services.stock_paper_autotrader import BUY_OUTCOME_ACTION, _record_buy_outcomes
    from app.services.stock_paper_ledger import _stock_paper_signal_result

    strategy = _strategy(db, "channel_breakout")
    hold = StrategySignal(strategy_id=strategy.id, symbol="AAPL", signal_time=NOW, action="HOLD", probability_up=0.6,
                          probability_down=0.4, confidence=0.6, reason="VIX gate below: VIX 27.0 >= 25 blocks entry. x",
                          features={"gated_action": "BUY"})
    plain = StrategySignal(strategy_id=strategy.id, symbol="MSFT", signal_time=NOW, action="HOLD", probability_up=0.5,
                           probability_down=0.5, confidence=0.5, reason="nothing", features={})
    db.add_all([hold, plain])
    db.flush()
    gated = _stock_paper_signal_result(hold, "channel_breakout", {"close": 100}, False)
    assert gated["signal_action"] == "HOLD" and gated["gated_action"] == "BUY"
    assert gated["gate_reason"].startswith("VIX gate below")
    assert "gated_action" not in _stock_paper_signal_result(plain, "channel_breakout", {"close": 100}, False)
    _record_buy_outcomes(db, [
        {"symbol": "AAPL", "strategy": "channel_breakout", "signal_action": "HOLD", "gated_action": "BUY",
         "status": "skipped", "reason": gated["gate_reason"]},
        {"symbol": "MSFT", "strategy": "channel_breakout", "signal_action": "HOLD", "status": "observed",
         "reason": "signal is not actionable"},
    ], NOW)
    row = db.query(AuditLog).filter(AuditLog.action == BUY_OUTCOME_ACTION).one()
    assert [item["symbol"] for item in row.payload["outcomes"]] == ["AAPL"]
    assert gc.gate_for_reason(row.payload["outcomes"][0]["reason"]) == "vix_gate"


def test_signal_cycle_records_a_gated_buy_without_an_order_path(db, monkeypatch):  # noqa: F811
    """The cycle's phase 1: a gated HOLD is skipped with the gate's reason, never actionable."""
    import inspect

    from app.services import stock_paper_autotrader as autotrader

    source = inspect.getsource(autotrader._run_signal_cycle)
    assert 'signal.get("gated_action") == "BUY"' in source
    assert '"gated_action": "BUY"' in source and "signal is not actionable" in source


def test_job_route_and_beat_are_wired():
    from app.tasks import jobs

    beat = celery_app.conf.beat_schedule
    assert beat["gate-cost"]["task"] == "app.tasks.jobs.gate_cost_job"
    schedule = beat["gate-cost"]["schedule"]
    assert schedule.hour == {17} and schedule.minute == {20} and len(schedule.day_of_week) == 5
    assert beat["trade-attribution"]["schedule"].hour == set(range(9, 17))   # the last attribution run is 16:35
    assert beat["experiment-verdict"]["schedule"].minute == {10}
    assert celery_app.conf.task_routes["app.tasks.jobs.gate_cost_job"] == {"queue": "learning"}
    assert required_role("GET", "/stock-paper/gate-costs") == "viewer"
    assert callable(jobs.gate_cost_job)
    assert settings.gate_cost_lookback_sessions == 60 and settings.gate_cost_min_scored == 30 == gc.MIN_SCORED
