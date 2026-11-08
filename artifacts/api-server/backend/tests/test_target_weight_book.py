"""Slow book (``target_weight_book``): netting, tranches, passive-arm sizing,
the governed risk exemption, turnover caps, shadow seed steps, the step 2 pass
rule and modeled distributions.  Fake gateways and executors only; nothing
reaches a broker.
"""
import tempfile
from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401  register all tables
from app.core.config import settings
from app.db.base import Base
from app.models import AuditLog, IntradayBar, MarketPrice, RiskRule, Strategy, StrategySignal
from app.models.stock_paper import (
    StockPaperAccount, StockPaperFill, StockPaperOrder, StockPaperPosition, StockPaperStrategyEvidence,
)
from app.services import seed as seed_module
from app.services import target_weight_book as twb
from app.services.risk import (
    TARGET_WEIGHT_TAG, PortfolioState, StrategyState, approve_target_weight_order, approve_trade,
)
from app.services.stock_paper_ledger import (
    StockPaperError, initialize_stock_paper_account, reserve_stock_paper_order,
)
from app.services.audit import write_audit_log
from tests.test_stock_paper_bracket_exits import _account_row
from tests.test_stock_paper_ledger import FakeAlpaca

UTC = timezone.utc
ET = twb.ET
D = Decimal
# Tuesday 2026-11-10 is the second Tuesday of November (tranche 2).
REBALANCE_NOW = datetime(2026, 11, 10, 15, 30, tzinfo=UTC)   # 10:30 ET


@pytest.fixture()
def db():
    tmp = tempfile.TemporaryDirectory()
    engine = create_engine("sqlite:///" + str(Path(tmp.name) / "book.sqlite"))
    Base.metadata.create_all(engine)
    with ExitStack() as stack:
        stack.enter_context(patch.object(settings, "active_paper_broker", "alpaca_paper"))
        stack.enter_context(patch.object(settings, "paper_broker_account_id", ""))
        stack.enter_context(patch.object(settings, "target_weight_book_enabled", True))
        stack.enter_context(patch.object(settings, "passive_arm_equity_weight", 0.20))
        stack.enter_context(patch.object(settings, "passive_arm_equity_symbol", "VOO"))
        stack.enter_context(patch.object(settings, "passive_arm_tbill_symbol", "SGOV"))
        stack.enter_context(patch.object(settings, "target_weight_slot_reserve", 0.10))
        stack.enter_context(patch.object(settings, "target_weight_cash_buffer", 0.0))
        stack.enter_context(patch.object(settings, "target_weight_rebalance_weekday", 1))
        with Session(engine) as session:
            yield session
    engine.dispose()
    tmp.cleanup()


def _account(db, cash="100000", equity="100000"):
    initialize_stock_paper_account(db, FakeAlpaca(account=_account_row(cash=cash, equity=equity)))
    db.add(RiskRule(name="book", value={"max_open_positions": 20}, is_active=True))
    db.commit()
    return db.query(StockPaperAccount).one()


def _arm(db, status=twb.SLEEVE_ACTIVE):
    row = Strategy(name="Passive Arm", strategy_type=twb.PASSIVE_ARM_TYPE, is_active=True, current_status=status,
                   parameters={"sleeve_cadence": "monthly", "target_weights": {"VOO": 0.2, "SGOV": 0.8}})
    db.add(row)
    db.commit()
    return row


def _bar(db, symbol, price, at=REBALANCE_NOW):
    from app.services.intraday_data import MARKET_DATA_PROVIDER

    db.add(IntradayBar(symbol=symbol, timeframe="1m", provider=MARKET_DATA_PROVIDER, feed_class="iex",
                       opened_at=at - timedelta(minutes=2), open=D(price), high=D(price), low=D(price),
                       close=D(price), volume=100, exchange_timestamp=at - timedelta(minutes=2),
                       ingested_at=at, last_verified_at=at))
    db.commit()


class Recorder:
    """Executor double: records the netted orders the engine would send."""

    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return {"order_id": len(self.calls), "order_status": "accepted"}


class Gateway:
    def __init__(self, fractionable=True, tradable=True):
        self.fractionable, self.tradable = fractionable, tradable

    def asset(self, symbol):
        return {"symbol": symbol, "tradable": self.tradable, "fractionable": self.fractionable, "status": "active"}


def _fill(db, account, symbol, side, qty, price, at, order_source=twb.SOURCE, strategy_id=None):
    order = StockPaperOrder(account_id=account.id, client_order_id=f"sp-{symbol}-{at.timestamp()}-{side}",
                            symbol=symbol, side=side, quantity=D(qty), order_type="limit", limit_price=D(price),
                            status="filled", source=order_source, strategy_id=strategy_id)
    db.add(order)
    db.flush()
    db.add(StockPaperFill(account_id=account.id, order_id=order.id, broker_activity_id=f"act-{order.id}",
                          symbol=symbol, side=side, quantity=D(qty), price=D(price), filled_at=at, raw_payload={}))
    db.commit()
    return order


