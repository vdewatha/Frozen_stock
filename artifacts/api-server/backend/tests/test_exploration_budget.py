"""Exploration budget: the learning-sized lots go where evidence is scarcest."""
from __future__ import annotations

import math
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.core.security import required_role
from app.models import StrategySignal
from app.services import exploration_budget as xb
from app.services.audit import write_audit_log
from app.services.stock_paper_autotrader import run_stock_paper_signal_cycle
from app.services.stock_paper_ledger import EXPLORATION_SOURCE, StockPaperError, _exploration_lift
from tests.test_learning_scoreboard import _news_run
from tests.test_loss_feedback_loop import _attribution, _strategy, db  # noqa: F401  (fixture)
from tests.test_paper_exploration import ExplorationLedgerTests, _Db
from tests.test_stock_paper_autotrader import signal_source

UTC = timezone.utc
NOW = datetime(2026, 11, 6, 15, 0, tzinfo=UTC)  # Friday 10:00 ET, inside the session
DISCOVERED = {"rule": {"kind": "ibs"}, "discovery": {"holdout": {"trades": 40, "win_rate": 0.6, "expectancy_bps": 35.0}}}


# ------------------------------------------------------------- evidence map

def test_wilson_half_width_shrinks_with_trades_and_is_none_without_them():
    assert xb.wilson(0, 0) == {"win_rate": None, "low": None, "high": None, "half_width": None}
    small, large = xb.wilson(6, 10), xb.wilson(60, 100)
    assert small["win_rate"] == large["win_rate"] == 0.6
    assert small["low"] < 0.6 < small["high"] and large["low"] < 0.6 < large["high"]
    assert large["half_width"] < small["half_width"]
    # The textbook value for 6 of 10 at z = 1.96.
    z, n, p = 1.96, 10, 0.6
    expected = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    assert small["half_width"] == round(expected, 4)


def test_backtest_expectancy_reads_discovered_rules_and_plain_parameters():
    assert xb.backtest_expectancy_bps(DISCOVERED) == 35.0
    assert xb.backtest_expectancy_bps({"discovery": {"discovery": {"net": {"expectancy_bps": 12.5}}}}) == 12.5
    assert xb.backtest_expectancy_bps({"backtest": {"expectancy_bps": -4}}) == -4.0
    assert xb.backtest_expectancy_bps({"expectancy_bps": "7.5"}) == 7.5
    assert xb.backtest_expectancy_bps({}) is None and xb.backtest_expectancy_bps(None) is None


def _shadow_scores(db, records):  # noqa: F811
    write_audit_log(db, event_type="stock_paper", action="setup_shadow_score", status="recorded", message="test",
                    payload={"report": {"as_of": "2026-11-05", "records": records, "decisions": []}})


def _watched_change(db, strategy_type, *, adopted_at, min_trades=20, change_id="abc123"):  # noqa: F811
    write_audit_log(db, event_type="experiment_engine", action="change_adopted", status="watching", message="test",
                    payload={"change_id": change_id, "strategy_id": 1, "strategy_type": strategy_type,
                             "template": "market_gate", "trigger": "costly_market_gate", "description": "SMA 20 -> 50",
                             "adopted_at": adopted_at.isoformat(), "state": "watching",
                             "expected": {"baseline": {"all": {"expectancy_bps": 5.0}},
                                          "adopted": {"all": {"expectancy_bps": 12.0}}},
                             "watch": {"min_trades": min_trades, "max_trades": 60}, "paper_only": True})


