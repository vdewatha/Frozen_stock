"""Experiment engine: lessons -> pre-registered experiments -> governed changes -> forward verdicts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.security import required_role
from app.models import AuditLog, Strategy
from app.models.loss_feedback import StockTradeAttribution
from app.services import experiment_engine as engine
from app.services import gate_replay
from app.services import seed
from app.services.earnings_calendar import earnings_blackout, strategy_blackout_sessions
from app.services.earnings_history import SURPRISE_COLUMN
from app.tasks.celery_app import celery_app
from tests.test_loss_feedback_loop import _strategy, db  # noqa: F401  (fixture)
from tests.test_meta_label import _walk

UTC = timezone.utc
NOW = datetime(2026, 11, 8, 17, 0, tzinfo=UTC)  # Sunday
_counter = {"n": 0}


def _attribution(db, *, strategy_type, cause, net_return, closed_at, contributing=(), opened_at=None, symbol="AAPL"):
    _counter["n"] += 1
    is_loss = net_return <= 0
    detail = {"diagnosis": {"cause": cause, "family": "win" if not is_loss else "missed",
                            "contributing": list(contributing)}}
    row = StockTradeAttribution(
        source="scheduled_round_trip", source_key=f"round_trip:{_counter['n']}", revision=1,
        attribution_sha256="0" * 64, strategy_type=strategy_type, symbol=symbol,
        opened_at=opened_at or closed_at - timedelta(days=3), closed_at=closed_at, exit_cause="catastrophe_stop",
        net_return=Decimal(str(net_return)), gross_return=Decimal(str(net_return)),
        net_pnl=Decimal(str(net_return * 1000)), costs_known=True, is_loss=is_loss, detail=detail, paper_only=True)
    db.add(row)
    db.flush()
    return row


def _window(exp, dd, n=100):
    return {"trades": n, "expectancy_bps": exp, "total_profit_bps": n * exp, "portfolio_max_drawdown": dd}


def _rows(**labels):
    return {label: {w: _window(*spec) for w in engine.WINDOWS} for label, spec in labels.items()}


# ------------------------------------------------------------ decision rule

def test_decision_rule_is_the_pre_registered_one():
    rows = _rows(baseline=(10.0, 0.10), sma20=(13.0, 0.105), sma50=(20.0, 0.12))
    rows["sma10"] = {"all": _window(12.0, 0.09), "2015+": _window(9.0, 0.09), "2020+": _window(15.0, 0.09)}
    decision = engine.decide(rows)
    assert decision["adopted"] == "sma20"
    assert decision["eligible"] == {"sma20": pytest.approx(3.0)}
    assert "2015+: expectancy" in decision["failed"]["sma10"]
    assert "drawdown" in decision["failed"]["sma50"]
    assert decision["rule"] == engine.RULE


def test_decision_rule_counts_total_profit_and_sample_size():
    # Higher expectancy on far fewer trades earns less in total: rejected under the profit goal.
    rows = _rows(baseline=(10.0, 0.10, 100), below20=(15.0, 0.08, 50))
    assert engine.decide(rows)["adopted"] is None
    assert "total profit" in engine.decide(rows)["failed"]["below20"]
    # Too few trades in a window is never evidence.
    rows = _rows(baseline=(10.0, 0.10, 100), below20=(15.0, 0.08, 20))
    assert "only 20 trades" in engine.decide(rows)["failed"]["below20"]
    # Nothing beats the current setup: no change.
    rows = _rows(baseline=(10.0, 0.10), off=(9.0, 0.10))
    assert engine.decide(rows)["adopted"] is None


def test_templates_cover_the_diagnosed_causes():
    assert [t.name for t in engine.templates_for({"missed_market_move"})] == ["market_gate"]
    assert [t.name for t in engine.templates_for({"high_vix_entry"})] == ["vix_gate"]
    assert [t.name for t in engine.templates_for({"missed_earnings"})] == ["earnings_blackout"]
    assert engine.templates_for({"signal_wrong", "news_wrong", "clustered_stops"}) == []
    # Turning a gate off is always one of the candidates.
    for template in engine.TEMPLATES:
        assert template.label(template.candidates[0]) == "off"


def test_current_setup_falls_back_to_the_account_blackout(monkeypatch):
    monkeypatch.setattr(settings, "paper_earnings_blackout_sessions", 7)
    assert engine.current_setup({})["earnings_blackout_sessions"] == 7
    assert engine.current_setup({"earnings_blackout_sessions": 0})["earnings_blackout_sessions"] == 0
    assert engine.current_setup({"market_gate_sma": "20", "vix_gate": "below", "vix_level": 25}) == {
        "market_gate_sma": 20, "vix_gate": "below", "vix_level": 25.0, "earnings_blackout_sessions": 7}


# ------------------------------------------------------------------ planning

def _lesson(strategy, cause, count, contributing=None):
    return {"strategy_type": strategy, "cause": cause, "count": count, "contributing": contributing or {},
            "lesson": "x", "proposal": "test it"}


def test_plan_maps_lessons_to_experiments_dedups_and_budgets():
    strategies = {"channel_breakout": object(), "rsi2_pullback": object(), "model_predictive_long": object()}
    lessons = [
        _lesson("all", "missed_market_move", 9),
        _lesson("channel_breakout", "missed_market_move", 4, {"high_vix_entry": 3}),
        _lesson("rsi2_pullback", "signal_wrong", 5),
        _lesson("ensemble", "missed_earnings", 3),
    ]
    history = [{"strategy_type": "rsi2_pullback", "template": "market_gate", "lesson_count": 8,
                "ran_at": (NOW - timedelta(days=7)).isoformat()}]
    plan = engine.plan_experiments(lessons, strategies, now=NOW, history=history, cooldown_days=28, max_experiments=1)
    planned = [(p["strategy_type"], p["template"], p["lesson_count"]) for p in plan["planned"]]
    deferred = [(p["strategy_type"], p["template"]) for p in plan["deferred"]]
    # The account-wide lesson expands to every active replayable strategy; the larger count wins the pair.
    assert planned == [("channel_breakout", "market_gate", 9)]
    assert deferred == [("channel_breakout", "vix_gate")]
    reasons = {(s["strategy_type"], s["template"]): s["reason"] for s in plan["skipped"]}
    assert "tested" in reasons[("rsi2_pullback", "market_gate")]          # within the cooldown, lesson grew by 1
    assert "replayed" in reasons[("model_predictive_long", "market_gate")]
    assert "not an active" in reasons[("ensemble", "earnings_blackout")]
    assert [u["cause"] for u in plan["untemplated"]] == ["signal_wrong"]
    # A lesson that grew by 3 since the last run is tested again inside the cooldown.
    lessons[0]["count"] = 11
    plan = engine.plan_experiments(lessons, strategies, now=NOW, history=history, cooldown_days=28, max_experiments=5)
    assert ("rsi2_pullback", "market_gate") in {(p["strategy_type"], p["template"]) for p in plan["planned"]}


# ------------------------------------------------------------- engine runs

@pytest.fixture()
def small_universe(monkeypatch):
    monkeypatch.setattr(settings, "paper_execution_symbols", ["SPY", "AAPL"])
    monkeypatch.setattr(settings, "experiment_engine_max_experiments", 4)
    frames = {"SPY": _walk(rows=400, seed=3, start="2024-01-02"), "AAPL": _walk(rows=400, seed=4, start="2024-01-02")}
    return {"loader": lambda symbol: frames[symbol], "earnings_loader": lambda symbol: pd.DataFrame(),
            "vix_loader": lambda start: pd.DataFrame(columns=["date", "close"])}


def _fake_replay(monkeypatch, scores, baseline_label="off"):
    """``scores[label] = (expectancy, drawdown, trades)``: the replay's answer per market-gate setup.

    ``baseline_label`` is the strategy's current gate, which the engine replays as ``baseline``.
    """
    def simulate(strategy, frames, spy, setup, replays, vix=None):
        return pd.DataFrame({"gate": [int(setup.get("market_gate_sma") or 0)]})

    def window_rows(trades, frames):
        gate = int(trades["gate"].iloc[0])
        label = "off" if not gate else f"sma{gate}"
        if label == baseline_label:
            label = "baseline"
        return {w: _window(*scores.get(label, scores["baseline"])) for w in engine.WINDOWS}

    monkeypatch.setattr(gate_replay, "window_rows", window_rows)
    return simulate


def _repeat_market_losses(db, strategy_type, count=4):
    for i in range(count):
        _attribution(db, strategy_type=strategy_type, cause="missed_market_move", net_return=-0.02,
                     closed_at=NOW - timedelta(days=10 - i), contributing=["market_down"])
    _attribution(db, strategy_type=strategy_type, cause="edge", net_return=0.03, closed_at=NOW - timedelta(days=4))


def test_run_adopts_the_gate_that_passes_and_keeps_a_ledger(db, monkeypatch, small_universe):  # noqa: F811
    row = _strategy(db, "channel_breakout")
    _repeat_market_losses(db, "channel_breakout")
    simulate = _fake_replay(monkeypatch, {"baseline": (5.0, 0.20, 200), "sma20": (12.0, 0.15, 150),
                                          "sma50": (14.0, 0.25, 120)})
    result = engine.run_experiment_engine(db, now=NOW, simulate=simulate, **small_universe)
    assert result["status"] == "complete" and result["planned"] == 1
    assert result["experiments"][0]["adopted"] == "sma20" and result["experiments"][0]["applied"]
    db.refresh(row)
    assert row.parameters["market_gate_sma"] == 20
    governance = db.scalars(select(AuditLog).where(AuditLog.action == "market_gate_update")).all()
    assert len(governance) == 1 and governance[0].payload["source"] == "experiment_engine"
    # The one-time seed step respects the bot's decision.
    assert seed.enable_market_gates(db) == []
    ledger = engine.experiment_ledger(db)
    change = ledger["changes"][0]
    assert change["state"] == "watching" and change["before"] == {"market_gate_sma": None}
    assert change["after"] == {"market_gate_sma": 20} and change["trigger"] == "missed_market_move"
    assert change["recent_before"]["trades"] == 5 and change["recent_before"]["cause_share"] == 1.0
    assert change["expected"]["adopted"]["all"]["expectancy_bps"] == 12.0
    assert ledger["experiments"][0]["decision"]["adopted"] == "sma20"
    assert ledger["latest_run"]["adopted"] == [change["change_id"]]
    # Same evidence a day later: the pair is inside its cooldown, nothing is re-run.
    again = engine.run_experiment_engine(db, now=NOW + timedelta(days=1), simulate=simulate, **small_universe)
    assert again["planned"] == 0 and "tested" in again["skipped"][0]["reason"]


def test_run_without_adoption_only_records(db, monkeypatch, small_universe):  # noqa: F811
    monkeypatch.setattr(settings, "experiment_engine_adoption_enabled", False)
    row = _strategy(db, "channel_breakout")
    _repeat_market_losses(db, "channel_breakout")
    simulate = _fake_replay(monkeypatch, {"baseline": (5.0, 0.20, 200), "sma20": (12.0, 0.15, 150)})
    result = engine.run_experiment_engine(db, now=NOW, simulate=simulate, **small_universe)
    assert result["experiments"][0]["adopted"] == "sma20" and not result["experiments"][0]["applied"]
    db.refresh(row)
    assert "market_gate_sma" not in row.parameters
    assert engine.experiment_ledger(db)["changes"] == []
    assert db.scalar(select(AuditLog).where(AuditLog.action == engine.ACTION_EXPERIMENT)).status == "eligible"


def test_run_keeps_the_setup_when_nothing_beats_it(db, monkeypatch, small_universe):  # noqa: F811
    row = _strategy(db, "channel_breakout", {"market_gate_sma": 20})
    _repeat_market_losses(db, "channel_breakout")
    simulate = _fake_replay(monkeypatch, {"baseline": (12.0, 0.15, 150), "sma50": (14.0, 0.14, 100),
                                          "sma10": (11.0, 0.15, 160), "off": (5.0, 0.20, 200)}, baseline_label="sma20")
    result = engine.run_experiment_engine(db, now=NOW, simulate=simulate, **small_universe)
    experiment = result["experiments"][0]
    assert experiment["adopted"] is None
    db.refresh(row)
    assert row.parameters == {"market_gate_sma": 20}
    payload = db.scalar(select(AuditLog).where(AuditLog.action == engine.ACTION_EXPERIMENT)).payload
    assert set(payload["setups"]) == {"baseline", "off", "sma10", "sma50"}   # the current value is the baseline
    assert "total profit" in payload["decision"]["failed"]["sma50"]


def test_lessons_without_a_template_are_reported_not_acted_on(db, monkeypatch, small_universe):  # noqa: F811
    _strategy(db, "rsi2_pullback")
    for i in range(4):
        _attribution(db, strategy_type="rsi2_pullback", cause="signal_wrong", net_return=-0.02,
                     closed_at=NOW - timedelta(days=10 - i))
    result = engine.run_experiment_engine(db, now=NOW, simulate=_fake_replay(monkeypatch, {"baseline": (1, 0.1)}),
                                          **small_universe)
    assert result["planned"] == 0
    assert result["untemplated"][0]["cause"] == "signal_wrong" and result["untemplated"][0]["proposal"]


def test_engine_can_be_disabled(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "experiment_engine_enabled", False)
    assert engine.run_experiment_engine(db, now=NOW)["status"] == "disabled"


# ------------------------------------------------------------------ verdicts

def _adopt(db, row, monkeypatch, small_universe, before_trades=5):
    _repeat_market_losses(db, row.strategy_type, count=before_trades - 1)
    simulate = _fake_replay(monkeypatch, {"baseline": (5.0, 0.20, 200), "sma20": (12.0, 0.15, 150)})
    engine.run_experiment_engine(db, now=NOW, simulate=simulate, **small_universe)
    return engine.adopted_changes(db)[0]


def test_verdict_waits_for_enough_trades_then_confirms(db, monkeypatch, small_universe):  # noqa: F811
    monkeypatch.setattr(settings, "experiment_verdict_min_trades", 6)
    row = _strategy(db, "channel_breakout")
    change = _adopt(db, row, monkeypatch, small_universe)
    assert change["watch"]["min_trades"] == 6
    for i in range(3):
        _attribution(db, strategy_type="channel_breakout", cause="edge", net_return=0.02,
                     closed_at=NOW + timedelta(days=2 + i), opened_at=NOW + timedelta(days=1 + i))
    review = engine.review_changes(db, now=NOW + timedelta(days=6))
    assert review["reviewed"] == [] and review["pending"][0]["needed"] == 3
    for i in range(3):
        _attribution(db, strategy_type="channel_breakout", cause="signal_wrong", net_return=-0.01,
                     closed_at=NOW + timedelta(days=6 + i), opened_at=NOW + timedelta(days=5 + i))
    review = engine.review_changes(db, now=NOW + timedelta(days=10))
    verdict = review["reviewed"][0]
    assert verdict["verdict"] == "working" and verdict["state"] == "confirmed" and not verdict["reverted"]
    assert verdict["after"]["trades"] == 6 and verdict["after"]["cause_share"] == 0.0
    change = engine.adopted_changes(db)[0]
    assert change["state"] == "confirmed" and change["verdict"] == "working"
    db.refresh(row)
    assert row.parameters["market_gate_sma"] == 20
    # A confirmed change is not judged again.
    assert engine.review_changes(db, now=NOW + timedelta(days=11))["reviewed"] == []


def test_verdict_reverts_a_change_that_is_not_helping(db, monkeypatch, small_universe):  # noqa: F811
    monkeypatch.setattr(settings, "experiment_verdict_min_trades", 5)
    row = _strategy(db, "channel_breakout")
    change = _adopt(db, row, monkeypatch, small_universe)
    # Trades opened before the change do not count, even if they closed after it.
    _attribution(db, strategy_type="channel_breakout", cause="missed_market_move", net_return=-0.05,
                 closed_at=NOW + timedelta(days=1), opened_at=NOW - timedelta(days=1))
    for i in range(5):
        _attribution(db, strategy_type="channel_breakout", cause="missed_market_move", net_return=-0.04,
                     closed_at=NOW + timedelta(days=2 + i), opened_at=NOW + timedelta(days=1 + i),
                     contributing=["market_down"])
    review = engine.review_changes(db, now=NOW + timedelta(days=8))
    verdict = review["reviewed"][0]
    assert verdict["verdict"] == "not_helping" and verdict["state"] == "reverted" and verdict["reverted"]
    assert verdict["after"]["trades"] == 5
    db.refresh(row)
    assert "market_gate_sma" not in row.parameters
    governance = db.scalars(select(AuditLog).where(AuditLog.action == "market_gate_update").order_by(AuditLog.id)).all()
    assert governance[-1].payload["reverted_change"] == change["change_id"]
    # The seed step still leaves the row alone after the revert.
    assert seed.enable_market_gates(db) == []
    assert engine.adopted_changes(db)[0]["state"] == "reverted"


def test_judge_rules():
    judge = engine.judge
    assert judge({"expectancy_bps": 10.0, "cause_share": 0.8}, {"expectancy_bps": 12.0, "cause_share": 0.8, "trades": 20},
                 max_trades=60) == "working"
    assert judge({"expectancy_bps": 10.0, "cause_share": 0.8}, {"expectancy_bps": 5.0, "cause_share": 0.3, "trades": 20},
                 max_trades=60) == "working"                       # cause halved: the lesson was learned
    assert judge({"expectancy_bps": 10.0, "cause_share": 0.8}, {"expectancy_bps": -5.0, "cause_share": 0.9, "trades": 20},
                 max_trades=60) == "not_helping"
    assert judge({"expectancy_bps": 10.0, "cause_share": 0.8}, {"expectancy_bps": 5.0, "cause_share": 0.6, "trades": 20},
                 max_trades=60) == "watching"                      # mixed: keep watching
    assert judge({"expectancy_bps": 10.0, "cause_share": 0.8}, {"expectancy_bps": 5.0, "cause_share": 0.6, "trades": 60},
                 max_trades=60) == "inconclusive"
    assert judge({"expectancy_bps": None, "cause_share": None}, {"expectancy_bps": 5.0, "cause_share": 0.6, "trades": 20},
                 max_trades=60) == "inconclusive"


# ------------------------------------------------------------- gate replay

def test_earnings_gate_blocks_the_sessions_before_a_report():
    frame = _walk(rows=30, seed=1)
    frame[SURPRISE_COLUMN] = np.nan
    frame.loc[10, SURPRISE_COLUMN] = 2.0   # the report reacts on bar 10
    allowed = gate_replay.earnings_gate_on(frame, 3)
    assert allowed[6] and not allowed[7] and not allowed[9] and allowed[10]
    assert gate_replay.earnings_gate_on(frame, 0).all()
    assert gate_replay.earnings_gate_on(frame.drop(columns=[SURPRISE_COLUMN]), 5).all()


def test_vix_and_market_gates_in_replay():
    frame = _walk(rows=6, seed=2, start="2024-01-02")
    vix = pd.DataFrame({"date": frame["date"], "close": [15, 22, 28, 18, 35, 19]})
    np.testing.assert_array_equal(gate_replay.vix_gate_on(frame, vix, "below", 20), [1, 0, 0, 1, 0, 1])
    np.testing.assert_array_equal(gate_replay.vix_gate_on(frame, vix, "above", 20), [0, 1, 1, 0, 1, 0])
    assert gate_replay.vix_gate_on(frame, None, "below", 20).all()         # fails open, as live
    assert gate_replay.vix_gate_on(frame, vix, None, None).all()
    assert not gate_replay.spy_gate_on(frame, None, 20).any()              # fails closed, as live
    assert gate_replay.spy_gate_on(frame, None, 0).all()
    assert gate_replay.setup_from_parameters({"vix_gate": "not_rising"})["vix_gate"] is None


def test_setup_replay_matches_the_market_gate_script():
    import sys
    from pathlib import Path

    scripts = Path(__file__).resolve().parents[1] / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        import run_market_gate_backtest as bt
    finally:
        sys.path.remove(str(scripts))
    frames = {"SPY": _walk(rows=700, seed=11), "QQQ": _walk(rows=700, seed=12)}
    from app.services import meta_label_primary as primary
    replays = {s: primary.replay_signals(f) for s, f in frames.items()}
    for strategy in ("channel_breakout", "ibs_mean_reversion"):
        legacy = bt.simulate(strategy, frames, frames["SPY"], 20, replays)
        new = gate_replay.simulate(strategy, frames, frames["SPY"], {"market_gate_sma": 20}, replays)
        pd.testing.assert_frame_equal(legacy.reset_index(drop=True), new.reset_index(drop=True))
    trades = gate_replay.simulate("channel_breakout", frames, frames["SPY"], {}, replays)
    rows = gate_replay.window_rows(trades, frames)
    assert set(rows) == set(engine.WINDOWS)
    assert rows["all"]["total_profit_bps"] == pytest.approx(rows["all"]["trades"] * rows["all"]["expectancy_bps"], abs=0.1)
    assert gate_replay.window_rows(pd.DataFrame(), frames)["2020+"]["trades"] == 0


# ------------------------------------------------------- wiring and surfaces

def test_per_strategy_earnings_blackout():
    assert strategy_blackout_sessions(None) is None and strategy_blackout_sessions({}) is None
    assert strategy_blackout_sessions({"earnings_blackout_sessions": 0}) == 0
    assert strategy_blackout_sessions({"earnings_blackout_sessions": "20"}) == 20
    assert strategy_blackout_sessions({"earnings_blackout_sessions": "x"}) is None
    # A strategy that turned its blackout off is not blocked by the account-wide one.
    assert earnings_blackout("AAPL", NOW, sessions=0, fetch=lambda symbol: [NOW.date()]) is None


def test_jobs_route_and_scoreboard_are_wired(db, monkeypatch, small_universe):  # noqa: F811
    beat = celery_app.conf.beat_schedule
    assert beat["experiment-engine"]["task"] == "app.tasks.jobs.experiment_engine_job"
    assert beat["experiment-verdict"]["task"] == "app.tasks.jobs.experiment_verdict_job"
    assert celery_app.conf.task_routes["app.tasks.jobs.experiment_engine_job"] == {"queue": "learning"}
    assert celery_app.conf.task_routes["app.tasks.jobs.experiment_verdict_job"] == {"queue": "learning"}
    assert required_role("GET", "/stock-paper/learning-experiments") == "viewer"
    from app.services.learning_scoreboard import build_learning_scoreboard

    monkeypatch.setattr("app.services.stock_paper_ledger.active_paper_broker_name", lambda: "alpaca_paper")
    row = _strategy(db, "channel_breakout")
    change = _adopt(db, row, monkeypatch, small_universe)
    changes = {c["key"]: c for c in build_learning_scoreboard(db, now=NOW + timedelta(days=1))["changes"]}
    item = changes[f"experiment:{change['change_id']}"]
    assert item["strategy_type"] == "channel_breakout" and item["state"] == "watching"
    assert item["before"]["trades"] == 5 and item["after"]["trades"] == 0


def test_real_replay_runs_end_to_end(db, monkeypatch, small_universe):  # noqa: F811
    """The unfaked path: synthetic history, real replay, real decision, JSON-safe ledger."""
    _strategy(db, "channel_breakout")
    _repeat_market_losses(db, "channel_breakout")
    result = engine.run_experiment_engine(db, now=NOW, **small_universe)
    assert result["status"] == "complete" and result["planned"] == 1 and result["errors"] == []
    payload = db.scalar(select(AuditLog).where(AuditLog.action == engine.ACTION_EXPERIMENT)).payload
    assert set(payload["rows"]["baseline"]) == set(engine.WINDOWS)
    assert {"trades", "expectancy_bps", "total_profit_bps", "portfolio_max_drawdown"} <= set(payload["rows"]["baseline"]["all"])
    assert payload["decision"]["rule"] == engine.RULE