def _rebalance_row(db, *, cycle, tranche, executed_on, sleeves, prices=None, funding=False, capital="0"):
    write_audit_log(db, event_type=twb.EVENT, entity_type="target_weight_tranche", entity_id=tranche,
                    action=twb.REBALANCE_ACTION, status="planned", message="test plan",
                    payload={"cycle": cycle, "tranche": tranche, "executed_on": executed_on,
                             "sleeves": sleeves, "prices": prices or {}, "funding": funding,
                             "tranche_capital": capital})
    db.commit()


# ------------------------------------------------------------------ netting

def test_netting_sums_tranches_and_sleeves_and_splits_back_by_target():
    states = {
        1: {"sleeves": {"7": {"VOO": "10", "SGOV": "40"}, "9": {"VOO": "5"}}},
        2: {"sleeves": {"7": {"VOO": "10", "SGOV": "40"}}},
    }
    target = twb.book_target(states)
    assert target == {"VOO": D("25"), "SGOV": D("80")}
    split = twb.attribution(states)
    assert split["VOO"] == {"7": D("20") / D("25"), "9": D("5") / D("25")}
    assert split["SGOV"] == {"7": D("1")}
    orders = twb.net_orders(target, {"VOO": D("30"), "SGOV": D("50")}, {"VOO": D("500"), "SGOV": D("100")},
                            {"VOO": D("0.0001"), "SGOV": D("1")}, min_notional=D("5"))
    # One netted order per symbol, sells first.
    assert [(o["symbol"], o["side"], o["quantity"]) for o in orders] == [("VOO", "sell", D("5")), ("SGOV", "buy", D("30"))]


def test_net_orders_close_out_a_dropped_symbol_exactly_and_skip_dust():
    orders = twb.net_orders({"VOO": D("0")}, {"VOO": D("1.2345"), "SGOV": D("10")}, {"VOO": D("500"), "SGOV": D("100")},
                            {"VOO": D("1"), "SGOV": D("1")}, min_notional=D("5"))
    by_symbol = {o["symbol"]: o for o in orders}
    assert by_symbol["VOO"]["quantity"] == D("1.2345")      # whole-share rounding never strands a remainder
    assert by_symbol["SGOV"]["quantity"] == D("10") and by_symbol["SGOV"]["side"] == "sell"
    assert twb.net_orders({"VOO": D("10.01")}, {"VOO": D("10")}, {"VOO": D("100")}, {"VOO": D("0.0001")},
                          min_notional=D("5")) == []


def test_band_keeps_small_changes_and_hard_cap_only_shrinks():
    sleeves = [{"id": 7, "share": D("1"), "weights": {"VOO": D("0.2"), "SGOV": D("0.8")}}]
    prices = {"VOO": D("100"), "SGOV": D("100")}
    inc = {"VOO": D("1"), "SGOV": D("1")}
    old = {"7": {"VOO": D("50"), "SGOV": D("195")}}
    plan = twb.plan_tranche(capital=D("25000"), sleeves=sleeves, prices=prices, old=old, band=D("0.02"), increments=inc)
    # VOO 50 -> 50 (no change); SGOV 195 -> 200 is $500 = 2% of the tranche: trades at the band edge.
    assert plan["shares"]["7"]["SGOV"] == D("200")
    wide = twb.plan_tranche(capital=D("25000"), sleeves=sleeves, prices=prices, old=old, band=D("0.04"), increments=inc)
    assert wide["shares"]["7"]["SGOV"] == D("195") and wide["decisions"]["SGOV"]["decision"] == "inside_band"
    capped = twb.plan_tranche(capital=D("20000"), sleeves=sleeves, prices=prices, old={"7": {"VOO": D("30"), "SGOV": D("195")}},
                              band=D("0"), increments=inc, hard_cap=True)
    assert capped["shares"]["7"]["VOO"] == D("30")            # a buy is refused at the hard cap
    assert capped["shares"]["7"]["SGOV"] == D("160")          # a reduction still goes out


# ---------------------------------------------------------------- schedule

def test_four_tranches_one_week_each_and_holiday_moves_within_its_week():
    dates = twb.tranche_dates(2026, 11, weekday=1)
    assert dates == {1: date(2026, 11, 3), 2: date(2026, 11, 10), 3: date(2026, 11, 17), 4: date(2026, 11, 24)}
    assert twb.due_tranche(date(2026, 11, 10), weekday=1) == 2
    assert twb.due_tranche(date(2026, 11, 12), weekday=1) == 2      # later session in the same week
    assert twb.due_tranche(date(2026, 11, 26), weekday=1) is None    # Thanksgiving: no session
    assert twb.due_tranche(date(2026, 11, 2), weekday=1) is None     # before the first tranche date
    # A Thursday weekday: Thanksgiving tranche 4 (2026-11-26) moves to Friday.
    assert twb.tranche_dates(2026, 11, weekday=3)[4] == date(2026, 11, 26)
    assert twb.due_tranche(date(2026, 11, 27), weekday=3) == 4