def test_evidence_map_counts_forward_trades_and_the_trades_still_needed(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "paper_allocation_min_trades", 5)
    monkeypatch.setattr(settings, "experiment_verdict_min_trades", 20)
    _strategy(db, "rule_ibs", parameters=DISCOVERED, status="paper_trading_candidate")
    _strategy(db, "macd_momentum", status="paper_trading_candidate")
    _strategy(db, "channel_breakout")  # active: scheduled size, not a learning strategy
    for i in range(4):
        _attribution(db, strategy_type="rule_ibs", net_return=0.01 if i < 3 else -0.01, source="exploration_round_trip",
                     closed_at=NOW - timedelta(days=10 - i))
    _attribution(db, strategy_type="channel_breakout", net_return=0.02, closed_at=NOW - timedelta(days=3))
    _shadow_scores(db, [
        {"strategy_type": "channel_breakout", "template": "market_gate", "candidate": "sma50", "live": "sma20",
         "is_live": False, "trades": 7, "overlap": 9, "win_rate": 0.5714, "expectancy_bps": 8.0,
         "delta_expectancy_bps": 3.0, "beats_live": False, "min_trades": 20},
        {"strategy_type": "channel_breakout", "template": "market_gate", "candidate": "sma20", "live": "sma20",
         "is_live": True, "trades": 9, "overlap": 9, "min_trades": 20},
    ])
    _watched_change(db, "channel_breakout", adopted_at=NOW - timedelta(days=5))
    _news_run(db, ["NVDA"])
    _attribution(db, strategy_type="channel_breakout", symbol="NVDA", net_return=0.03, closed_at=NOW - timedelta(days=1))

    out = xb.evidence_map(db, now=NOW)
    items = {item["key"]: item for item in out["items"]}
    rule = items["strategy:rule_ibs"]
    assert rule["trades"] == 4 and rule["wins"] == 3 and rule["needed"] == xb.STRATEGY_MIN_TRADES - 4
    assert rule["expectancy_bps"] == 35.0 and rule["win_rate"] == 0.75 and 0 < rule["half_width"] < 0.5
    assert rule["sources"] == {"exploration_round_trip": 4, "scheduled_round_trip": 0}
    assert items["strategy:macd_momentum"]["trades"] == 0 and items["strategy:macd_momentum"]["half_width"] is None
    assert "strategy:channel_breakout" not in items  # active strategies trade scheduled size
    cells = [item for item in out["items"] if item["kind"] == "regime" and item["strategy_type"] == "rule_ibs"]
    assert cells and sum(cell["trades"] for cell in cells) == 4 and all(cell["minimum"] == 5 for cell in cells)
    shadow = items["shadow:channel_breakout:market_gate:sma50"]
    assert shadow["trades"] == 7 and shadow["overlap"] == 9 and shadow["needed"] == 13 and shadow["wins"] == 4
    assert "shadow:channel_breakout:market_gate:sma20" not in items  # the live value is not a candidate
    change = items["experiment:abc123"]
    assert change["trades"] == 2 and change["needed"] == 18 and change["expectancy_bps"] == 7.0
    news = items["news"]
    assert news["symbols"] == ["NVDA"] and news["trades"] == 1 and news["needed"] == xb.NEWS_MIN_TRADES - 1
    assert out["items"][0]["needed"] == max(item["needed"] for item in out["items"])  # scarcest first
    assert out["totals"]["waiting"] == len(out["items"]) and out["paper_only"] is True


def test_evidence_map_names_every_learning_strategy_even_without_records(db, monkeypatch):  # noqa: F811
    _strategy(db, "rule_new", parameters=DISCOVERED, status="paper_trading_candidate")
    monkeypatch.setattr(xb, "strategy_items", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("down")))
    out = xb.evidence_map(db, now=NOW)
    [item] = [item for item in out["items"] if item["kind"] == "strategy"]
    assert item["key"] == "strategy:rule_new" and item["needed"] == xb.STRATEGY_MIN_TRADES and item["unread"] is True


# ---------------------------------------------------------------- priority

def _items(**needed_by_strategy):
    return [xb.make_item("strategy", f"strategy:{name}", name, strategy_type=name, trades=xb.STRATEGY_MIN_TRADES - needed,
                         wins=0, minimum=xb.STRATEGY_MIN_TRADES, expectancy_bps=expectancy)
            for name, (needed, expectancy) in needed_by_strategy.items()]


