"""Long research passes never sit "idle in transaction" (2026-11-10).

The experiment engine's after-close run held the transaction its first reads
opened through 15 minutes of history loading and replays. Postgres terminated
the connection, the risk-rule read failed ("risk rule unavailable"), and the
book experiments ran with no risk rules.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from app.services import experiment_engine as engine
from tests.test_experiment_engine import NOW, _fake_replay, _repeat_market_losses, small_universe  # noqa: F401
from tests.test_loss_feedback_loop import _strategy, db  # noqa: F401  (fixture)


def test_history_loads_with_no_open_transaction(db, monkeypatch, small_universe):  # noqa: F811
    _strategy(db, "channel_breakout")
    _repeat_market_losses(db, "channel_breakout")
    simulate = _fake_replay(monkeypatch, {"baseline": (5.0, 0.20, 200), "sma20": (12.0, 0.15, 150)})
    seen = []
    base_loader = small_universe["loader"]

    def loader(symbol):
        seen.append(db.in_transaction())
        return base_loader(symbol)

    result = engine.run_experiment_engine(db, now=NOW, simulate=simulate, **{**small_universe, "loader": loader})
    assert result["status"] == "complete" and result["planned"] == 1
    assert seen and not any(seen)


def test_replays_run_with_no_open_transaction(db, monkeypatch, small_universe):  # noqa: F811
    _strategy(db, "channel_breakout")
    _repeat_market_losses(db, "channel_breakout")
    inner = _fake_replay(monkeypatch, {"baseline": (5.0, 0.20, 200), "sma20": (12.0, 0.15, 150)})
    seen = []

    def simulate(*args, **kwargs):
        seen.append(db.in_transaction())
        return inner(*args, **kwargs)

    engine.run_experiment_engine(db, now=NOW, simulate=simulate, **small_universe)
    assert seen and not seen[0]


def test_the_risk_rules_are_read_before_the_long_load(db, monkeypatch, small_universe):  # noqa: F811
    order = []
    monkeypatch.setattr(engine, "_active_rules", lambda _db: order.append("rules") or {"max_positions": 5})
    monkeypatch.setattr(engine, "plan_experiments", lambda *a, **k: {
        "planned": [{"template": next(t.name for t in engine.TEMPLATES if t.kind == "book"),
                     "strategy_type": "book", "trigger": "t", "lesson_count": 1, "lesson_scope": "s"}],
        "deferred": [], "skipped": [], "untemplated": []})
    base_loader = small_universe["loader"]
    engine.run_experiment_engine(db, now=NOW, simulate=lambda *a, **k: None,
                                 **{**small_universe, "loader": lambda s: order.append("load") or base_loader(s)})
    assert order[0] == "rules" and "load" in order


def test_release_keeps_writes_and_survives_a_dead_connection():
    db = MagicMock()
    engine.release_read_transaction(db)
    db.commit.assert_called_once()
    db = MagicMock()
    db.commit.side_effect = RuntimeError("terminating connection due to idle-in-transaction timeout")
    engine.release_read_transaction(db)
    db.rollback.assert_called_once()