def test_rebalance_plans_only_the_due_tranche_once_and_records_its_state(db):
    account = _account(db)
    arm = _arm(db)
    _bar(db, "VOO", "500")
    _bar(db, "SGOV", "100")
    recorder = Recorder()
    result = twb.run_rebalance(db, now=REBALANCE_NOW, executor=recorder, gateway=Gateway())
    assert result["tranche"] == 2 and result["planned"]
    rows = twb.tranche_rows(db)
    assert len(rows) == 1 and rows[0].payload["tranche"] == 2 and rows[0].payload["funding"] is True
    # A stale quote never sizes anything.
    stale = twb.run_rebalance(db, now=REBALANCE_NOW + timedelta(hours=1), executor=recorder, gateway=Gateway())
    assert stale["status"] == "blocked" and "fresh reference price" in stale["reason"]
    # Second run that day: no new plan, only what is still missing (here: everything, executor is a double).
    first_calls = len(recorder.calls)
    _bar(db, "VOO", "500", at=REBALANCE_NOW + timedelta(hours=1))
    _bar(db, "SGOV", "100", at=REBALANCE_NOW + timedelta(hours=1))
    again = twb.run_rebalance(db, now=REBALANCE_NOW + timedelta(hours=1), executor=recorder, gateway=Gateway())
    assert "planned" not in again and len(twb.tranche_rows(db)) == 1
    assert len(recorder.calls) == 2 * first_calls
    assert {call["key"].rsplit(":", 1)[0] for call in recorder.calls[:first_calls]} == \
        {call["key"].rsplit(":", 1)[0] for call in recorder.calls[first_calls:]}
    # Next day of the same week: tranche 2 is done for this month.
    later = twb.run_rebalance(db, now=REBALANCE_NOW + timedelta(days=1), executor=recorder, gateway=Gateway())
    assert later["status"] == "skipped" and "already rebalanced" in later["reason"]
    assert all(call["strategy_id"] == arm.id for call in recorder.calls)
    assert all(call["key"].startswith("tw:2026-11:t2:") for call in recorder.calls)


# ------------------------------------------------------ passive-arm sizing

def test_passive_arm_takes_only_free_capital_in_its_equity_weight(db):
    account = _account(db, cash="70000", equity="100000")
    _arm(db)
    # A slot strategy (IBS in SPY) holds 30% of equity: the book never uses it.
    db.add(StockPaperPosition(account_id=account.id, symbol="SPY", quantity=D("60"), market_value=D("30000"),
                              current_price=D("500"), observed_at=REBALANCE_NOW, raw_payload={}))
    db.commit()
    _bar(db, "VOO", "500")
    _bar(db, "SGOV", "100")
    capital = twb.capital_plan(db, account, {"VOO", "SGOV"}, {"VOO": D("500"), "SGOV": D("100")})
    # 100k equity - 30k slot holdings - 10k slot reserve = 60k for the book.
    assert capital["book_capital"] == D("60000")
    recorder = Recorder()
    twb.run_rebalance(db, now=REBALANCE_NOW, executor=recorder, gateway=Gateway())
    plan = twb.tranche_rows(db)[-1].payload
    assert D(plan["tranche_capital"]) == D("15000")       # a quarter of the free capital
    shares = plan["sleeves"][str(db.query(Strategy).filter_by(strategy_type="passive_arm").one().id)]
    assert D(shares["VOO"]) * 500 == D("3000")            # 20% equity weight of the arm
    assert D(shares["SGOV"]) * 100 == D("12000")
    buys = {call["symbol"]: call for call in recorder.calls}
    assert set(buys) == {"VOO", "SGOV"} and all(call["side"] == "buy" for call in recorder.calls)
    # Marketable limit: the decision price plus the collar.
    assert buys["VOO"]["limit"] == D("501.00")
    # Nothing is bought beyond free cash (70k cash covers 15k).
    assert sum(call["quantity"] * call["limit"] for call in recorder.calls) <= D("70000")


def test_passive_arm_buys_are_bounded_by_cash_and_never_use_index_etfs(db):
    account = _account(db, cash="2000", equity="100000")
    _arm(db)
    _bar(db, "VOO", "500")
    _bar(db, "SGOV", "100")
    recorder = Recorder()
    twb.run_rebalance(db, now=REBALANCE_NOW, executor=recorder, gateway=Gateway(fractionable=False))
    assert sum(call["quantity"] * call["limit"] for call in recorder.calls) <= D("2000")
    assert all(call["quantity"] == call["quantity"].to_integral_value() for call in recorder.calls)
    assert twb.book_symbols(db) == frozenset({"VOO", "SGOV"})
    with pytest.raises(ValueError):
        settings.__class__(passive_arm_equity_symbol="SPY")


def test_untradable_symbol_blocks_the_rebalance(db):
    _account(db)
    _arm(db)
    _bar(db, "VOO", "500")
    _bar(db, "SGOV", "100")
    recorder = Recorder()
    result = twb.run_rebalance(db, now=REBALANCE_NOW, executor=recorder, gateway=Gateway(tradable=False))
    assert result["status"] == "blocked" and recorder.calls == []


# ------------------------------------------------- risk exemption scope

def _portfolio(**exposures):
    return PortfolioState(open_positions_by_symbol=exposures, total_equity=100_000)