def _entry(symbol, strategy_type, price=100.0, expectancy=None, news=False):
    return {"symbol": symbol, "strategy_type": strategy_type, "price": price, "exploring": not news, "news": news,
            "expectancy_bps": expectancy}


def test_priority_weighs_trades_needed_by_potential_and_discounts_lots_in_flight():
    items = _items(scarce=(28, None), rich=(28, 100.0), known=(4, 50.0), losing=(28, -300.0))
    scarce = xb.priority(xb.items_for_entry(items, "scarce"), expectancy_bps=None, in_flight=0)
    rich = xb.priority(xb.items_for_entry(items, "rich"), expectancy_bps=100.0, in_flight=0)
    known = xb.priority(xb.items_for_entry(items, "known"), expectancy_bps=50.0, in_flight=0)
    losing = xb.priority(xb.items_for_entry(items, "losing"), expectancy_bps=-300.0, in_flight=0)
    busy = xb.priority(xb.items_for_entry(items, "rich"), expectancy_bps=100.0, in_flight=3)
    assert scarce["score"] == 28.0 and rich["score"] == 56.0 and known["score"] == 6.0
    assert losing["score"] == 14.0  # a losing backtest halves the weight at worst
    assert busy["score"] == 14.0 and busy["in_flight"] == 3
    assert xb.potential_weight(None) == 1.0 and xb.potential_weight(500.0) == 2.0 and xb.potential_weight(-80.0) == 0.5


def test_entries_are_taken_in_priority_order_until_the_budget_is_spent():
    items = _items(scarce=(28, None), rich=(28, 100.0), known=(4, 50.0))
    entries = [_entry("AAPL", "known"), _entry("MSFT", "scarce"), _entry("NVDA", "rich"), _entry("QQQ", "rich")]
    with patch.object(settings, "paper_exploration_risk_fraction", 0.0025), \
         patch.object(settings, "exploration_lift_max", 2.0):
        # 750 covers the rich entry's lifted lot (5 shares, 500) and one learning
        # lot (2.5 shares rounded down to 2, 200), with 50 left: not another lot.
        decisions = xb.rank_entries(entries, items, equity=Decimal("100000"), budget=Decimal("750"), spent=Decimal("0"))
    by_rank = sorted(decisions.values(), key=lambda row: row["rank"])
    assert [(row["symbol"], row["status"]) for row in by_rank] == [
        ("NVDA", "taken"), ("MSFT", "taken"), ("QQQ", "skipped"), ("AAPL", "skipped")]
    # The rich, far-from-minimum entry is lifted to 2x; each lot in flight
    # discounts the next lot of the same strategy, so MSFT (scarce, 28 x 1 / 1)
    # outranks the second rich entry (27 x 2 / 2).
    assert by_rank[0]["multiplier"] == 2.0 and Decimal(by_rank[0]["estimated_notional"]) == Decimal("500")
    assert by_rank[1]["priority"] == 28.0 and Decimal(by_rank[1]["estimated_notional"]) == Decimal("200")
    assert by_rank[2]["priority"] == 27.0 and by_rank[2]["multiplier"] == 1.0
    assert by_rank[2]["reason"].startswith("session learning budget spent") and Decimal(by_rank[2]["budget_remaining"]) == 50
    assert by_rank[3]["symbol"] == "AAPL" and by_rank[3]["priority"] == 6.0


def test_an_already_spent_session_skips_everything_and_a_roomy_one_takes_all():
    items = _items(a=(28, None), b=(28, None))
    entries = [_entry("AAPL", "a"), _entry("MSFT", "b")]
    with patch.object(settings, "paper_exploration_risk_fraction", 0.0025):
        spent = xb.rank_entries(entries, items, equity=Decimal("100000"), budget=Decimal("1000"), spent=Decimal("900"))
        roomy = xb.rank_entries(entries, items, equity=Decimal("100000"), budget=Decimal("1000"), spent=Decimal("0"))
    assert {row["status"] for row in spent.values()} == {"skipped"}
    assert {row["status"] for row in roomy.values()} == {"taken"}


