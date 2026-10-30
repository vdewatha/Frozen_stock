"""Paper loss feedback loop: trade attribution, loss-driven agenda, governed
parameter application, meta-label auto-enforce and performance weights."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  register all tables
from app.core.config import settings
from app.db.base import Base
from app.models import StockLearningCycle, Strategy, StrategySignal
from app.models.loss_feedback import (
    StockAllocationWeight,
    StockMetaLabelMode,
    StockMetaLabelModeEvent,
    StockParameterApplication,
    StockParameterSet,
    StockTradeAttribution,
)
from app.models.stock_paper import StockPaperAccount
from app.services import (
    experiments,
    loss_feedback,
    meta_label_modes,
    parameter_application,
    strategy_allocation,
    trade_attribution,
)
from app.services.learning import propose_parameter_experiments, rank_proposals_for_losses
from app.services.meta_label_shadow import MetaLabelRuntime
from app.services.stock_paper_autotrader import _quantity_for_buy
from tests.test_paper_trade_scorecard import Ledger, at

UTC = timezone.utc
NOW = datetime(2026, 10, 30, 21, 0, tzinfo=UTC)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = Session(engine)
    yield session
    session.close()
    engine.dispose()


def _strategy(db, strategy_type, parameters=None, status="paper_trading_active"):
    row = Strategy(name=strategy_type.title(), strategy_type=strategy_type, parameters=parameters or {},
                   current_status=status)
    db.add(row)
    db.flush()
    return row


_counter = {"n": 0}


def _attribution(db, *, strategy_type, symbol="SPY", net_return, exit_cause="indicator", closed_at=None,
                 source="scheduled_round_trip", meta=None, quality="moderate", costs_known=True):
    _counter["n"] += 1
    closed = closed_at or NOW - timedelta(days=2)
    detail = {"signal_quality": {"bucket": quality}, "regime": {"trend": "up", "volatility": "normal"},
              "holding": {"relation": "before_rule"}, "mistakes": {"primary": None}, "meta_label": meta}
    row = StockTradeAttribution(
        source=source, source_key=f"test:{_counter['n']}", revision=1, attribution_sha256="0" * 64,
        strategy_type=strategy_type, symbol=symbol, opened_at=closed - timedelta(hours=5), closed_at=closed,
        exit_cause=exit_cause, net_return=Decimal(str(net_return)) if costs_known else None,
        gross_return=Decimal(str(net_return)), net_pnl=Decimal(str(net_return * 1000)) if costs_known else None,
        costs_known=costs_known, is_loss=net_return <= 0, detail=detail, paper_only=True,
    )
    db.add(row)
    db.flush()
    return row


# ------------------------------------------------------------------ attribution

def test_exit_cause_vocabulary():
    cause = trade_attribution.exit_cause
    assert cause("stop_loss_leg") == "catastrophe_stop"
    assert cause("protective_stop") == "catastrophe_stop"
    assert cause("stop") == "catastrophe_stop"
    assert cause("horizon") == "time_stop"
    assert cause("trial_window_complete") == "time_stop"
    assert cause("sell_signal") == "indicator"
    assert cause("sell_signal", held_sessions=5, max_holding_sessions=5) == "time_stop"
    assert cause("take_profit_leg") == "take_profit"
    assert cause("drawdown_limit") == "protection_lock"
    assert cause("recovery") == "protection_lock"
    assert cause("operator_stop") == "operator"
    assert cause(None) == "other"


def test_regime_slippage_and_holding_helpers():
    flat = [100.0 + (i % 2) * 0.1 for i in range(60)]
    rising = [100.0 * (1.001 ** i) for i in range(60)]
    wild = [100.0 * (1.05 if i % 2 else 0.95) for i in range(60)]
    assert trade_attribution.regime_tags(flat[:10])["trend"] == "unknown"
    assert trade_attribution.regime_tags(rising)["trend"] == "up"
    assert trade_attribution.regime_tags(flat)["volatility"] == "low"
    assert trade_attribution.regime_tags(wild)["volatility"] == "high"
    # Positive = adverse for both sides.
    assert trade_attribution.slippage_bps(100.5, 100.0, "buy") == pytest.approx(50.0)
    assert trade_attribution.slippage_bps(96.8, 97.0, "sell") == pytest.approx(20.6186, rel=1e-3)
    assert trade_attribution.holding_relation(3, 5) == "before_rule"
    assert trade_attribution.holding_relation(5, 5) == "at_rule"
    assert trade_attribution.holding_relation(6, 5) == "beyond_rule"
    assert trade_attribution.holding_relation(6, None) == "no_time_rule"
    assert trade_attribution.signal_quality(probability=0.52, threshold=0.5)["bucket"] == "marginal"
    assert trade_attribution.signal_quality(confidence=0.8)["bucket"] == "strong"


def _book(db):
    account = StockPaperAccount(broker="alpaca_paper", broker_account_id="paper", cash=Decimal("9000"),
                                buying_power=Decimal("9000"), equity=Decimal("10000"), status="reconciled",
                                raw_payload={})
    db.add(account)
    db.flush()
    trend = _strategy(db, "trend_a", {"max_holding_sessions": 5})
    signal = StrategySignal(strategy_id=trend.id, symbol="AAPL", signal_time=at(14), action="BUY",
                            confidence=Decimal("0.52"), probability_up=Decimal("0.6"),
                            features={"meta_label": {"mode": "shadow", "decision": "take", "probability": 0.61}})
    db.add(signal)
    db.flush()
    ledger = Ledger(db, account)
    entry = ledger.order(symbol="AAPL", side="buy", quantity=10, strategy=trend, order_class="bracket",
                         stop=97, take=103, signal=signal)
    entry.limit_price = Decimal("100")
    ledger.fill(entry, 10, "100.5", at(14, 1), fee="0.05")
    ledger.order(symbol="AAPL", side="sell", quantity=10, strategy=trend, order_class="bracket",
                 parent=entry, leg_role="take_profit")
    stop = ledger.order(symbol="AAPL", side="sell", quantity=10, strategy=trend, order_class="bracket",
                        parent=entry, leg_role="stop_loss", stop=97)
    stop_fill = ledger.fill(stop, 10, "96.8", at(18), fee="0.05")
    return account, trend, entry, stop_fill


def test_round_trip_attribution_explains_a_catastrophe_stop_loss(db):
    account, _trend, entry, _fill = _book(db)
    rows = trade_attribution.attribute_round_trips(db, account_id=account.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.source == "scheduled_round_trip" and row.source_key == f"round_trip:{entry.id}"
    assert row.strategy_type == "trend_a" and row.symbol == "AAPL"
    assert row.exit_cause == "catastrophe_stop"
    assert row.is_loss and row.costs_known
    expected_net = ((Decimal("96.8") - Decimal("100.5")) * 10 - Decimal("0.10")) / Decimal("1005")
    assert float(row.net_return) == pytest.approx(float(expected_net), rel=1e-6)
    detail = row.detail
    assert detail["slippage"]["entry_bps"] == pytest.approx(50.0)
    assert detail["slippage"]["exit_reference_price"] == pytest.approx(97.0)
    assert detail["slippage"]["exit_bps"] == pytest.approx(20.6186, rel=1e-3)
    assert detail["signal_quality"]["bucket"] == "marginal"
    assert detail["holding"]["rule_sessions"] == 5 and detail["holding"]["relation"] == "before_rule"
    assert detail["regime"]["trend"] == "unknown"  # no daily closes before entry -> explicit unknown
    assert detail["meta_label"]["decision"] == "take" and detail["meta_label"]["mode"] == "shadow"
    assert detail["paper_only"] and detail["hindsight_only"]

    # Idempotent: unchanged evidence records nothing new.
    assert trade_attribution.attribute_round_trips(db, account_id=account.id) == []


def test_changed_evidence_appends_a_revision_not_an_edit(db):
    account, _trend, entry, stop_fill = _book(db)
    first = trade_attribution.attribute_round_trips(db, account_id=account.id)[0]
    original_sha = first.attribution_sha256
    stop_fill.fee = Decimal("1.00")  # late commission enrichment
    db.flush()
    second = trade_attribution.attribute_round_trips(db, account_id=account.id)
    assert len(second) == 1 and second[0].revision == 2
    assert first.attribution_sha256 == original_sha
    current = trade_attribution.current_attributions(db)
    assert [row.revision for row in current] == [2]


def test_trial_lot_attribution_uses_horizon_rule(db):
    trial = SimpleNamespace(id="trial-1", strategy_id=None, policy={"probability_threshold": 0.55})
    trade = {"lot_id": 7, "trial_id": "trial-1", "entry_decision_id": 3, "symbol": "SPY",
             "entered_at": at(14).isoformat(), "exited_at": (at(14) + timedelta(days=6)).isoformat(),
             "entry_price": 500.0, "exit_price": 495.0, "quantity": 2.0, "fees": 0.0,
             "planned_horizon_sessions": 4, "exit_reason": "horizon", "probability": 0.57,
             "classification": {"primary_mistake": "held_too_long", "mistakes": ["held_too_long"]}}
    with patch("app.services.trade_mistakes.trial_lot_trades", return_value=[trade]):
        rows = trade_attribution.attribute_trial(db, trial)
        again = trade_attribution.attribute_trial(db, trial)
    assert again == []
    row = rows[0]
    assert row.source == "trial_lot" and row.trial_id == "trial-1"
    assert row.strategy_type == "stock_forward_trial"
    assert row.exit_cause == "time_stop" and row.is_loss
    assert float(row.net_return) == pytest.approx(-0.01)
    assert row.detail["holding"]["relation"] == "at_rule"
    assert row.detail["signal_quality"]["bucket"] == "marginal"
    assert row.detail["mistakes"]["primary"] == "held_too_long"


def test_attribution_projection_breaks_down_losses(db):
    _attribution(db, strategy_type="trend_a", symbol="AAPL", net_return=-0.03, exit_cause="catastrophe_stop")
    _attribution(db, strategy_type="trend_a", symbol="MSFT", net_return=0.01)
    _attribution(db, strategy_type="ibs_mean_reversion", symbol="SPY", net_return=-0.01, exit_cause="time_stop")
    out = trade_attribution.attribution_projection(db, days=30, now=NOW)
    summary = out["summary"]
    assert summary["trades"] == 3 and summary["losses"] == 2
    assert summary["by_strategy"][0]["key"] == "trend_a"
    assert {row["key"] for row in summary["by_exit_cause"]} == {"catastrophe_stop", "indicator", "time_stop"}
    assert summary["by_symbol"][0]["key"] == "AAPL"
    assert len(out["recent_losses"]) == 2
    filtered = trade_attribution.attribution_projection(db, days=30, now=NOW, exit_cause_filter="time_stop")
    assert filtered["summary"]["trades"] == 1


# ------------------------------------------------------------------ agenda

def test_agenda_ranks_strategies_by_recent_attributed_losses(db):
    for strategy_type in ("rsi2_pullback", "ibs_mean_reversion", "winner"):
        _strategy(db, strategy_type)
    for _ in range(3):
        _attribution(db, strategy_type="rsi2_pullback", symbol="QQQ", net_return=-0.02, exit_cause="catastrophe_stop",
                     quality="marginal")
    _attribution(db, strategy_type="rsi2_pullback", symbol="SPY", net_return=-0.005, exit_cause="indicator")
    _attribution(db, strategy_type="ibs_mean_reversion", symbol="SPY", net_return=-0.01, exit_cause="time_stop")
    _attribution(db, strategy_type="winner", symbol="SPY", net_return=0.04)
    _attribution(db, strategy_type="stock_forward_trial", source="trial_lot", net_return=-0.5)
    # Outside the window: ignored.
    _attribution(db, strategy_type="ibs_mean_reversion", net_return=-0.9, closed_at=NOW - timedelta(days=90))

    agenda = parameter_application.build_loss_agenda(db, now=NOW)
    ranked = [item["strategy_type"] for item in agenda["items"]]
    assert ranked == ["rsi2_pullback", "ibs_mean_reversion"]
    top = agenda["items"][0]
    assert top["rank"] == 1 and top["losses"] == 4
    assert top["symbols"][0] == "QQQ"
    assert top["focus"][:2] == ["catastrophe_stop", "marginal_signal"]
    assert agenda["items"][1]["focus"] == ["time_stop"]
    # ML trial lots are never parameter-researched (training is out of scope).
    assert "stock_forward_trial" not in ranked

    order = parameter_application.agenda_strategy_order(
        db, ["moving_average_crossover", "ibs_mean_reversion", "winner", "rsi2_pullback"], now=NOW)
    assert order == ["rsi2_pullback", "ibs_mean_reversion", "moving_average_crossover", "winner"]


def test_loss_focus_orders_proposals_that_address_the_dominant_cause():
    current = {"entry_threshold": 10, "trend_filter_sma": 200, "symbol_scope": ["SPY"]}
    proposals = propose_parameter_experiments("rsi2_pullback", current)
    stops = rank_proposals_for_losses([dict(p) for p in proposals], current, ["catastrophe_stop"])
    assert "stricter_entry" in stops[0]["change_tags"]
    assert stops[0]["new_parameters"]["entry_threshold"] == 5
    assert all(p["new_parameters"]["symbol_scope"] == ["SPY"] for p in stops)
    # Without focus the original order is kept.
    plain = rank_proposals_for_losses([dict(p) for p in proposals], current, None)
    assert [p["experiment_name"] for p in plain] == [p["experiment_name"] for p in proposals]
    ibs = propose_parameter_experiments("ibs_mean_reversion", {"entry_threshold": 0.2, "exit_threshold": 0.8})
    timed = rank_proposals_for_losses([dict(p) for p in ibs], {"entry_threshold": 0.2, "exit_threshold": 0.8},
                                      ["time_stop"])
    assert "faster_exit" in timed[0]["change_tags"]


# ------------------------------------------------------------------ governed application

def _backtest(score):
    return {
        "score": score, "rejected": False, "rejection_reasons": [], "number_of_trades": 40,
        "max_drawdown": 0.05, "total_return": 0.05, "annualized_return": 0.05, "sharpe_ratio": 1.0,
        "sortino_ratio": 1.0, "win_rate": 0.5, "profit_factor": 1.3, "equity_curve": [],
        "excess_return": {"vs_cash": 0.05}, "beats_baselines": {"cash": True},
        "statistics": {"sharpe_per_period": 0.2, "observations": 300, "skewness": 0.0, "kurtosis": 3.0},
    }


def test_governed_experiment_never_edits_the_strategy_row():
    db = MagicMock()
    db.query.return_value.filter.return_value.one_or_none.return_value = SimpleNamespace(
        id=1, name="RSI2", parameters={"entry_threshold": 10})
    stored = SimpleNamespace(id=1, parameters={"entry_threshold": 10}, updated_at=None)
    db.get.return_value = stored
    prices = __import__("pandas").DataFrame({"date": [f"2025-01-{i % 28 + 1:02d}" for i in range(420)]})
    proposals = [{"experiment_name": "deeper", "new_parameters": {"entry_threshold": 5}, "hypothesis": "h"}]
    with patch.object(experiments, "trusted_history", return_value=(prices, "test")), \
         patch.object(experiments, "propose_parameter_experiments", return_value=proposals), \
         patch("app.services.backtester.run_backtest", side_effect=lambda *a, **k: _backtest(0.3)), \
         patch.object(experiments, "_decision", return_value=("promoted", "passed every gate")), \
         patch.object(experiments, "write_audit_log"), \
         patch.object(experiments, "list_strategy_experiments", return_value=[]):
        outcome = experiments.run_strategy_experiments(
            db, symbol="spy", strategy_slug="rsi2_pullback", apply_promotions=True, governed=True,
            loss_focus=["catastrophe_stop"])
    assert outcome["applied_parameters"] is None
    assert outcome["promoted_candidate"]["parameters"] == {"entry_threshold": 5}
    assert outcome["promoted_candidate"]["base_parameters"] == {"entry_threshold": 10}
    assert stored.parameters == {"entry_threshold": 10}


def _cycle(db, cycle_id, status):
    row = StockLearningCycle(
        cycle_id=cycle_id, request_sha256=cycle_id, trigger="scheduled", status=status, stage="training",
        requested_by="loss_feedback", symbols=["SPY"], cutoff_date=date(2026, 10, 30), horizon_days=5,
        provider="yfinance", seed=1, gates={}, evidence={}, last_reason="queued",
    )
    db.add(row)
    db.flush()
    return row


def test_parameter_set_flows_through_governed_cycle_before_overlay(db):
    strategy = _strategy(db, "rsi2_pullback", {"entry_threshold": 10, "symbol_scope": ["SPY"]})
    item = {"rank": 1, "trades": 4, "losses": 4, "loss_return": -0.065, "focus": ["catastrophe_stop"],
            "loss_by_exit_cause": {"catastrophe_stop": -0.06}, "attribution_ids": [11, 12]}
    candidate = {"parameters": {"entry_threshold": 5}, "experiment_id": 99, "experiment_name": "deeper",
                 "score": 0.3, "base_parameters": dict(strategy.parameters)}
    proposed = parameter_application.propose_parameter_set(
        db, strategy_type="rsi2_pullback", candidate=candidate, agenda_item=item, symbol="SPY", now=NOW)
    assert proposed["status"] == "proposed" and proposed["version"] == 1
    parameter_set = db.get(StockParameterSet, proposed["parameter_set_id"])
    assert parameter_set.parameters == {"entry_threshold": 5, "symbol_scope": ["SPY"]}
    assert parameter_set.lineage["experiment_id"] == 99
    assert parameter_set.lineage["attribution_ids"] == [11, 12]
    # Nothing is active before the governed cycle is promoted.
    assert parameter_application.parameter_overrides(db) == {}
    # A second candidate waits while one set is under governance.
    second = parameter_application.propose_parameter_set(
        db, strategy_type="rsi2_pullback", candidate=candidate | {"parameters": {"entry_threshold": 15}},
        agenda_item=item, symbol="SPY", now=NOW)
    assert second["status"] == "deferred"

    captured = {}

    def create(_db, **kwargs):
        captured.update(kwargs)
        return _cycle(db, "c" * 64, "queued"), False

    with patch("app.services.stock_learning_cycle.create_learning_cycle", side_effect=create), \
         patch("app.services.stock_learning_cycle.scheduled_learning_control_projection",
               return_value={"paused": False}):
        parameter_application.advance_parameter_applications(db, now=NOW)
        application = db.scalar(select(StockParameterApplication))
        assert application.status == "cycle_scheduled" and application.cycle_id == "c" * 64
        assert captured["lineage"]["parameter_set_id"] == parameter_set.id
        assert captured["lineage"]["experiment_id"] == 99
        assert captured["lineage"]["attribution_ids"] == [11, 12]
        assert captured["trigger"] == "scheduled"

        db.get(StockLearningCycle, "c" * 64).status = "promoted"
        parameter_application.advance_parameter_applications(db, now=NOW + timedelta(hours=1))
    assert application.status == "applied"
    overrides = parameter_application.parameter_overrides(db)
    assert overrides["rsi2_pullback"]["parameters"]["entry_threshold"] == 5
    # The strategy row itself is never edited in place.
    db.refresh(strategy)
    assert strategy.parameters == {"entry_threshold": 10, "symbol_scope": ["SPY"]}
    view = parameter_application.overlay(strategy, overrides)
    assert view.parameters["entry_threshold"] == 5 and view.id == strategy.id
    assert view.strategy_type == "rsi2_pullback" and view.parameter_set_version == 1
    # The identical set is never re-proposed.
    again = parameter_application.propose_parameter_set(
        db, strategy_type="rsi2_pullback", candidate=candidate | {"base_parameters": dict(strategy.parameters)},
        agenda_item=item, symbol="SPY", now=NOW)
    assert again["status"] == "skipped"


def test_failed_governed_cycle_rejects_the_parameter_set(db):
    _strategy(db, "ibs_mean_reversion", {"entry_threshold": 0.2})
    proposed = parameter_application.propose_parameter_set(
        db, strategy_type="ibs_mean_reversion",
        candidate={"parameters": {"entry_threshold": 0.1}, "experiment_id": 5, "experiment_name": "x", "score": 1},
        agenda_item={"attribution_ids": [1]}, symbol="SPY", now=NOW)
    cycle = _cycle(db, "d" * 64, "queued")
    application = db.scalar(select(StockParameterApplication).where(
        StockParameterApplication.parameter_set_id == proposed["parameter_set_id"]))
    application.status, application.cycle_id = "cycle_scheduled", cycle.cycle_id
    cycle.status = "demoted"
    with patch("app.services.stock_learning_cycle.scheduled_learning_control_projection",
               return_value={"paused": False}):
        parameter_application.advance_parameter_applications(db, now=NOW)
    assert application.status == "rejected"
    assert parameter_application.parameter_overrides(db) == {}


# ------------------------------------------------------------------ meta-label modes

def _meta_samples(db, strategy_type, mode, outcomes, *, closed_at=None):
    for decision, win in outcomes:
        _attribution(db, strategy_type=strategy_type, net_return=0.01 if win else -0.01,
                     meta={"mode": mode, "decision": decision}, closed_at=closed_at)


def test_shadow_metrics_precision_recall_and_lift():
    samples = [{"decision": "take", "win": True}] * 6 + [{"decision": "take", "win": False}] * 2 \
        + [{"decision": "skip", "win": True}] * 2 + [{"decision": "skip", "win": False}] * 4
    metrics = meta_label_modes.shadow_metrics(samples)
    assert metrics["samples"] == 14
    assert metrics["precision"] == pytest.approx(0.75)
    assert metrics["recall"] == pytest.approx(0.75)
    assert metrics["lift"] == pytest.approx(0.75 - 8 / 14, abs=1e-6)


def test_meta_label_promotes_to_enforce_then_demotes_on_degradation(db):
    _meta_samples(db, "trend_a", "shadow", [("take", True)] * 4 + [("take", False)] * 2 + [("skip", False)] * 4)
    with patch.object(settings, "meta_label_enforce_min_samples", 10), \
         patch.object(settings, "meta_label_demote_min_samples", 5):
        first = meta_label_modes.review_meta_label_modes(db, ["trend_a", "ibs_mean_reversion"], now=NOW)
        modes = {row["strategy_type"]: row["mode"] for row in first}
        assert modes == {"trend_a": "enforce", "ibs_mean_reversion": "shadow"}
        assert meta_label_modes.enforced_strategy_types(db) == {"trend_a"}
        event = db.scalar(select(StockMetaLabelModeEvent))
        assert (event.from_mode, event.to_mode) == ("shadow", "enforce")
        assert event.evidence["shadow"]["precision"] == pytest.approx(4 / 6, abs=1e-6)

        # Enforced trades after promotion lose: back to shadow with evidence.
        _meta_samples(db, "trend_a", "enforce", [("take", False)] * 4 + [("take", True)],
                      closed_at=NOW + timedelta(days=2))
        later = NOW + timedelta(days=3)
        for row in db.scalars(select(StockTradeAttribution).where(
                StockTradeAttribution.closed_at > NOW)).all():
            row.opened_at = NOW + timedelta(days=1)
        db.flush()
        second = meta_label_modes.review_meta_label_modes(db, ["trend_a"], now=later)
    assert second[0]["mode"] == "shadow"
    events = db.scalars(select(StockMetaLabelModeEvent).order_by(StockMetaLabelModeEvent.id)).all()
    assert [(e.from_mode, e.to_mode) for e in events] == [("shadow", "enforce"), ("enforce", "shadow")]
    assert events[1].evidence["enforced"]["precision"] == pytest.approx(0.2)


def test_meta_label_stays_shadow_below_sample_bar(db):
    _meta_samples(db, "trend_a", "shadow", [("take", True)] * 5)
    out = meta_label_modes.review_meta_label_modes(db, ["trend_a"], now=NOW)
    assert out[0]["mode"] == "shadow"
    assert "below minimum" in out[0]["evidence"]["reason"]
    assert db.scalar(select(func.count(StockMetaLabelModeEvent.id))) == 0


def test_runtime_enforces_only_auto_promoted_strategies():
    runtime = MetaLabelRuntime(configured_mode="shadow", mode="shadow", path="x", bundle={"threshold": 0.5},
                               enforced_types=frozenset({"trend_a"}))
    assert runtime.allows_buy({"decision": "skip"}, "other") == (True, None)
    allowed, reason = runtime.allows_buy({"decision": "skip", "status": "scored"}, "trend_a")
    assert not allowed and "enforce" in reason
    assert runtime.allows_buy({"decision": "take"}, "trend_a") == (True, None)
    # Model unavailable: the auto-enforced strategy fails closed, others stay open.
    degraded = MetaLabelRuntime(configured_mode="shadow", mode="off", path="x",
                                enforced_types=frozenset({"trend_a"}))
    assert degraded.allows_buy(None, "trend_a")[0] is False
    assert degraded.allows_buy(None, "other") == (True, None)
    # Configured off disables auto-enforce entirely.
    off = MetaLabelRuntime(configured_mode="off", mode="off", path="x", enforced_types=frozenset({"trend_a"}))
    assert off.allows_buy(None, "trend_a") == (True, None)


# ------------------------------------------------------------------ allocation weights

def test_performance_multiplier_is_bounded_and_shrink_first():
    policy = strategy_allocation.bounds()
    assert strategy_allocation.performance_multiplier([-0.5] * 3, policy)["multiplier"] == 1.0  # too few trades
    worst = strategy_allocation.performance_multiplier([-0.5] * 50, policy)
    best = strategy_allocation.performance_multiplier([0.5] * 50, policy)
    assert worst["multiplier"] == policy["floor"] and worst["status"] == "reduced"
    assert best["multiplier"] == policy["ceiling"] and best["status"] == "increased"
    mild_loss = strategy_allocation.performance_multiplier([-0.004] * 10, policy)["multiplier"]
    mild_gain = strategy_allocation.performance_multiplier([0.004] * 10, policy)["multiplier"]
    # Same magnitude: losses shrink more than gains grow.
    assert 1.0 - mild_loss > mild_gain - 1.0 > 0
    # Shrinkage: fewer trades move the weight less.
    few = strategy_allocation.performance_multiplier([-0.01] * 5, policy)["multiplier"]
    many = strategy_allocation.performance_multiplier([-0.01] * 40, policy)["multiplier"]
    assert many < few < 1.0
    for returns in ([-1.0] * 100, [1.0] * 100, [0.0] * 10):
        value = strategy_allocation.performance_multiplier(returns, policy)["multiplier"]
        assert policy["floor"] <= value <= policy["ceiling"]


def test_concentration_cap_trims_growth_only():
    capped = strategy_allocation.apply_concentration_cap({"a": 1.25, "b": 0.5, "c": 0.5}, 0.5)
    assert capped["a"] == 1.0 and capped["b"] == 0.5
    untouched = strategy_allocation.apply_concentration_cap({"a": 1.25, "b": 1.0, "c": 1.0}, 0.5)
    assert untouched == {"a": 1.25, "b": 1.0, "c": 1.0}


def test_session_weights_persist_once_per_session(db):
    for _ in range(10):
        _attribution(db, strategy_type="loser", net_return=-0.02)
        _attribution(db, strategy_type="winner", net_return=0.01)
    _attribution(db, strategy_type="probe", net_return=-0.5, source="exploration_round_trip")
    _attribution(db, strategy_type="unknown_cost", net_return=-0.5, costs_known=False)
    session = date(2026, 10, 30)
    weights = strategy_allocation.session_weights(
        db, ["loser", "winner", "probe", "unknown_cost"], session_date=session, now=NOW)
    assert weights["loser"] < Decimal("1") <= weights["winner"] <= Decimal("1.25")
    assert weights["probe"] == Decimal("1")  # exploration probes never count
    assert weights["unknown_cost"] == Decimal("1")  # unknown costs are not treated as zero
    # More losses later in the session do not change today's stored weights.
    for _ in range(10):
        _attribution(db, strategy_type="winner", net_return=-0.05)
    again = strategy_allocation.session_weights(db, ["loser", "winner"], session_date=session, now=NOW)
    assert again["winner"] == weights["winner"]
    assert db.scalar(select(func.count(StockAllocationWeight.id))) == 4
    projection = strategy_allocation.allocation_projection(db)
    assert projection["current"]["session_date"] == "2026-10-30"
    assert projection["live_sizing_affected"] is False


def test_sizing_applies_clamped_weight_but_never_to_exploration():
    account = SimpleNamespace(equity=Decimal("100000"))
    rules = {"max_risk_per_trade": "0.01"}
    base = _quantity_for_buy(account, Decimal("100"), rules)
    assert _quantity_for_buy(account, Decimal("100"), rules, allocation_weight=Decimal("0.5")) == base / 2
    assert _quantity_for_buy(account, Decimal("100"), rules, allocation_weight=Decimal("9")) == base * Decimal("1.25")
    assert _quantity_for_buy(account, Decimal("100"), rules, allocation_weight=Decimal("0")) == base / 2
    explore = _quantity_for_buy(account, Decimal("100"), rules, exploration=True)
    assert _quantity_for_buy(account, Decimal("100"), rules, exploration=True,
                             allocation_weight=Decimal("0.5")) == explore


# ------------------------------------------------------------------ evidence

def test_learning_from_losses_summary_is_deterministic_evidence(db):
    _strategy(db, "rsi2_pullback")
    _attribution(db, strategy_type="rsi2_pullback", net_return=-0.02, exit_cause="catastrophe_stop")
    trial_row = _attribution(db, strategy_type="stock_forward_trial", source="trial_lot", net_return=-0.01)
    trial_row.trial_id = "trial-9"
    db.flush()
    first = loss_feedback.learning_from_losses_summary(db, until=NOW, trial_ids=["trial-9"])
    second = loss_feedback.learning_from_losses_summary(db, until=NOW, trial_ids=["trial-9"])
    assert first == second
    assert first["status"] == "available"
    assert first["attribution"]["losses"] == 2
    assert first["trial_attribution"]["trades"] == 1
    assert first["agenda"][0]["strategy_type"] == "rsi2_pullback"
    assert first["paper_only"] is True and first["live_authorized"] is False


def test_loss_feedback_jobs_are_scheduled_on_learning_queue():
    from app.tasks.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule
    assert schedule["loss-feedback"]["task"] == "app.tasks.jobs.loss_feedback_job"
    assert schedule["parameter-application"]["task"] == "app.tasks.jobs.parameter_application_job"
    routes = celery_app.conf.task_routes
    assert routes["app.tasks.jobs.loss_feedback_job"]["queue"] == "learning"


def test_demotion_requires_a_fresh_shadow_sample_before_re_enforcing(db):
    _meta_samples(db, "trend_a", "shadow", [("take", True)] * 4 + [("take", False)] * 2 + [("skip", False)] * 4)
    with patch.object(settings, "meta_label_enforce_min_samples", 10), \
         patch.object(settings, "meta_label_demote_min_samples", 5):
        meta_label_modes.review_meta_label_modes(db, ["trend_a"], now=NOW)
        _meta_samples(db, "trend_a", "enforce", [("take", False)] * 5, closed_at=NOW + timedelta(days=2))
        for row in db.scalars(select(StockTradeAttribution).where(StockTradeAttribution.closed_at > NOW)).all():
            row.opened_at = NOW + timedelta(days=1)
        db.flush()
        demoted_at = NOW + timedelta(days=3)
        assert meta_label_modes.review_meta_label_modes(db, ["trend_a"], now=demoted_at)[0]["mode"] == "shadow"
        # The old favourable shadow trades must not immediately re-promote.
        again = meta_label_modes.review_meta_label_modes(db, ["trend_a"], now=demoted_at + timedelta(days=1))
        assert again[0]["mode"] == "shadow"
        assert again[0]["evidence"]["shadow"]["samples"] == 0
        # A new shadow sample collected after the demotion can qualify again.
        _meta_samples(db, "trend_a", "shadow", [("take", True)] * 6 + [("skip", False)] * 4,
                      closed_at=demoted_at + timedelta(days=5))
        for row in db.scalars(select(StockTradeAttribution).where(
                StockTradeAttribution.closed_at > demoted_at + timedelta(days=4))).all():
            row.opened_at = demoted_at + timedelta(days=4)
        db.flush()
        final = meta_label_modes.review_meta_label_modes(db, ["trend_a"], now=demoted_at + timedelta(days=6))
    assert final[0]["mode"] == "enforce"


def test_loss_feedback_routes_are_readable_by_viewers():
    from app.core.security import required_role

    assert required_role("GET", "/stock/forward-trials/learning/trade-attributions") == "viewer"
    assert required_role("GET", "/stock/forward-trials/learning/loss-feedback") == "viewer"
    assert required_role("POST", "/stock/forward-trials/learning/loss-feedback") == "admin"