def test_exemption_lifts_only_the_two_caps_for_tagged_orders():
    order = {"symbol": "VOO", "action": "BUY", "order_tag": TARGET_WEIGHT_TAG, "proposed_exposure": 0.20}
    assert approve_target_weight_order(order, _portfolio(), exemption_active=True)[0]
    # Without the governance action in force the caps apply to target-weight orders too.
    ok, reason = approve_target_weight_order(order, _portfolio(), exemption_active=False)
    assert not ok and "Risk-per-trade" in reason
    # The gross-exposure cap is kept.
    ok, reason = approve_target_weight_order(order, _portfolio(SGOV=0.85), exemption_active=True)
    assert not ok and "gross exposure" in reason
    # An untagged order is refused here and still hits the caps in approve_trade.
    untagged = {k: v for k, v in order.items() if k != "order_tag"}
    assert not approve_target_weight_order(untagged, _portfolio(), exemption_active=True)[0]
    scheduled = untagged | {"strategy": "1", "confidence": 0.9}
    ok, reason = approve_trade(scheduled | {"order_tag": TARGET_WEIGHT_TAG}, _portfolio(), StrategyState())
    assert not ok and "symbol exposure" in reason


def _ledger_gates(stack, now):
    for item in (
        patch("app.services.stock_paper_ledger.session_bounds",
              return_value=(now - timedelta(hours=1), now + timedelta(hours=1))),
        patch("app.services.stock_paper_ledger.feed_status", return_value={"status": "ready"}),
        patch("app.services.stock_paper_ledger._validate_reference_price"),
        patch("app.services.stock_learning_cycle.evaluate_launch_admission_prerequisites",
              return_value={"all": {"status": "pass"}}),
    ):
        stack.enter_context(item)


def test_ledger_exempts_only_target_weight_orders_for_book_symbols(db):
    _account(db)
    arm = _arm(db)
    twb.set_exemption(db, active=True, reason="test grant")
    scheduled = Strategy(name="IBS", strategy_type="ibs_mean_reversion", is_active=True,
                         current_status="paper_trading_active", parameters={})
    db.add(scheduled)
    db.flush()
    now = datetime.now(UTC)
    db.add(StockPaperStrategyEvidence(strategy_id=scheduled.id, evidence_id="ev-1", status="approved",
                                      verified_drawdown=D("0"), consecutive_losses=0, observed_at=now,
                                      expires_at=now + timedelta(days=1), provenance={}))
    signal = StrategySignal(strategy_id=scheduled.id, symbol="SPY", signal_time=now, action="BUY",
                            probability_up=D("0.8"), probability_down=D("0.2"), confidence=D("0.8"), features={})
    db.add(signal)
    db.commit()
    with ExitStack() as stack:
        _ledger_gates(stack, now)
        # 20% of equity in one target-weight BUY: exempt from the 1% and 10% caps.
        order = reserve_stock_paper_order(db, symbol="VOO", side="buy", quantity=D("200"), reference_price=D("100"),
                                          idempotency_key="tw-1", source=twb.SOURCE, strategy_id=arm.id)
        assert order.strategy_id == arm.id and order.source == twb.SOURCE

        class Accepting(FakeAlpaca):
            def submit_order(self, payload):
                self.payload = payload
                return {"id": "broker-1", "status": "accepted"}

        gateway = Accepting()
        from app.services.stock_paper_ledger import dispatch_reserved_order

        sent = dispatch_reserved_order(db, order.id, gateway)
        assert sent.status == "accepted" and gateway.payload["type"] == "limit"
        assert gateway.payload["time_in_force"] == "day" and "order_class" not in gateway.payload
        # A scheduled BUY of the same size still hits the per-BUY cap.
        with pytest.raises(StockPaperError, match="Risk-per-trade|risk policy"):
            reserve_stock_paper_order(db, symbol="SPY", side="buy", quantity=D("200"), reference_price=D("100"),
                                      idempotency_key="sched-1", source="scheduled_paper_signal", signal_id=signal.id)
        # The tag cannot be borrowed: a target-weight order on a slot symbol, or naming a non-sleeve row.
        with pytest.raises(StockPaperError, match="book's own symbols"):
            reserve_stock_paper_order(db, symbol="SPY", side="buy", quantity=D("10"), reference_price=D("100"),
                                      idempotency_key="tw-2", source=twb.SOURCE, strategy_id=arm.id)
        with pytest.raises(StockPaperError, match="sleeve"):
            reserve_stock_paper_order(db, symbol="VOO", side="buy", quantity=D("10"), reference_price=D("100"),
                                      idempotency_key="tw-3", source=twb.SOURCE, strategy_id=scheduled.id)
        with pytest.raises(StockPaperError, match="explicit attribution"):
            reserve_stock_paper_order(db, symbol="SPY", side="buy", quantity=D("1"), reference_price=D("100"),
                                      idempotency_key="sched-2", source="scheduled_paper_signal",
                                      strategy_id=scheduled.id, signal_id=signal.id)
        # Revoked: the same target-weight BUY is held to the caps again.
        twb.set_exemption(db, active=False, reason="test revoke")
        db.commit()
        with pytest.raises(StockPaperError, match="Risk-per-trade"):
            reserve_stock_paper_order(db, symbol="SGOV", side="buy", quantity=D("200"), reference_price=D("100"),
                                      idempotency_key="tw-4", source=twb.SOURCE, strategy_id=arm.id)