# ------------------------------------------------------------------- lift

def test_lift_needs_a_far_item_with_a_positive_backtest():
    far = xb.make_item("strategy", "strategy:x", "x", strategy_type="x", trades=5, wins=3, minimum=30, expectancy_bps=20.0)
    near = dict(far, trades=15, needed=15)
    no_edge = dict(far, expectancy_bps=None)
    losing = dict(far, expectancy_bps=-5.0)
    with patch.object(settings, "exploration_lift_max", 2.0):
        assert xb.lift_multiplier(far) == {"multiplier": 2.0, "reason": xb.LIFT_FAR}
        assert xb.lift_multiplier(near)["multiplier"] == 1.0 and xb.lift_multiplier(near)["reason"] == xb.LIFT_NEAR
        assert xb.lift_multiplier(no_edge)["multiplier"] == 1.0 and xb.lift_multiplier(losing)["multiplier"] == 1.0
        assert xb.lift_multiplier(None)["multiplier"] == 1.0
    with patch.object(settings, "exploration_lift_max", 1.5):
        assert xb.lift_multiplier(far)["multiplier"] == 1.5
    assert xb.lift_multiplier(far, max_lift=5.0)["multiplier"] == 2.0  # never above 2x


def test_apply_lift_never_exceeds_the_scheduled_size():
    assert xb.apply_lift(Decimal("2.5"), 2.0, Decimal("10")) == Decimal("5")
    assert xb.apply_lift(Decimal("2.5"), 2.0, Decimal("3")) == Decimal("3")
    assert xb.apply_lift(Decimal("2.5"), 2.0, None) == Decimal("5")
    assert xb.apply_lift(Decimal("2.5"), 1.0, Decimal("1")) == Decimal("1")


def test_estimated_notional_follows_whole_share_rounding_and_the_one_share_floor():
    with patch.object(settings, "paper_exploration_risk_fraction", 0.0025), \
         patch.object(settings, "paper_min_one_share_equity_fraction", 0.015), \
         patch.object(settings, "paper_min_share_round_up", 0.5):
        assert xb.estimate_notional(Decimal("100000"), Decimal("100")) == Decimal("200")       # 2 whole shares
        assert xb.estimate_notional(Decimal("100000"), Decimal("100"), multiplier=2.0) == Decimal("500")
        assert xb.estimate_notional(Decimal("100000"), Decimal("600")) == Decimal("600")       # one-share floor
        assert xb.estimate_notional(Decimal("100000"), Decimal("400")) == Decimal("400")       # rounds up from 0.625
        assert xb.estimate_notional(Decimal("100000"), Decimal("2000")) == Decimal("0")        # above the floor cap
        assert xb.estimate_notional(Decimal("0"), Decimal("100")) == Decimal("0")


def test_the_ledger_admits_a_lifted_lot_only_from_the_signal_record():
    with patch.object(settings, "exploration_lift_max", 2.0):
        assert _exploration_lift(SimpleNamespace(features=None)) == Decimal("1")
        assert _exploration_lift(SimpleNamespace(features={"exploration": {"multiplier": 2.0}})) == Decimal("2")
        assert _exploration_lift(SimpleNamespace(features={"exploration": {"multiplier": 7}})) == Decimal("2")
        assert _exploration_lift(SimpleNamespace(features={"exploration": {"multiplier": 0.2}})) == Decimal("1")
        assert _exploration_lift(SimpleNamespace(features={"exploration": {"multiplier": "nan"}})) == Decimal("1")


