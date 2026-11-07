"""Setup shadow: every candidate gate value earns a forward record from the live BUY decisions."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app.core.config import settings
from app.core.security import required_role
from app.models import AuditLog
from app.services import experiment_engine as engine
from app.services import setup_shadow as shadow
from app.services.strategies import vix_gate
from app.services.strategies.base import ExitPolicy
from app.tasks.celery_app import celery_app
from tests.test_experiment_engine import _adopt, _fake_replay, small_universe  # noqa: F401  (fixture)
from tests.test_loss_feedback_loop import _strategy, db  # noqa: F401  (fixture)

UTC = timezone.utc
NOW = datetime(2026, 11, 6, 15, 0, tzinfo=UTC)          # Friday 10:00 ET, inside the session
AFTER_CLOSE = datetime(2026, 11, 6, 22, 30, tzinfo=UTC)  # Friday after the close
BRACKET = ExitPolicy(stop_loss_pct=0.03, take_profit_pct=0.06, max_holding_sessions=20)


def _frame(closes, start="2026-08-03"):
    closes = np.asarray(closes, dtype=float)
    dates = pd.bdate_range(start, periods=len(closes)).strftime("%Y-%m-%d")
    return pd.DataFrame({"date": dates, "open": closes, "high": closes * 1.005, "low": closes * 0.995,
                         "close": closes, "volume": np.full(len(closes), 2_000_000.0)})


def _context(*, spy=None, vix=27.0, earnings=None, now=NOW):
    reading = {"vix": vix, "vix_date": "2026-11-05", "vix_change_pct": 3.0} if vix is not None else {
        "vix": None, "vix_date": None, "vix_change_pct": None, "reason": "vix_unavailable"}
    return shadow.CycleContext(None, now=now, spy=spy, vix=reading,
                               earnings=earnings or (lambda symbol, sessions: symbol == "AAPL" and sessions >= 10))


def _strategies(**parameters):
    return {name: SimpleNamespace(id=i + 1, strategy_type=name, parameters=params)
            for i, (name, params) in enumerate(parameters.items())}


# ---------------------------------------------------------------- recording

def test_every_candidate_is_decided_from_the_cycle_states(monkeypatch):
    monkeypatch.setattr(settings, "paper_earnings_blackout_sessions", 10)
    falling = _frame(100 - np.arange(60) * 0.5)   # SPY below every average
    context = _context(spy=falling, vix=27.0)
    strategies = _strategies(channel_breakout={"market_gate_sma": 20, "vix_gate": "below", "vix_level": 25},
                             rsi2_pullback={"earnings_blackout_sessions": 0})
    results = [
        {"symbol": "AAPL", "strategy": "channel_breakout", "signal_action": "HOLD", "gated_action": "BUY",
         "status": "skipped", "reason": "VIX gate below: VIX 27.0 >= 25 blocks entry.", "reference_price": "101.5"},
        {"symbol": "SPY", "strategy": "rsi2_pullback", "signal_action": "BUY", "status": "submitted",
         "reference_price": "650"},
        {"symbol": "MSFT", "strategy": "channel_breakout", "signal_action": "HOLD", "status": "observed"},
        {"symbol": "MSFT", "strategy": "unknown_strategy", "signal_action": "BUY", "status": "blocked"},
    ]
    decisions = shadow.cycle_decisions(results, strategies, context=context)
    assert [(d["symbol"], d["strategy"], d["price"], d["live_status"]) for d in decisions] == [
        ("AAPL", "channel_breakout", "101.5", "skipped"), ("SPY", "rsi2_pullback", "650", "submitted")]
    aapl = decisions[0]["templates"]
    assert aapl["market_gate"] == {"live": "sma20", "live_allowed": False,
                                   "candidates": {"off": True, "sma10": False, "sma20": False, "sma50": False}}
    assert aapl["vix_gate"] == {"live": "below25", "live_allowed": False,
                                "candidates": {"off": True, "below20": False, "below25": False, "below30": True}}
    # The account-wide blackout (10 sessions) is the live value; AAPL reports inside 10 sessions.
    assert aapl["earnings_blackout"] == {"live": "blackout10", "live_allowed": False,
                                         "candidates": {"off": True, "blackout10": False, "blackout20": False}}
    spy = decisions[1]["templates"]
    assert spy["market_gate"]["live"] == "off" and spy["market_gate"]["live_allowed"] is True
    assert spy["vix_gate"]["live"] == "off" and spy["vix_gate"]["candidates"]["below30"] is True
    assert spy["earnings_blackout"] == {"live": "off", "live_allowed": True,
                                        "candidates": {"off": True, "blackout10": True, "blackout20": True}}
    # The SPY states were computed once per window from the frame the cycle loaded.
    assert set(context.summary()["market_states"]) == {"10", "20", "50"}


def test_unknown_states_fail_the_way_the_live_gates_do():
    # No SPY history: gated candidates fail closed; no VIX close: the gate fails open; no earnings cache: unknown.
    context = _context(spy=None, vix=None, earnings=lambda symbol, sessions: None)
    strategies = _strategies(channel_breakout={"market_gate_sma": 20})
    results = [{"symbol": "AAPL", "strategy": "channel_breakout", "signal_action": "BUY", "status": "skipped",
                "reason": "entry protection market_gate: SPY history is shorter than 20 bars (fail closed)"}]
    templates = shadow.cycle_decisions(results, strategies, context=context)[0]["templates"]
    assert templates["market_gate"]["candidates"] == {"off": True, "sma10": False, "sma20": False, "sma50": False}
    assert templates["vix_gate"]["candidates"] == {"off": True, "below20": True, "below25": True, "below30": True}
    assert templates["earnings_blackout"]["candidates"] == {"off": True, "blackout10": None, "blackout20": None}


def test_live_values_outside_the_candidate_set_are_labelled_and_decided():
    context = _context(spy=_frame(100 + np.arange(60) * 0.5), vix=27.0)
    strategies = _strategies(channel_breakout={"vix_gate": "not_rising", "earnings_blackout_sessions": 5,
                                               "market_gate_sma": 200})
    results = [{"symbol": "MSFT", "strategy": "channel_breakout", "signal_action": "BUY", "status": "submitted"}]
    templates = shadow.cycle_decisions(results, strategies, context=context)[0]["templates"]
    vix = templates["vix_gate"]
    assert vix["live"].startswith("not_rising") and vix["live_allowed"] is True   # +3% in 5 sessions is not a rise
    assert set(vix["candidates"]) == {"off", "below20", "below25", "below30", vix["live"]}
    earnings = templates["earnings_blackout"]
    assert earnings["live"] == "blackout5" and earnings["live_allowed"] is True and earnings["candidates"]["blackout5"] is True
    market = templates["market_gate"]
    assert market["live"] == "sma200" and market["live_allowed"] is False   # 60 bars cannot define a 200-day average
    assert market["candidates"]["sma50"] is True


def test_real_vix_and_earnings_paths_read_the_process_caches():
    from app.services import earnings_calendar

    vix_gate.set_vix_history({"2026-11-04": 22.0, "2026-11-05": 31.0})
    earnings_calendar.clear_cache()
    try:
        earnings_calendar.upcoming_earnings("AAPL", NOW, fetch=lambda symbol: [date(2026, 11, 12)])
        context = shadow.CycleContext(None, now=NOW, spy=_frame(100 + np.arange(60) * 0.5))
        assert context.session == date(2026, 11, 5) and context.vix()["vix"] == 31.0
        strategies = _strategies(channel_breakout={})
        results = [{"symbol": "AAPL", "strategy": "channel_breakout", "signal_action": "BUY", "status": "submitted"},
                   {"symbol": "NVDA", "strategy": "channel_breakout", "signal_action": "BUY", "status": "submitted"}]
        decisions = shadow.cycle_decisions(results, strategies, context=context)
        assert decisions[0]["templates"]["vix_gate"]["candidates"] == {"off": True, "below20": False, "below25": False,
                                                                       "below30": False}
        assert decisions[0]["templates"]["earnings_blackout"]["candidates"]["blackout10"] is False
        # NVDA's calendar is not cached: unknown, and no fetch was made for it.
        assert decisions[1]["templates"]["earnings_blackout"]["candidates"]["blackout10"] is None
        assert not earnings_calendar.earnings_known("NVDA")
    finally:
        vix_gate.set_vix_history(None)
        earnings_calendar.clear_cache()


def test_a_row_is_written_only_when_the_picture_or_the_session_changes(db):  # noqa: F811
    strategies = _strategies(channel_breakout={"market_gate_sma": 20})
    buy = {"symbol": "AAPL", "strategy": "channel_breakout", "signal_action": "BUY", "status": "submitted",
           "reference_price": "100"}
    rising = _frame(100 + np.arange(60) * 0.5)
    first = shadow.record_cycle_decisions(db, [buy], strategies, now=NOW, context=_context(spy=rising))
    assert first["session"] == "2026-11-05" and len(first["decisions"]) == 1 and first["paper_only"]
    # Same picture, new price: no row.  A different gate state: a row.  A new session: a row.
    assert shadow.record_cycle_decisions(db, [{**buy, "reference_price": "101"}], strategies,
                                         now=NOW + timedelta(minutes=1), context=_context(spy=rising)) is None
    falling = _frame(100 - np.arange(60) * 0.5)
    assert shadow.record_cycle_decisions(db, [buy], strategies, now=NOW + timedelta(minutes=2),
                                         context=_context(spy=falling)) is not None
    assert shadow.record_cycle_decisions(db, [buy], strategies, now=NOW + timedelta(days=3),
                                         context=_context(spy=falling, now=NOW + timedelta(days=3))) is not None
    assert shadow.record_cycle_decisions(db, [{**buy, "signal_action": "HOLD"}], strategies, now=NOW,
                                         context=_context(spy=rising)) is None
    rows = db.query(AuditLog).filter(AuditLog.action == shadow.ACTION_DECISIONS).order_by(AuditLog.id).all()
    # Monday 10:00 ET: the last completed session is Friday's.
    assert [row.payload["session"] for row in rows] == ["2026-11-05", "2026-11-05", "2026-11-06"]
    assert rows[0].payload["decisions"][0]["price"] == "100"


def test_the_cycle_records_after_the_orders_phase_and_survives_a_fake_db():
    import inspect

    from app.services import stock_paper_autotrader as autotrader

    source = inspect.getsource(autotrader._run_signal_cycle)
    assert "record_cycle_decisions(db, results, cycle_rows, now=now, protections=protections)" in source
    assert source.index("_record_buy_outcomes(db, results, now)") < source.index("record_cycle_decisions(")
    # Nothing to record: no query, no row, no error (the cycle's fake dbs have no audit table).
    assert shadow.record_cycle_decisions(object(), [], {}, now=NOW, context=_context()) is None
    from app.services.entry_protections import EntryProtections, ProtectionConfig

    assert EntryProtections(now=NOW, config=ProtectionConfig()).market_history is None


# ------------------------------------------------------------------ scoring

def _decision_row(db, at, decisions, session=None):
    from app.services.audit import write_audit_log

    session = session or shadow.gate_cost.last_completed_session(at).isoformat()
    row = write_audit_log(db, event_type=shadow.EVENT, action=shadow.ACTION_DECISIONS, status="recorded",
                          message="x", payload={"observed_at": at.isoformat(), "session": session,
                                                "decisions": decisions, "paper_only": True})
    row.created_at = at.astimezone(UTC).replace(tzinfo=None)
    db.flush()
    return row


def _templated(symbol, strategy, *, live="sma20", live_allowed=False, candidates=None, price="100"):
    candidates = candidates or {"off": True, "sma10": False, "sma20": live_allowed, "sma50": False}
    return {"symbol": symbol, "strategy": strategy, "price": price, "mode": "governed", "live_status": "skipped",
            "templates": {"market_gate": {"live": live, "live_allowed": live_allowed, "candidates": candidates}}}


def test_decisions_are_scored_once_per_symbol_strategy_session_open_or_scored(db):  # noqa: F811
    _strategy(db, "channel_breakout")
    rising = _frame(100 + np.arange(70) * 0.6, start="2026-08-03")   # ends 2026-11-06
    assert rising["date"].iloc[-1] == "2026-11-06"
    bar = lambda i: datetime.combine(date.fromisoformat(rising["date"][i]), datetime.min.time(), UTC) + timedelta(hours=15)
    # Two cycles on the same session: one decision.  One late session: still open.
    _decision_row(db, bar(30), [_templated("AAPL", "channel_breakout")], session=rising["date"][30])
    _decision_row(db, bar(30) + timedelta(minutes=5), [_templated("AAPL", "channel_breakout", price="101")],
                  session=rising["date"][30])
    _decision_row(db, bar(31), [_templated("AAPL", "channel_breakout"), _templated("MSFT", "channel_breakout")],
                  session=rising["date"][31])
    _decision_row(db, bar(66), [_templated("AAPL", "channel_breakout")], session=rising["date"][66])
    report = shadow.build_scores(db, now=AFTER_CLOSE, sessions=120, min_trades=2, loader={"AAPL": rising, "MSFT": rising}.get,
                                 policies={"channel_breakout": BRACKET}, opens=lambda symbol, session: None)
    assert report["totals"] == {"decisions": 4, "scored": 3, "open": 1, "unscorable": 0}
    statuses = [(d["symbol"], d["session"], d["status"]) for d in report["decisions"]]
    assert statuses == [("AAPL", rising["date"][30], "scored"), ("AAPL", rising["date"][31], "scored"),
                        ("MSFT", rising["date"][31], "scored"), ("AAPL", rising["date"][66], "open")]
    assert report["decisions"][0]["price"] == "100" and report["decisions"][0]["net_return"] > 0.05
    assert report["decisions"][0]["entry_date"] == rising["date"][31]
    off = next(r for r in report["records"] if r["candidate"] == "off")
    assert (off["decisions"], off["allowed"], off["trades"], off["open"]) == (4, 4, 3, 1)
    assert off["live"] == "sma20" and off["live_record"]["trades"] == 0 and off["beats_live"]
    assert off["delta_expectancy_bps"] == off["expectancy_bps"] > 500


def test_scores_are_recorded_once_per_session_and_feed_the_views(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr("app.services.stock_paper_ledger.active_paper_broker_name", lambda: "alpaca_paper")
    _strategy(db, "channel_breakout")
    rising = _frame(100 + np.arange(70) * 0.6, start="2026-08-03")
    for i in range(20, 26):
        at = datetime.combine(date.fromisoformat(rising["date"][i]), datetime.min.time(), UTC) + timedelta(hours=15)
        _decision_row(db, at, [_templated("AAPL", "channel_breakout")], session=rising["date"][i])
    kwargs = {"loader": {"AAPL": rising}.get, "policies": {"channel_breakout": BRACKET},
              "opens": lambda symbol, session: None, "min_trades": 5}
    result = shadow.record_scores(db, now=AFTER_CLOSE, **kwargs)
    assert result["recorded"] and result["as_of"] == "2026-11-06" and result["decisions"] == 6
    assert result["beating"] == ["channel_breakout/market_gate=off"]
    again = shadow.record_scores(db, now=AFTER_CLOSE + timedelta(hours=1), **kwargs)
    assert not again["recorded"] and "already recorded" in again["reason"]
    assert db.query(AuditLog).filter(AuditLog.action == shadow.ACTION_SCORE).count() == 1

    view = shadow.setup_shadow_view(db)
    assert view["as_of"] == "2026-11-06" and view["paper_only"] and view["revert_margin_bps"] == engine.REVERT_EXPECTANCY_BPS
    assert [r["candidate"] for r in view["beating"]] == ["off"]
    assert {t["name"] for t in view["templates"]} == {"market_gate", "vix_gate", "earnings_blackout", "sector_gate",
                                                      "breadth_gate"}
    assert "decisions" not in view and "live_record" not in view["records"][0]
    board = shadow.scoreboard_setup_shadow(db)
    assert board["status"] == "ok" and "6 BUY decisions in the window, 6 scored" in board["detail"]
    assert board["beating"][0]["delta_expectancy_bps"] > 0
    from app.services.learning_scoreboard import build_learning_scoreboard

    section = build_learning_scoreboard(db, now=AFTER_CLOSE)["setup_shadow"]
    assert section["as_of"] == "2026-11-06" and section["beating"][0]["candidate"] == "off"
    record = shadow.shadow_track_record(db, "channel_breakout", "market_gate", min_trades=5)
    assert record["live"] == "sma20" and record["beating"] == ["off"] and record["decisions"] == 6


def test_scoreboard_and_view_wait_before_the_first_scores(db):  # noqa: F811
    assert shadow.scoreboard_setup_shadow(db)["status"] == "waiting"
    view = shadow.setup_shadow_view(db)
    assert view["as_of"] is None and view["records"] == [] and view["min_trades"] == settings.experiment_verdict_min_trades


# ------------------------------------------------------------- track record

def _scored(symbol, session, net, *, candidates, live="sma20", live_allowed=None, status="scored"):
    live_allowed = candidates[live] if live_allowed is None else live_allowed
    return {"symbol": symbol, "strategy": "channel_breakout", "session": session, "status": status, "net_return": net,
            "templates": {"market_gate": {"live": live, "live_allowed": live_allowed, "candidates": candidates}}}


def test_track_record_counts_each_candidates_trades_and_the_delta_on_the_overlap():
    both = {"off": True, "sma10": True, "sma20": True, "sma50": False}
    only_off = {"off": True, "sma10": False, "sma20": False, "sma50": False}
    decisions = [
        _scored("AAPL", "2026-10-01", 0.02, candidates=both),      # both take it: +200
        _scored("MSFT", "2026-10-02", -0.01, candidates=both),     # both: -100
        _scored("NVDA", "2026-10-05", 0.03, candidates=only_off),  # off alone: +300
        _scored("AMD", "2026-10-06", -0.04, candidates=only_off),  # off alone: -400
        _scored("TSLA", "2026-10-07", 0.05, candidates=only_off, status="open"),
        _scored("QQQ", "2026-10-08", 0.01, candidates={"off": True, "sma10": None, "sma20": True, "sma50": False}),
        _scored("IWM", "2026-09-01", 0.09, candidates=both, live="off"),   # before the live value changed: excluded
    ]
    records = {r["candidate"]: r for r in shadow.track_records(decisions, min_trades=3)}
    off, sma20, sma50, sma10 = records["off"], records["sma20"], records["sma50"], records["sma10"]
    assert (off["decisions"], off["allowed"], off["trades"], off["open"], off["differing"]) == (6, 6, 5, 1, 2)
    assert off["expectancy_bps"] == pytest.approx(20.0) and off["win_rate"] == 0.6 and off["total_profit_bps"] == 100.0
    assert off["live_record"] == {"trades": 3, "expectancy_bps": pytest.approx(66.7), "win_rate": pytest.approx(0.6667),
                                  "total_profit_bps": 200.0}
    assert off["overlap"] == 5 and off["delta_total_bps"] == -100.0 and off["delta_expectancy_bps"] == pytest.approx(-46.7)
    assert not off["beats_live"] and off["evidence"] == "enough overlapping decisions to compare"
    assert sma20["is_live"] and sma20["delta_expectancy_bps"] == 0.0 and not sma20["beats_live"]
    assert (sma50["trades"], sma50["overlap"], sma50["delta_total_bps"]) == (0, 3, -200.0)
    assert sma50["evidence"] == "3 more scored decisions before comparing"
    assert sma10["unknown"] == 1 and sma10["decisions"] == 5
    # The live value's own record always sorts last among its template's candidates.
    ordered = [r["candidate"] for r in shadow.track_records(decisions, min_trades=3)]
    assert ordered[-1] == "sma20" and ordered[0] == "sma10"


def test_beats_live_needs_the_margin_the_overlap_and_more_total_profit():
    winners = [_scored(f"S{i}", f"2026-10-{i + 1:02d}", 0.02, candidates={"off": True, "sma20": False})
               for i in range(4)]
    off = {r["candidate"]: r for r in shadow.track_records(winners, min_trades=4)}["off"]
    assert off["beats_live"] and off["delta_expectancy_bps"] == 200.0 and off["live_record"]["trades"] == 0
    assert not {r["candidate"]: r for r in shadow.track_records(winners, min_trades=5)}["off"]["beats_live"]
    assert not {r["candidate"]: r for r in shadow.track_records(winners, min_trades=4, margin_bps=250)}["off"]["beats_live"]
    # Same expectancy on fewer trades: not more total profit, so a stricter gate never "beats" by sample size.
    equal = [_scored(f"S{i}", f"2026-10-{i + 1:02d}", 0.02, candidates={"off": True, "sma20": i < 4}, live="off")
             for i in range(6)]
    sma20 = {r["candidate"]: r for r in shadow.track_records(equal, min_trades=4)}["sma20"]
    assert sma20["delta_expectancy_bps"] == 0.0 and sma20["delta_total_bps"] == -400.0 and not sma20["beats_live"]


# --------------------------------------------------------------- the engine

def _report(decisions, as_of="2026-11-06"):
    return {"as_of": as_of, "decisions": decisions, "records": shadow.track_records(decisions, min_trades=4),
            "totals": {"decisions": len(decisions)}, "window": {}, "min_trades": 4}


def test_shadow_lessons_name_the_template_and_plan_exactly_it(db, monkeypatch):  # noqa: F811
    decisions = [_scored(f"S{i}", f"2026-10-{i + 1:02d}", 0.02, candidates={"off": True, "sma10": True, "sma20": False})
                 for i in range(5)]
    monkeypatch.setattr(shadow, "latest_scores", lambda db: _report(decisions))
    lessons = shadow.shadow_lessons(db, min_trades=4)
    assert len(lessons) == 1
    lesson = lessons[0]
    assert (lesson["strategy_type"], lesson["cause"], lesson["template"], lesson["live"]) == (
        "channel_breakout", "shadow_evidence", "market_gate", "sma20")
    assert lesson["candidate"] in {"off", "sma10"} and lesson["count"] == 5 and "In shadow" in lesson["lesson"]
    assert shadow.shadow_lessons(db, min_trades=6) == []
    plan = engine.plan_experiments(lessons, {"channel_breakout": object(), "rsi2_pullback": object()}, now=NOW,
                                   history=[], cooldown_days=28, max_experiments=4)
    assert [(p["strategy_type"], p["template"], p["trigger"], p["lesson_count"]) for p in plan["planned"]] == [
        ("channel_breakout", "market_gate", "shadow_evidence", 5)]
    assert plan["planned"][0]["shadow_candidate"] == lesson["candidate"]
    assert plan["untemplated"] == [] and plan["skipped"] == []


def test_engine_run_replays_shadow_evidence_under_the_unchanged_rule(db, monkeypatch, small_universe):  # noqa: F811
    row = _strategy(db, "channel_breakout", {"market_gate_sma": 20})
    decisions = [_scored(f"S{i}", f"2026-10-{i + 1:02d}", 0.02, candidates={"off": True, "sma20": False})
                 for i in range(25)]
    monkeypatch.setattr(shadow, "latest_scores", lambda db: _report(decisions))
    # The replay rejects "off": the shadow lesson plans the experiment but the rule still decides.
    simulate = _fake_replay(monkeypatch, {"baseline": (5.0, 0.20, 200), "off": (4.0, 0.25, 220),
                                          "sma10": (4.0, 0.20, 190), "sma50": (3.0, 0.20, 100)}, baseline_label="sma20")
    result = engine.run_experiment_engine(db, now=NOW, simulate=simulate, **small_universe)
    assert result["planned"] == 1 and result["adopted"] == []
    assert result["shadow_lessons"][0]["candidate"] == "off" and result["shadow_lessons"][0]["count"] == 25
    experiment = result["experiments"][0]
    assert experiment["trigger"] == "shadow_evidence" and experiment["adopted"] is None
    assert engine.RULE == engine.decide({"baseline": {}, "x": {}})["rule"]   # the rule text is untouched
    db.refresh(row)
    assert row.parameters["market_gate_sma"] == 20


def test_engine_run_survives_an_unreadable_shadow_ledger(db, monkeypatch, small_universe):  # noqa: F811
    def boom(db, **kwargs):
        raise RuntimeError("ledger unreadable")

    monkeypatch.setattr(shadow, "shadow_lessons", boom)
    result = engine.run_experiment_engine(db, now=NOW, **small_universe)
    assert result["status"] == "complete" and result["shadow_lessons"] == []


def test_verdict_carries_the_shadow_record_and_reverts_on_shadow_evidence(db, monkeypatch, small_universe):  # noqa: F811
    from tests.test_experiment_engine import NOW as ENGINE_NOW, _attribution

    monkeypatch.setattr(settings, "experiment_verdict_min_trades", 5)
    row = _strategy(db, "channel_breakout")
    change = _adopt(db, row, monkeypatch, small_universe)
    assert change["template"] == "market_gate" and row.parameters["market_gate_sma"] == 20
    adopted_session = shadow.gate_cost.last_completed_session(ENGINE_NOW).isoformat()
    # One live trade so far: too few for the live verdict.
    _attribution(db, strategy_type="channel_breakout", cause="edge", net_return=0.01,
                 closed_at=ENGINE_NOW + timedelta(days=3), opened_at=ENGINE_NOW + timedelta(days=2))
    # Shadow: five decisions since the change where "off" would have taken +2% winners that sma20 blocked,
    # plus one on the adoption session itself (not counted) and one before it.
    since = [_scored(f"S{i}", (date.fromisoformat(adopted_session) + timedelta(days=i + 1)).isoformat(), 0.02,
                     candidates={"off": True, "sma20": False}) for i in range(5)]
    stale = [_scored("OLD", adopted_session, 0.5, candidates={"off": True, "sma20": False}),
             _scored("OLDER", "2026-10-01", 0.5, candidates={"off": True, "sma20": False})]
    monkeypatch.setattr(shadow, "latest_scores", lambda db: _report(since[:4] + stale))
    review = engine.review_changes(db, now=ENGINE_NOW + timedelta(days=8))
    assert review["reviewed"] == [] and review["pending"][0]["needed"] == 4
    pending_shadow = review["pending"][0]["shadow"]
    assert pending_shadow["decisions"] == 4 and pending_shadow["beating"] == []
    off = next(c for c in pending_shadow["candidates"] if c["candidate"] == "off")
    assert off["overlap"] == 4 and off["delta_expectancy_bps"] == 200.0 and not off["beats_live"]

    monkeypatch.setattr(shadow, "latest_scores", lambda db: _report(since + stale))
    review = engine.review_changes(db, now=ENGINE_NOW + timedelta(days=9))
    verdict = review["reviewed"][0]
    assert verdict["verdict"] == "not_helping" and verdict["state"] == "reverted" and verdict["reverted"]
    assert verdict["shadow_verdict"] and verdict["beaten_by"]["candidate"] == "off"
    assert verdict["beaten_by"]["overlap"] == 5 and verdict["beaten_by"]["delta_expectancy_bps"] == 200.0
    assert verdict["after"]["trades"] == 1 and verdict["shadow"]["beating"] == ["off"]
    db.refresh(row)
    assert "market_gate_sma" not in row.parameters or not row.parameters["market_gate_sma"]
    audit = db.query(AuditLog).filter(AuditLog.action == engine.ACTION_VERDICT).one()
    assert "on shadow evidence" in audit.message and audit.payload["shadow_verdict"]
    assert engine.adopted_changes(db)[0]["state"] == "reverted"


def test_verdict_without_a_shadow_ledger_judges_as_before(db, monkeypatch, small_universe):  # noqa: F811
    from tests.test_experiment_engine import NOW as ENGINE_NOW, _attribution

    monkeypatch.setattr(settings, "experiment_verdict_min_trades", 5)
    row = _strategy(db, "channel_breakout")
    _adopt(db, row, monkeypatch, small_universe)
    for i in range(5):
        _attribution(db, strategy_type="channel_breakout", cause="edge", net_return=0.02,
                     closed_at=ENGINE_NOW + timedelta(days=2 + i), opened_at=ENGINE_NOW + timedelta(days=1 + i))
    review = engine.review_changes(db, now=ENGINE_NOW + timedelta(days=9))
    verdict = review["reviewed"][0]
    assert verdict["verdict"] == "working" and not verdict["shadow_verdict"] and verdict["beaten_by"] is None
    assert verdict["shadow"]["decisions"] == 0 and verdict["shadow"]["candidates"] == []


def test_shadow_beats_adopted_picks_the_largest_gain():
    shadow_record = {"candidates": [
        {"candidate": "sma20", "is_live": True, "beats_live": False},
        {"candidate": "off", "is_live": False, "beats_live": True, "delta_expectancy_bps": 12.0},
        {"candidate": "sma50", "is_live": False, "beats_live": True, "delta_expectancy_bps": 30.0},
        {"candidate": "sma10", "is_live": False, "beats_live": False, "delta_expectancy_bps": 40.0}]}
    assert engine.shadow_beats_adopted(shadow_record)["candidate"] == "sma50"
    assert engine.shadow_beats_adopted(None) is None and engine.shadow_beats_adopted({"candidates": []}) is None


# ------------------------------------------------------------------ wiring

def test_job_route_queue_and_beat_are_wired():
    from app.tasks import jobs

    beat = celery_app.conf.beat_schedule
    assert beat["setup-shadow"]["task"] == "app.tasks.jobs.setup_shadow_job"
    schedule = beat["setup-shadow"]["schedule"]
    assert schedule.hour == {17} and schedule.minute == {25} and len(schedule.day_of_week) == 5
    assert beat["daily-market-data-import"]["schedule"].minute == {15}   # the session's bars land at 17:15
    assert celery_app.conf.task_routes["app.tasks.jobs.setup_shadow_job"] == {"queue": "learning_fast"}
    assert required_role("GET", "/stock-paper/setup-shadow") == "viewer"
    assert callable(jobs.setup_shadow_job)
    assert settings.setup_shadow_lookback_sessions == 120