def test_ledger_keeps_the_gross_cap_for_target_weight_orders(db):
    _account(db, cash="100000", equity="100000")
    arm = _arm(db)
    twb.set_exemption(db, active=True, reason="test grant")
    db.commit()
    with ExitStack() as stack:
        _ledger_gates(stack, datetime.now(UTC))
        reserve_stock_paper_order(db, symbol="SGOV", side="buy", quantity=D("900"), reference_price=D("100"),
                                  idempotency_key="tw-a", source=twb.SOURCE, strategy_id=arm.id)
        with pytest.raises(StockPaperError, match="gross exposure|unavailable cash"):
            reserve_stock_paper_order(db, symbol="VOO", side="buy", quantity=D("200"), reference_price=D("100"),
                                      idempotency_key="tw-b", source=twb.SOURCE, strategy_id=arm.id)


# ---------------------------------------------------------------- turnover

def test_turnover_widens_bands_above_soft_and_refuses_buys_at_hard_cap(db):
    account = _account(db)
    arm = _arm(db)
    _bar(db, "VOO", "500")
    _bar(db, "SGOV", "100")
    # 1.8M traded in the year = 9x one-sided on 100k: above the soft budget (8).
    _fill(db, account, "SGOV", "buy", "9000", "100", REBALANCE_NOW - timedelta(days=30), strategy_id=arm.id)
    _fill(db, account, "SGOV", "sell", "9000", "100", REBALANCE_NOW - timedelta(days=29), strategy_id=arm.id)
    usage = twb.turnover(db, account, REBALANCE_NOW)
    assert usage["state"] == "above_soft_budget" and usage["trailing_12m"] == pytest.approx(9.0)
    assert twb.band_in_force(usage["state"]) == D(str(settings.target_weight_rebalance_band)) * 2
    # Another 200k one-sided: 11x, at the hard cap.
    _fill(db, account, "SGOV", "buy", "2000", "100", REBALANCE_NOW - timedelta(days=20), strategy_id=arm.id)
    _fill(db, account, "SGOV", "sell", "2000", "100", REBALANCE_NOW - timedelta(days=19), strategy_id=arm.id)
    assert twb.turnover(db, account, REBALANCE_NOW)["state"] == "at_hard_cap"
    # The book holds VOO above its target: at the hard cap only that reduction goes out.
    db.add(StockPaperPosition(account_id=account.id, symbol="VOO", quantity=D("100"), market_value=D("50000"),
                              current_price=D("500"), observed_at=REBALANCE_NOW, raw_payload={}))
    db.commit()
    _rebalance_row(db, cycle="2026-11", tranche=1, executed_on="2026-11-03",
                   sleeves={str(arm.id): {"VOO": "20", "SGOV": "0"}})
    recorder = Recorder()
    result = twb.run_rebalance(db, now=REBALANCE_NOW, executor=recorder, gateway=Gateway())
    assert result["status"] == "complete"
    assert recorder.calls and all(call["side"] == "sell" for call in recorder.calls)
    assert twb.tranche_rows(db)[-1].payload["hard_cap"] is True


# ------------------------------------------------------------- shadow seed

def _seed(db):
    seed_module.seed_defaults(db)
    return {row.strategy_type: row for row in db.query(Strategy).all()}


def test_seed_shadows_broad_technicals_and_rsi2_stock_leg_once(db):
    rows = _seed(db)
    for slug in ("ensemble", "rsi_mean_reversion", "bollinger_mean_reversion", "channel_breakout", "trend_pullback"):
        assert rows[slug].current_status == "shadow", slug
    # Already retired or paused rows are a decision that stands.
    assert rows["macd_momentum"].current_status == "retired"
    assert rows["moving_average_crossover"].current_status in {"retired", "shadow"}
    # IBS is unchanged by the slow book; step 4 (reports/short-horizon-reverdict-20261107.md) shadows it.
    assert rows["ibs_mean_reversion"].current_status == "shadow"
    rsi2 = rows["rsi2_pullback"].parameters
    assert "SPY" in rsi2["symbol_scope"] and "AAPL" in rsi2["shadow_symbols"]
    assert not set(rsi2["shadow_symbols"]) & {"SPY", "QQQ", "IWM", "DIA"}
    arm = rows["passive_arm"]
    assert arm.current_status == twb.SLEEVE_ACTIVE and arm.parameters["sleeve_cadence"] == "monthly"
    assert twb.exemption_active(db)
    assert {"VOO", "SGOV"} <= {row[0] for row in db.query(app.models.Asset.symbol).all()}
    # An operator brings one strategy back and narrows RSI(2): a later seed pass never overrides it.
    rows["trend_pullback"].current_status = "research"
    rows["rsi2_pullback"].parameters = dict(rsi2) | {"shadow_symbols": []}
    twb.set_exemption(db, active=False, reason="owner revoked")
    db.commit()
    rows = _seed(db)
    assert rows["trend_pullback"].current_status == "research"
    assert rows["rsi2_pullback"].parameters["shadow_symbols"] == []
    assert not twb.exemption_active(db)
    assert db.query(Strategy).filter_by(strategy_type="passive_arm").count() == 1