def test_a_lifted_exploration_order_passes_the_ledger_cap_with_the_record_and_fails_without():
    harness = ExplorationLedgerTests("test_exploration_is_refused_when_disabled")
    harness.setUp()
    try:
        from sqlalchemy.orm import Session

        with Session(harness.engine) as db, ExitStack() as stack:  # noqa: F811
            for item in harness.gates():
                stack.enter_context(item)
            stack.enter_context(patch.object(settings, "exploration_lift_max", 2.0))
            account, _strategy_row, signal = harness.seed(db)
            double = harness.budget_quantity(account) * 2
            with pytest.raises(StockPaperError, match="exploration budget"):
                harness.reserve(db, signal, double)
            db.rollback()
            row = db.get(StrategySignal, signal.id)
            row.features = {**(row.features or {}), "exploration": {"multiplier": 2.0, "reason": xb.LIFT_FAR}}
            db.commit()
            order = harness.reserve(db, signal, double, key="explore-lifted")
            assert order.source == EXPLORATION_SOURCE and order.quantity == double
    finally:
        harness.tearDown()


# ------------------------------------------------------------------ cycle

def _cycle(symbols, *, budget_fraction, strategies=None, orders=None):
    """A paper cycle over ``symbols`` with a fake ledger (tests/test_paper_exploration.py pattern)."""
    strategies = strategies or [SimpleNamespace(id=3, strategy_type="macd_momentum", current_status="paper_trading_candidate")]
    account = SimpleNamespace(id=1, status="reconciled", reconciliation_required=False,
                              unexplained_residual=False, equity=Decimal("100000"))
    order = SimpleNamespace(id=9, status="accepted")
    with patch.object(settings, "paper_execution_symbols", list(symbols)), \
         patch.object(settings, "paper_exploration_enabled", True), \
         patch.object(settings, "paper_exploration_risk_fraction", 0.0025), \
         patch.object(settings, "exploration_budget_fraction", budget_fraction), \
         patch.object(settings, "exploration_lift_max", 2.0), \
         patch("app.services.stock_paper_autotrader.active_paper_account", return_value=account), \
         patch("app.services.stock_paper_autotrader._is_regular_session", return_value=True), \
         signal_source(True, side_effect=lambda db, symbol, strategy_type: {
             "signal_id": 5, "signal_action": "BUY", "reference_price": "100"}), \
         patch("app.services.stock_paper_autotrader.reserve_stock_paper_order", return_value=order) as reserve, \
         patch("app.services.stock_paper_autotrader.dispatch_reserved_order", return_value=order):
        result = run_stock_paper_signal_cycle(_Db(strategies))
    return result, reserve


def test_the_cycle_takes_learning_buys_in_priority_order_and_skips_the_rest_with_the_budget_reason():
    # A learning lot at 100 is 2.5 shares rounded down to 2 (200). 1% of
    # 100,000 = 1,000 would take all three; 0.5% takes two of the three.
    result, reserve = _cycle(["AAPL", "MSFT", "NVDA"], budget_fraction=0.005)
    by_symbol = {row["symbol"]: row for row in result["results"]}
    assert by_symbol["AAPL"]["status"] == "submitted" and by_symbol["MSFT"]["status"] == "submitted"
    assert by_symbol["NVDA"]["status"] == "skipped" and by_symbol["NVDA"]["reason"] == xb.SKIP_REASON
    assert by_symbol["NVDA"]["exploration"]["status"] == "skipped" and by_symbol["NVDA"]["exploration"]["rank"] == 3
    assert by_symbol["AAPL"]["exploration"]["status"] == "taken" and by_symbol["AAPL"]["exploration"]["multiplier"] == 1.0
    assert reserve.call_count == 2 and result["submitted_count"] == 2
    assert all(call.kwargs["source"] == EXPLORATION_SOURCE for call in reserve.call_args_list)
    summary = result["exploration_budget"]
    assert summary["budget"] == "500.00000" or Decimal(summary["budget"]) == Decimal("500")
    assert summary["taken"] == 2 and summary["skipped"] == 1 and summary["ranked"] == 3
    # The fake ledger has no attribution tables: every entry still ranks
    # against its strategy item, which then needs its full minimum.
    assert by_symbol["NVDA"]["exploration"]["items"] == ["strategy:macd_momentum"]
    assert by_symbol["NVDA"]["exploration"]["trades_needed"] == xb.STRATEGY_MIN_TRADES - 2


