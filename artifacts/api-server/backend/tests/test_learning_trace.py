"""Learning trace: diagnosed problem -> experiment -> change -> forward evaluation -> retained or reverted."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.core.config import settings
from app.models import AuditLog
from app.models.loss_feedback import StockTradeAttribution
from app.services import experiment_engine as engine
from app.services import learning_trace as lt
from app.services.audit import write_audit_log
from app.services.loss_diagnosis import repeating_failures
from tests.test_experiment_engine import (NOW, _adopt, _attribution, _fake_replay, _repeat_market_losses,  # noqa: F401
                                          small_universe)
from tests.test_loss_feedback_loop import _strategy, db  # noqa: F401  (fixtures)


def _market_loss_ids(db):
    rows = db.scalars(select(StockTradeAttribution).order_by(StockTradeAttribution.id)).all()
    return [row.id for row in rows if row.is_loss and (row.detail or {})["diagnosis"]["cause"] == "missed_market_move"]


def test_a_lesson_names_the_losing_trades_it_stands_on_and_its_id_follows_the_evidence(db):  # noqa: F811
    _repeat_market_losses(db, "channel_breakout")
    rows = db.scalars(select(StockTradeAttribution)).all()
    lesson = next(item for item in repeating_failures(rows) if item["strategy_type"] == "channel_breakout")
    assert lesson["attribution_ids"] == _market_loss_ids(db) and len(lesson["attribution_ids"]) == 4
    identity = engine.lesson_identity(lesson)
    assert identity["lesson_source"] == "trade_attribution" and identity["lesson_attribution_ids"] == lesson["attribution_ids"]
    assert len(identity["lesson_id"]) == 12 and engine.lesson_identity(dict(lesson)) == identity
    # One more loss is new evidence: a new id.
    _attribution(db, strategy_type="channel_breakout", cause="missed_market_move", net_return=-0.02,
                 closed_at=NOW - timedelta(days=1), contributing=["market_down"])
    grown = next(item for item in repeating_failures(db.scalars(select(StockTradeAttribution)).all())
                 if item["strategy_type"] == "channel_breakout")
    assert engine.lesson_identity(grown)["lesson_id"] != identity["lesson_id"]
    # A lesson read from a ledger (gate cost, setup shadow) has no trades of its own.
    ledger = engine.lesson_identity({"strategy_type": "ibs_mean_reversion", "cause": "gate_cost", "gate": "vix", "count": 9})
    assert ledger["lesson_source"] == "ledger" and ledger["lesson_attribution_ids"] == []
    assert ledger["lesson_id"] != engine.lesson_identity({"strategy_type": "ibs_mean_reversion", "cause": "gate_cost",
                                                         "gate": "vix", "count": 10})["lesson_id"]
    # Detail dicts (no row id) still make a lesson; it simply names no trade.
    assert all(item["attribution_ids"] == [] for item in repeating_failures(
        [{"strategy_type": "x", "symbol": "AAPL", "closed_at": f"2026-11-0{i}", "diagnosis": {"cause": "signal_wrong"},
          "is_loss": True} for i in range(1, 5)]))


def test_a_chain_runs_from_the_losing_trades_to_a_retained_change(db, monkeypatch, small_universe):  # noqa: F811
    monkeypatch.setattr(settings, "experiment_verdict_min_trades", 6)
    row = _strategy(db, "channel_breakout")
    change = _adopt(db, row, monkeypatch, small_universe)
    trace = lt.learning_trace(db)
    (chain,) = trace["chains"]
    assert chain["strategy_type"] == "channel_breakout" and chain["template"] == "market_gate"
    assert chain["diagnosis"]["cause"] == "missed_market_move" and chain["diagnosis"]["source"] == "trade_attribution"
    assert chain["diagnosis"]["attribution_ids"] == _market_loss_ids(db) and chain["diagnosis"]["losses"] == 4
    assert len(chain["diagnosis"]["lesson_id"]) == 12 and chain["diagnosis"]["lesson"]
    assert chain["experiment"]["status"] == "adopted" and chain["experiment"]["adopted"] == "sma20"
    assert chain["experiment"]["cost_seconds"] >= 0 and chain["experiment"]["setups"] >= 2
    assert chain["change"]["change_id"] == change["change_id"] and chain["change"]["after"] == {"market_gate_sma": 20}
    assert chain["forward"] is None and chain["outcome"] == "watching" and chain["gaps"] == ["adopted, not judged yet"]
    assert trace["summary"]["outcomes"]["watching"] == 1 and trace["summary"]["with_lesson_id"] == 1
    assert trace["summary"]["research_seconds"] is not None and trace["evidence"].startswith("Not yet: 1 experiments, 1 changes adopted")
    # Six winning trades opened after the change: the forward judge confirms it.
    for i in range(6):
        _attribution(db, strategy_type="channel_breakout", cause="edge", net_return=0.02,
                     closed_at=NOW + timedelta(days=2 + i), opened_at=NOW + timedelta(days=1 + i))
    engine.review_changes(db, now=NOW + timedelta(days=10))
    trace = lt.learning_trace(db)
    (chain,) = trace["chains"]
    forward = chain["forward"]
    assert chain["outcome"] == "retained" and chain["complete"] is True and chain["gaps"] == []
    assert forward["state"] == "confirmed" and forward["verdict"] == "working"
    assert forward["trades_after"] == 6 and forward["trades_needed"] == 6 and forward["sample_sufficient"] is True
    assert forward["expectancy_after_bps"] == 200.0 and forward["before_after_bps"] == forward["expectancy_after_bps"] - forward["expectancy_before_bps"]
    assert forward["comparison"].startswith("before against after: not a control")
    assert trace["summary"]["retained_on_sufficient_sample"] == 1 and trace["summary"]["complete"] == 1
    assert trace["evidence"].startswith("1 change(s) retained on a sufficient forward sample")
    # The run summary carries what the whole run cost.
    run = db.scalars(select(AuditLog).where(AuditLog.event_type == "experiment_engine", AuditLog.action == "run")).first()
    assert run.payload["cost"]["seconds"] >= run.payload["cost"]["experiment_seconds"] >= 0


def test_a_reverted_change_and_an_experiment_that_changed_nothing_are_told_apart(db, monkeypatch, small_universe):  # noqa: F811
    monkeypatch.setattr(settings, "experiment_verdict_min_trades", 5)
    row = _strategy(db, "channel_breakout")
    _adopt(db, row, monkeypatch, small_universe)
    for i in range(5):
        _attribution(db, strategy_type="channel_breakout", cause="missed_market_move", net_return=-0.04,
                     closed_at=NOW + timedelta(days=2 + i), opened_at=NOW + timedelta(days=1 + i),
                     contributing=["market_down"])
    engine.review_changes(db, now=NOW + timedelta(days=8))
    # A second strategy's experiment finds nothing better than the current setup.
    _strategy(db, "macd_momentum")
    _repeat_market_losses(db, "macd_momentum")
    worse = {f"sma{window}": (4.0, 0.25, 150) for window in (5, 10, 20, 50, 100, 200)}
    simulate = _fake_replay(monkeypatch, {"baseline": (5.0, 0.20, 200), **worse})
    second = engine.run_experiment_engine(db, now=NOW + timedelta(days=9), simulate=simulate, **small_universe)
    # The first strategy is tested again too (its lesson grew by five losses); neither experiment adopts anything.
    assert sorted(item["strategy_type"] for item in second["experiments"]) == ["channel_breakout", "macd_momentum"]
    assert second["adopted"] == []
    trace = lt.learning_trace(db)
    assert len(trace["chains"]) == 3
    reverted = next(chain for chain in trace["chains"] if chain["change"])
    unchanged = next(chain for chain in trace["chains"] if chain["strategy_type"] == "macd_momentum")
    retest = next(chain for chain in trace["chains"] if chain["strategy_type"] == "channel_breakout" and not chain["change"])
    assert reverted["strategy_type"] == "channel_breakout" and reverted["outcome"] == "reverted"
    assert reverted["forward"]["trades_after"] == 5 and reverted["forward"]["expectancy_after_bps"] == -400.0
    assert unchanged["outcome"] == "no_change" and unchanged["change"] is None and unchanged["forward"] is None
    assert unchanged["experiment"]["status"] == "rejected" and unchanged["complete"] is True
    # The re-test stands on different evidence than the first experiment did.
    assert retest["diagnosis"]["lesson_id"] != reverted["diagnosis"]["lesson_id"]
    assert trace["summary"]["outcomes"] == {"no_change": 2, "watching": 0, "retained": 0, "reverted": 1, "closed": 0}
    assert trace["summary"]["changes_adopted"] == 1 and trace["summary"]["retained_on_sufficient_sample"] == 0
    assert "none retained on a sufficient forward sample" in trace["evidence"]


def test_missing_links_are_listed_as_gaps_never_invented(db):  # noqa: F811
    assert lt.learning_trace(db)["evidence"] == "No experiment has run yet, so there is nothing to judge."
    # An experiment recorded before lessons carried an id or a cost, which says it adopted a change no row holds.
    write_audit_log(db, event_type="experiment_engine", action="experiment", status="adopted", message="old",
                    payload={"strategy_type": "trend_pullback", "template": "market_gate", "trigger": "missed_market_move",
                             "lesson_count": 3, "adopted": "sma10", "applied": True, "ran_at": "2026-11-01T17:00:00+00:00",
                             "rows": {"baseline": {}, "sma10": {}}})
    # A change with no experiment row behind it, judged on fewer trades than it needs.
    write_audit_log(db, event_type="experiment_engine", action="change_adopted", status="watching", message="orphan",
                    payload={"change_id": "orphan000001", "strategy_type": "rsi2_pullback", "watch": {"min_trades": 20}})
    write_audit_log(db, event_type="experiment_engine", action="experiment", status="adopted", message="thin",
                    payload={"strategy_type": "ibs_mean_reversion", "template": "vix_gate", "trigger": "gate_cost",
                             "lesson_id": "abc123abc123", "lesson_source": "ledger", "change_id": "thin00000001",
                             "adopted": "vix20", "applied": True, "cost": {"seconds": 1.5, "setups": 3}})
    write_audit_log(db, event_type="experiment_engine", action="change_adopted", status="watching", message="thin",
                    payload={"change_id": "thin00000001", "strategy_type": "ibs_mean_reversion", "watch": {"min_trades": 20}})
    write_audit_log(db, event_type="experiment_engine", action="change_verdict", status="confirmed", message="thin",
                    payload={"change_id": "thin00000001", "state": "confirmed", "verdict": "working",
                             "after": {"trades": 0, "expectancy_bps": None}, "shadow": {"decisions": 40, "beating": 0},
                             "realised_gain_bps": 12.0, "realised_source": "paired_shadow"})
    db.flush()
    trace = lt.learning_trace(db)
    thin, old = trace["chains"]
    assert old["gaps"] == ["no lesson id: the experiment was recorded before lessons carried one", "no cost recorded",
                           "the experiment says it adopted a change, but no change row is linked"]
    assert old["outcome"] == "no_change" and old["experiment"]["setups"] == 2 and old["complete"] is False
    assert thin["outcome"] == "retained" and thin["forward"]["sample_sufficient"] is False
    assert thin["forward"]["comparison"].startswith("concurrent")
    assert thin["gaps"] == ["judged on 0 trades, fewer than the 20 it needs (decided on shadow evidence)"]
    assert trace["unlinked_changes"] == ["orphan000001"]
    # Retained on a thin sample is not counted as proof.
    assert trace["summary"]["retained_on_sufficient_sample"] == 0 and trace["summary"]["research_seconds"] == 1.5
    assert trace["summary"]["experiments_with_cost"] == 1 and trace["summary"]["with_lesson_id"] == 1


def test_the_route_exists_for_viewers():
    from app.api.stock_paper import router
    from app.core.security import required_role

    paths = {route.path: route.methods for route in router.routes}
    assert "GET" in paths["/stock-paper/learning-trace"]
    assert required_role("GET", "/stock-paper/learning-trace") == "viewer"