def test_shadow_strategy_records_signals_but_never_enters():
    from tests.test_stock_paper_autotrader import _Db, _account as cycle_account, signal_source

    shadow = SimpleNamespace(id=2, strategy_type="rsi_mean_reversion", current_status="shadow", parameters={})
    with patch.object(settings, "paper_execution_symbols", ["AAPL"]), \
         patch("app.services.stock_paper_autotrader.active_paper_account", return_value=cycle_account()), \
         patch("app.services.stock_paper_autotrader._is_regular_session", return_value=True), \
         patch.object(settings, "paper_exploration_enabled", True), \
         signal_source(True, return_value={"signal_id": 8, "signal_action": "BUY", "reference_price": "100"}) as observe, \
         patch("app.services.stock_paper_autotrader.reserve_stock_paper_order") as reserve:
        from app.services.stock_paper_autotrader import run_stock_paper_signal_cycle

        result = run_stock_paper_signal_cycle(_Db([shadow]))
    observe.assert_called_once()
    reserve.assert_not_called()
    assert result["results"][0]["status"] == "observed" and result["results"][0]["shadow"] is True


@pytest.mark.usefixtures("legacy_technical_exits")
def test_shadow_scope_observes_stocks_and_trades_index_etfs():
    """RSI(2)'s ``shadow_symbols`` mechanism, on a strategy without a daily-bar entry window."""
    from tests.test_stock_paper_autotrader import _Db, _account as cycle_account, signal_source

    rsi2 = SimpleNamespace(id=5, strategy_type="moving_average_crossover", current_status="paper_trading_active",
                           parameters={"shadow_symbols": ["AAPL"]})
    order = SimpleNamespace(id=42, status="accepted")
    with patch.object(settings, "paper_execution_symbols", ["AAPL", "SPY"]), \
         patch("app.services.stock_paper_autotrader.active_paper_account", return_value=cycle_account()), \
         patch("app.services.stock_paper_autotrader._is_regular_session", return_value=True), \
         signal_source(False, return_value={"signal_id": 9, "signal_action": "BUY", "reference_price": "100"}), \
         patch("app.services.stock_paper_autotrader.reserve_stock_paper_order", return_value=order) as reserve, \
         patch("app.services.stock_paper_autotrader.dispatch_reserved_order", return_value=order):
        from app.services.stock_paper_autotrader import run_stock_paper_signal_cycle

        result = run_stock_paper_signal_cycle(_Db([rsi2]))
    by_symbol = {row["symbol"]: row for row in result["results"]}
    assert by_symbol["AAPL"]["status"] == "observed" and "shadow scope" in by_symbol["AAPL"]["reason"]
    assert reserve.call_count == 1 and reserve.call_args.kwargs["symbol"] == "SPY"


def test_slot_cycle_never_trades_book_symbols():
    from tests.test_stock_paper_autotrader import _Db, _account as cycle_account, signal_source

    strategy = SimpleNamespace(id=1, strategy_type="ibs_mean_reversion", current_status="paper_trading_active")
    with patch.object(settings, "paper_execution_symbols", ["SPY", "VOO"]), \
         patch("app.services.stock_paper_autotrader.active_paper_account", return_value=cycle_account()), \
         patch("app.services.stock_paper_autotrader._is_regular_session", return_value=True), \
         signal_source(False, return_value={"signal_id": 1, "signal_action": "HOLD", "reference_price": "100"}):
        from app.services.stock_paper_autotrader import run_stock_paper_signal_cycle

        result = run_stock_paper_signal_cycle(_Db([strategy]))
    assert result["symbols"] == ["SPY"] and result["target_weight_book_symbols"] == ["SGOV", "VOO"]


# ------------------------------------------------- pass rule and pause

def _pnl_row(db, session, book_return, replay_return):
    write_audit_log(db, event_type=twb.EVENT, entity_type="target_weight_book", action=twb.PNL_ACTION,
                    status="recorded", message="test",
                    payload={"session": session, "book": {"return": book_return}, "replay": {"return": replay_return}})


def _complete_cycle(db, arm, cycle="2026-11", passed=True):
    for tranche, day in zip(twb.TRANCHES, ("2026-11-03", "2026-11-10", "2026-11-17", "2026-11-24")):
        _rebalance_row(db, cycle=cycle, tranche=tranche, executed_on=day,
                       sleeves={str(arm.id): {"VOO": "10", "SGOV": "100"}})
        row = twb.tranche_rows(db)[-1]
        write_audit_log(db, event_type=twb.EVENT, entity_type="target_weight_tranche", entity_id=tranche,
                        action=twb.CHECK_ACTION, status="passed" if passed else "failed", message="test",
                        payload={"rebalance_audit_id": row.id, "passed": passed, "tranche": tranche})
    db.commit()