def test_the_cycle_lifts_a_far_candidate_with_a_positive_backtest_to_twice_the_learning_size():
    strategies = [SimpleNamespace(id=3, strategy_type="rule_ibs", current_status="paper_trading_candidate",
                                  parameters=DISCOVERED)]
    result, reserve = _cycle(["AAPL"], budget_fraction=0.01, strategies=strategies)
    [row] = result["results"]
    assert row["status"] == "submitted" and row["exploration"]["multiplier"] == 2.0
    assert row["exploration"]["reason"] == xb.LIFT_FAR and row["exploration"]["lifted_quantity"] == "5.000000"
    assert reserve.call_args.kwargs["quantity"] == Decimal("5")  # 2 x 2.5 shares, whole-share rounded


def test_the_lift_never_exceeds_the_scheduled_size_or_the_session_budget():
    strategies = [SimpleNamespace(id=3, strategy_type="rule_ibs", current_status="paper_trading_candidate",
                                  parameters=DISCOVERED)]
    # A 0.3% scheduled size caps the 2x lift (0.5%) at 3 shares.
    with patch("app.services.stock_paper_autotrader._quantity_for_buy", wraps=None) as _unused:
        pass
    from app.services import stock_paper_autotrader as auto

    real = auto._quantity_for_buy

    def tight(account, price, rules, **kwargs):
        return real(account, price, {**rules, "max_risk_per_trade": "0.003"}, **kwargs)

    with patch.object(auto, "_quantity_for_buy", side_effect=tight):
        result, reserve = _cycle(["AAPL"], budget_fraction=0.01, strategies=strategies)
    assert reserve.call_args.kwargs["quantity"] == Decimal("3")
    assert result["results"][0]["exploration"]["lifted_quantity"] == "3.000000"
    # A budget that covers the learning size only withholds the lift instead of the lot.
    result, reserve = _cycle(["AAPL"], budget_fraction=0.003, strategies=strategies)
    [row] = result["results"]
    assert row["status"] == "submitted" and row["exploration"]["multiplier"] == 1.0
    assert row["exploration"]["reason"].startswith("lift withheld")
    assert reserve.call_args.kwargs["quantity"] == Decimal("2")


def test_scheduled_entries_are_never_ranked_or_skipped_by_the_budget():
    strategies = [SimpleNamespace(id=3, strategy_type="macd_momentum", current_status="paper_trading_active")]
    account = SimpleNamespace(id=1, status="reconciled", reconciliation_required=False,
                              unexplained_residual=False, equity=Decimal("100000"))
    order = SimpleNamespace(id=9, status="accepted")
    with patch.object(settings, "paper_execution_symbols", ["AAPL", "MSFT"]), \
         patch.object(settings, "paper_exploration_enabled", True), \
         patch.object(settings, "exploration_budget_fraction", 0.0001), \
         patch("app.services.stock_paper_autotrader.active_paper_account", return_value=account), \
         patch("app.services.stock_paper_autotrader._is_regular_session", return_value=True), \
         signal_source(False, side_effect=lambda db, symbol, strategy_type: {
             "signal_id": 5, "signal_action": "BUY", "reference_price": "100"}), \
         patch("app.services.stock_paper_autotrader.reserve_stock_paper_order", return_value=order) as reserve, \
         patch("app.services.stock_paper_autotrader.dispatch_reserved_order", return_value=order):
        result = run_stock_paper_signal_cycle(_Db(strategies))
    assert reserve.call_count == 2 and result["exploration_budget"]["ranked"] == 0
    assert all("exploration" not in row for row in result["results"])