def test_cycle_verdict_passes_and_counts_toward_the_three_cycle_clock(db):
    account = _account(db)
    arm = _arm(db)
    _complete_cycle(db, arm)
    _pnl_row(db, "2026-11-30", 0.0010, 0.0009)
    db.commit()
    verdicts = twb.evaluate_cycles(db, account, date(2026, 11, 30), now=datetime(2026, 12, 1, tzinfo=UTC))
    assert len(verdicts) == 1 and verdicts[0]["passed"] and verdicts[0]["counts_toward_clock"]
    assert verdicts[0]["clock_cycles_passed"] == 1 and not verdicts[0]["pass_rule_met"]
    assert not twb.engine_state(db)["paused"]
    # Judged once.
    assert twb.evaluate_cycles(db, account, date(2026, 12, 1), now=datetime(2026, 12, 2, tzinfo=UTC)) == []


def test_tracking_or_weight_failure_pauses_the_engine_and_resume_restarts_the_clock(db):
    account = _account(db)
    arm = _arm(db)
    _complete_cycle(db, arm, passed=False)
    _pnl_row(db, "2026-11-30", 0.0040, 0.0010)     # 30 bps behind/ahead of the replay
    db.commit()
    verdict = twb.evaluate_cycles(db, account, date(2026, 11, 30), now=datetime(2026, 12, 1, tzinfo=UTC))[0]
    assert not verdict["passed"]
    assert any("weights" in reason for reason in verdict["reasons"])
    assert any("replay" in reason for reason in verdict["reasons"])
    state = twb.engine_state(db)
    assert state["paused"] and "replay" in state["reason"]
    # Paused: the rebalance job holds (no orders).
    _bar(db, "VOO", "500")
    _bar(db, "SGOV", "100")
    recorder = Recorder()
    held = twb.run_rebalance(db, now=REBALANCE_NOW, executor=recorder, gateway=Gateway())
    assert held["status"] == "skipped" and "paused" in held["reason"] and recorder.calls == []
    resumed = twb.resume_engine(db, reason="fixed the fill reconciliation")
    db.commit()
    state = twb.engine_state(db)
    assert not state["paused"] and state["clock_start_audit_id"] == resumed["audit_id"]
    # The next month's cycle is judged on the restarted clock: earlier plans do not count.
    view = twb.book_view(db, now=REBALANCE_NOW)
    assert view["cycles"]["clock_cycles_passed"] == 0
    assert view["engine"]["exemption_active"] is False


def test_pass_rule_met_after_three_complete_cycles(db):
    account = _account(db)
    arm = _arm(db)
    for cycle, days, last in (("2026-11", ("03", "10", "17", "24"), date(2026, 11, 30)),
                              ("2026-12", ("01", "08", "15", "22"), date(2026, 12, 31)),
                              ("2027-01", ("05", "12", "19", "26"), date(2027, 1, 29))):
        for tranche, day in zip(twb.TRANCHES, days):
            _rebalance_row(db, cycle=cycle, tranche=tranche, executed_on=f"{cycle}-{day}",
                           sleeves={str(arm.id): {"VOO": "10"}})
            row = twb.tranche_rows(db)[-1]
            write_audit_log(db, event_type=twb.EVENT, entity_type="target_weight_tranche", entity_id=tranche,
                            action=twb.CHECK_ACTION, status="passed", message="test",
                            payload={"rebalance_audit_id": row.id, "passed": True, "tranche": tranche})
        _pnl_row(db, last.isoformat(), 0.001, 0.001)
        db.commit()
        verdicts = twb.evaluate_cycles(db, account, last, now=datetime.combine(last, datetime.min.time(), tzinfo=UTC))
    assert verdicts[-1]["clock_cycles_passed"] == 3 and verdicts[-1]["pass_rule_met"]


# ---------------------------------------------------------- distributions

def test_distribution_accrual_books_ex_date_times_shares_once(db):
    account = _account(db)
    arm = _arm(db)
    bought = datetime(2026, 11, 10, 15, 35, tzinfo=UTC)
    _fill(db, account, "SGOV", "buy", "100", "100.40", bought, strategy_id=arm.id)
    _fill(db, account, "VOO", "buy", "4", "500", bought, strategy_id=arm.id)
    _rebalance_row(db, cycle="2026-11", tranche=2, executed_on="2026-11-10",
                   sleeves={str(arm.id): {"SGOV": "100", "VOO": "4"}})

    def fetcher(symbol):
        return {"SGOV": [(date(2026, 10, 1), D("0.40")), (date(2026, 11, 13), D("0.35"))],
                "VOO": [(date(2026, 12, 20), D("1.80"))]}[symbol]

    result = twb.accrue_distributions(db, account, date(2026, 11, 30), fetcher=fetcher)
    assert result["recorded"] == 1          # Oct ex-date predates the holding; Dec is in the future
    payload = result["distributions"][0]
    assert payload["symbol"] == "SGOV" and payload["ex_date"] == "2026-11-13"
    assert D(payload["amount"]) == D("35.0000") and payload["sleeves"] == {str(arm.id): "35"}
    assert twb.accrue_distributions(db, account, date(2026, 11, 30), fetcher=fetcher)["recorded"] == 0
    # The ex-date's P&L includes the modeled distribution on both paper and replay.
    for symbol, prev, close in (("SGOV", "100.50", "100.20"), ("VOO", "500", "505")):
        db.add(MarketPrice(symbol=symbol, price_date=date(2026, 11, 12), close=D(prev), source="yfinance"))
        db.add(MarketPrice(symbol=symbol, price_date=date(2026, 11, 13), close=D(close), source="yfinance"))
    db.commit()
    pnl = twb.daily_pnl(db, account, date(2026, 11, 13))
    assert D(pnl["symbols"]["SGOV"]["distribution"]) == D("35")
    sgov_pnl = D("100") * (D("100.20") - D("100.50")) + D("35")
    assert D(pnl["symbols"]["SGOV"]["pnl"]) == sgov_pnl
    assert pnl["passive_arm"]["strategy_id"] == arm.id
    assert pnl["book"]["return"] == pytest.approx(pnl["replay"]["return"])
    assert pnl["sleeves"][str(arm.id)]["distributions"] == pytest.approx(35.0)


def test_daily_pnl_replay_differs_only_by_execution_against_the_decision_price(db):
    account = _account(db)
    arm = _arm(db)
    _rebalance_row(db, cycle="2026-11", tranche=2, executed_on="2026-11-10",
                   sleeves={str(arm.id): {"VOO": "10"}}, prices={"VOO": "500"})
    _fill(db, account, "VOO", "buy", "10", "500.50", datetime(2026, 11, 10, 15, 35, tzinfo=UTC), strategy_id=arm.id)
    db.add(MarketPrice(symbol="VOO", price_date=date(2026, 11, 9), close=D("498"), source="yfinance"))
    db.add(MarketPrice(symbol="VOO", price_date=date(2026, 11, 10), close=D("502"), source="yfinance"))
    db.commit()
    pnl = twb.daily_pnl(db, account, date(2026, 11, 10))
    assert D(pnl["symbols"]["VOO"]["pnl"]) == D("10") * (D("502") - D("500.50"))
    assert D(pnl["symbols"]["VOO"]["replay_pnl"]) == D("10") * (D("502") - D("500"))
    assert pnl["book"]["return"] < pnl["replay"]["return"]


def test_daily_job_benchmarks_fills_against_delayed_sip_and_checks_the_tranche(db):
    account = _account(db)
    arm = _arm(db)
    filled_at = datetime(2026, 11, 10, 15, 35, 20, tzinfo=UTC)
    _fill(db, account, "VOO", "buy", "10", "500.50", filled_at, strategy_id=arm.id)
    _fill(db, account, "SPY", "buy", "2", "600", filled_at, order_source="scheduled_paper_signal")
    _rebalance_row(db, cycle="2026-11", tranche=2, executed_on="2026-11-10",
                   sleeves={str(arm.id): {"VOO": "10"}}, prices={"VOO": "500"})

    class Bars:
        def bars(self, symbols, start, end, *, feed):
            assert feed == "sip" and symbols == ["VOO"]          # only the book's fills
            return {"VOO": [
                {"opened_at": datetime(2026, 11, 10, 15, 35, tzinfo=UTC), "open": 500.2, "close": 500.3, "vwap": 500.25},
                {"opened_at": datetime(2026, 11, 10, 20, 59, tzinfo=UTC), "open": 501.0, "close": 502.0, "vwap": 501.5},
            ]}

    result = twb.run_daily(db, now=datetime(2026, 11, 10, 22, 45, tzinfo=UTC), dividend_fetcher=lambda _s: [],
                           bars_client=Bars())
    assert result["status"] == "complete" and result["session"] == "2026-11-10"
    bench = db.query(AuditLog).filter_by(event_type=twb.EVENT, action=twb.BENCHMARK_ACTION).one().payload
    fill = bench["fills"][0]
    assert bench["sip_close"] == {"VOO": 502.0}
    assert fill["minute_bps"] == pytest.approx((500.50 - 500.25) / 500.25 * 1e4, abs=1e-3)
    assert fill["close_bps"] == pytest.approx((500.50 - 502.0) / 502.0 * 1e4, abs=1e-3)
    check = db.query(AuditLog).filter_by(event_type=twb.EVENT, action=twb.CHECK_ACTION).one().payload
    assert check["passed"] and check["worst_gap"] == 0
    # The day's closes are not imported yet: the P&L waits for the morning run.
    assert result["pnl"]["missing_closes"] == ["2026-11-10"]


def test_viewer_endpoint_is_viewer_readable():
    from app.core.security import required_role

    assert required_role("GET", "/stock-paper/target-weight-book") == "viewer"
    assert required_role("POST", "/stock-paper/target-weight-book/resume") == "admin"
    assert required_role("POST", "/stock-paper/target-weight-book/exemption") == "admin"