# ---------------------------------------------------------- records and views

def test_the_plan_is_recorded_once_per_picture_and_feeds_the_views(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr("app.services.stock_paper_ledger.active_paper_broker_name", lambda: "alpaca_paper")
    monkeypatch.setattr(settings, "exploration_budget_fraction", 0.01)
    _strategy(db, "rule_ibs", parameters=DISCOVERED, status="paper_trading_candidate")
    items = _items(rule_ibs=(28, 35.0))
    with patch.object(settings, "paper_exploration_risk_fraction", 0.0025):
        # 300 covers one learning lot (200; the 2x lift at 500 is withheld), not two.
        decisions = xb.rank_entries([_entry("AAPL", "rule_ibs"), _entry("MSFT", "rule_ibs")], items,
                                    equity=Decimal("100000"), budget=Decimal("300"), spent=Decimal("0"))
    plan = xb.ExplorationPlan(session="2026-11-06", budget=Decimal("300"), spent_before=Decimal("0"),
                              decisions=decisions, evidence={"as_of": NOW.isoformat(), "items": items})
    results = [{"symbol": "AAPL", "strategy": "rule_ibs", "status": "submitted"},
               {"symbol": "MSFT", "strategy": "rule_ibs", "status": "skipped", "reason": xb.SKIP_REASON}]
    assert xb.record_plan(db, plan, results, NOW) is True
    assert xb.record_plan(db, plan, results, NOW + timedelta(minutes=1)) is False  # same picture, same session
    row = xb.latest_plan_row(db)
    assert row.payload["taken"][0]["symbol"] == "AAPL" and row.payload["taken"][0]["outcome"] == "submitted"
    assert row.payload["skipped"][0]["symbol"] == "MSFT" and row.payload["evidence"]["items"][0]["key"] == "strategy:rule_ibs"

    view = xb.exploration_budget_view(db, now=NOW)
    assert view["budget"]["fraction"] == 0.01 and view["budget"]["session"] == "2026-11-06"
    assert view["evidence"]["items"][0]["key"] == "strategy:rule_ibs"
    # Soonest verdict first: the current regime cell's record (5 trades) before the strategy's own (30).
    needed = [row["trades_until_verdict"] for row in view["next_verdicts"]]
    assert needed == sorted(needed) and view["next_verdicts"][0]["kind"] == "regime"
    assert next(row for row in view["next_verdicts"] if row["key"] == "strategy:rule_ibs")["trades_until_verdict"] == 30
    assert view["today"]["taken"][0]["symbol"] == "AAPL" and view["today"]["skipped"][0]["symbol"] == "MSFT"
    assert view["latest_cycle"]["session"] == "2026-11-06" and view["paper_only"] is True

    from app.services import learning_scoreboard as board

    section = board.build_learning_scoreboard(db, now=NOW)["exploration"]
    assert section["status"] == "ok" and section["today"] == {"taken": 1, "skipped": 1}
    assert section["next_verdicts"][0]["strategy_type"] == "rule_ibs" and "next verdict: rule_ibs in" in section["detail"]
    assert section["budget"]["session"] == "2026-11-06"


def test_the_view_renders_before_any_trade_or_plan(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr("app.services.stock_paper_ledger.active_paper_broker_name", lambda: "alpaca_paper")
    view = xb.exploration_budget_view(db, now=NOW)
    assert view["evidence"]["items"] == [] and view["latest_cycle"] is None and view["today"] == {"taken": [], "skipped": []}
    assert view["budget"]["budget"] == "0" or Decimal(view["budget"]["budget"]) == 0
    from app.services import learning_scoreboard as board

    assert board.build_learning_scoreboard(db, now=NOW)["exploration"]["status"] == "waiting"


def test_viewers_can_read_the_budget_route():
    assert required_role("GET", "/stock-paper/exploration-budget") == "viewer"
    assert required_role("GET", "/stock-paper/learning-scoreboard") == "viewer"
