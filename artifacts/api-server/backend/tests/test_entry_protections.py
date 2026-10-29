"""Entry protections: entry-only circuit breakers for autonomous paper BUYs."""
import tempfile
from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401  register all tables
from app.core.config import settings
from app.db.base import Base
from app.models.stock_paper import StockPaperAccount, StockPaperEquitySnapshot, StockPaperFill, StockPaperOrder
from app.services.entry_protections import (
    ClosedTrade,
    EntryProtections,
    ExitEvent,
    FillRecord,
    ProtectionConfig,
    _client_order_id,
    cooldown_block,
    drawdown_block,
    load_entry_protections,
    low_profit_block,
    protection_config,
    protective_stop_idempotency_key,
    reconstruct_trades,
    session_drawdown,
    stoploss_guard_block,
    time_of_day_block,
)
from app.services.intraday_data import session_bounds
from app.services.stock_paper_autotrader import run_stock_paper_signal_cycle
from tests.test_stock_paper_autotrader import _ModelDb, signal_source

UTC = timezone.utc
ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 28, 16, 0, tzinfo=UTC)  # Wed 12:00 ET, mid-session
CFG = ProtectionConfig()


def _stop(minutes_ago, strategy="macd_momentum", key=None, symbol="SPY", stop=True):
    return ExitEvent(symbol, NOW - timedelta(minutes=minutes_ago), key or f"order:{minutes_ago}", stop, (strategy,))


def _trade(pnl, hours_ago, strategy="macd_momentum", symbol="SPY"):
    closed = NOW - timedelta(hours=hours_ago)
    return ClosedTrade(symbol, strategy, closed - timedelta(hours=1), closed, Decimal(str(pnl)))


# --- configuration -------------------------------------------------------

def test_config_defaults_follow_settings_and_risk_rules_override():
    with patch.object(settings, "paper_entry_open_buffer_minutes", 15), \
         patch.object(settings, "paper_entry_close_buffer_minutes", 10):
        config = protection_config({})
        assert config == ProtectionConfig()
    with patch.object(settings, "paper_exit_cooldown_minutes", 45):
        assert protection_config({}).exit_cooldown_minutes == 45
        assert protection_config({"exit_cooldown_minutes": 5}).exit_cooldown_minutes == 5
        assert protection_config({"exit_cooldown_minutes": 0}).exit_cooldown_minutes == 0
        # Garbage or negative risk-rule values fall back to the setting.
        assert protection_config({"exit_cooldown_minutes": "x"}).exit_cooldown_minutes == 45
        assert protection_config({"exit_cooldown_minutes": -3}).exit_cooldown_minutes == 45
    assert protection_config({"session_drawdown_lock_pct": "0.05"}).session_drawdown_lock_pct == Decimal("0.05")
    assert protection_config({"session_drawdown_lock_pct": "nan"}).session_drawdown_lock_pct == Decimal("0.02")


# --- time of day -----------------------------------------------------------

def _et(day, hh, mm, ss=0):
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=ET).astimezone(UTC)


@pytest.mark.parametrize("hh,mm,ss,blocked", [
    (9, 30, 0, True), (9, 44, 59, True), (9, 45, 0, False), (12, 0, 0, False),
    (15, 49, 59, False), (15, 50, 0, True), (15, 59, 59, True), (16, 0, 0, False), (9, 29, 59, False),
])
def test_time_of_day_edges_on_a_regular_session(hh, mm, ss, blocked):
    day = date(2026, 10, 28)
    bounds = session_bounds(day)
    assert bounds[0] == _et(day, 9, 30) and bounds[1] == _et(day, 16, 0)
    outcome = time_of_day_block(_et(day, hh, mm, ss), bounds, CFG)
    assert (outcome is not None) is blocked
    if blocked:
        assert outcome["status"] == "skipped" and outcome["protection"] == "time_of_day"
        assert "time_of_day" in outcome["reason"]


def test_time_of_day_uses_the_half_day_early_close():
    day = date(2026, 11, 27)  # day after Thanksgiving: 13:00 ET close
    bounds = session_bounds(day)
    assert bounds[1] == _et(day, 13, 0)
    assert time_of_day_block(_et(day, 12, 49, 59), bounds, CFG) is None
    block = time_of_day_block(_et(day, 12, 50), bounds, CFG)
    assert block and "13:00 ET" in block["reason"]
    assert time_of_day_block(_et(day, 15, 55), bounds, CFG) is None  # closed: session gate's job


def test_time_of_day_buffers_disable_individually():
    day = date(2026, 10, 28)
    bounds = session_bounds(day)
    no_open = ProtectionConfig(entry_open_buffer_minutes=0)
    assert time_of_day_block(_et(day, 9, 31), bounds, no_open) is None
    assert time_of_day_block(_et(day, 15, 55), bounds, no_open) is not None
    no_close = ProtectionConfig(entry_close_buffer_minutes=0)
    assert time_of_day_block(_et(day, 15, 55), bounds, no_close) is None
    assert time_of_day_block(_et(day, 9, 31), None, CFG) is None


# --- stop-loss guard -------------------------------------------------------

def test_stoploss_guard_pauses_the_strategy_after_n_stops_in_window():
    events = [_stop(300), _stop(100), _stop(30)]
    block = stoploss_guard_block(events, "macd_momentum", NOW, CFG)
    assert block["protection"] == "stoploss_guard"
    assert block["protection_until"] == (NOW + timedelta(minutes=90)).isoformat()
    # Another strategy is unaffected unless the global guard is enabled.
    assert stoploss_guard_block(events, "rsi_mean_reversion", NOW, CFG) is None
    global_cfg = ProtectionConfig(stoploss_guard_global_count=3)
    assert stoploss_guard_block(events, "rsi_mean_reversion", NOW, global_cfg)["protection"] == "stoploss_guard"


def test_stoploss_guard_threshold_window_pause_and_disable():
    assert stoploss_guard_block([_stop(100), _stop(30)], "macd_momentum", NOW, CFG) is None
    # One stop outside the 6h lookback.
    assert stoploss_guard_block([_stop(400), _stop(100), _stop(30)], "macd_momentum", NOW, CFG) is None
    # Pause over: last stop 121 minutes ago.
    assert stoploss_guard_block([_stop(300), _stop(200), _stop(121)], "macd_momentum", NOW, CFG) is None
    # Non-stop exits do not count.
    events = [_stop(300), _stop(100), _stop(30, stop=False)]
    assert stoploss_guard_block(events, "macd_momentum", NOW, CFG) is None
    events = [_stop(300), _stop(100), _stop(30)]
    assert stoploss_guard_block(events, "macd_momentum", NOW, ProtectionConfig(stoploss_guard_count=0)) is None
    assert stoploss_guard_block(events, "macd_momentum", NOW, ProtectionConfig(stoploss_guard_count=4)) is None


# --- cooldown --------------------------------------------------------------

def test_cooldown_blocks_reentry_on_the_exited_symbol_only():
    events = [_stop(10, stop=False)]
    block = cooldown_block(events, "SPY", NOW, CFG)
    assert block["protection"] == "exit_cooldown"
    assert cooldown_block(events, "QQQ", NOW, CFG) is None
    assert cooldown_block([_stop(30, stop=False)], "SPY", NOW, CFG) is None  # exactly M minutes later
    assert cooldown_block(events, "SPY", NOW, ProtectionConfig(exit_cooldown_minutes=0)) is None
    assert cooldown_block(events, "SPY", NOW, ProtectionConfig(exit_cooldown_minutes=5)) is None


# --- low-profit lock ---------------------------------------------------------

def test_low_profit_lock_after_k_losing_trades():
    trades = [_trade(5, 30), _trade(-3, 20), _trade(-4, 10), _trade(1, 2)]
    block = low_profit_block(trades, "SPY", "macd_momentum", NOW, CFG)
    assert block["protection"] == "low_profit_symbol" and "-1.00" in block["reason"]
    assert low_profit_block(trades, "SPY", "rsi_mean_reversion", NOW, CFG) is None
    assert low_profit_block(trades, "QQQ", "macd_momentum", NOW, CFG) is None
    assert low_profit_block(trades[1:], "SPY", "macd_momentum", NOW, CFG) is None  # only 3 trades
    winning = [_trade(5, 30), _trade(-3, 20), _trade(-1, 10), _trade(1, 2)]
    assert low_profit_block(winning, "SPY", "macd_momentum", NOW, CFG) is None  # net 2 >= 0
    expired = [_trade(5, 60), _trade(-3, 50), _trade(-4, 40), _trade(1, 25)]
    assert low_profit_block(expired, "SPY", "macd_momentum", NOW, CFG) is None  # lock lasted 1 day
    assert low_profit_block(trades, "SPY", "macd_momentum", NOW, ProtectionConfig(low_profit_trades=0)) is None


# --- session drawdown ------------------------------------------------------

def test_session_drawdown_lock_uses_peak_since_open():
    dd = session_drawdown(Decimal("98900"), [Decimal("100000"), Decimal("101000")])
    assert dd == (Decimal("101000") - Decimal("98900")) / Decimal("101000")
    block = drawdown_block(dd, CFG, NOW + timedelta(hours=4))
    assert block["protection"] == "session_drawdown"
    assert drawdown_block(session_drawdown(Decimal("99000"), [Decimal("100000")]), CFG) is None  # 1%
    # No snapshot since the open: the prior-close equity is the baseline.
    assert session_drawdown(Decimal("97000"), [], opening_fallback=Decimal("100000")) == Decimal("0.03")
    assert session_drawdown(None, [Decimal("100000")]) is None
    assert drawdown_block(Decimal("0.5"), ProtectionConfig(session_drawdown_lock_pct=Decimal("0")), None) is None


# --- reconstruction --------------------------------------------------------

def _fill(side, qty, price, minutes_ago, strategy=None, order_id=None, stop=False, fee="0"):
    return FillRecord("SPY", side, Decimal(qty), Decimal(price), NOW - timedelta(minutes=minutes_ago),
                      Decimal(fee), order_id, strategy, stop)


def test_reconstruct_attributes_unattributed_stop_fifo_and_counts_partial_fills_once():
    fills = [
        _fill("buy", "2", "100", 200, "macd_momentum", 1, fee="1"),
        _fill("buy", "3", "100", 190, "moving_average_crossover", 2),
        # App-side protective stop: no strategy, two partial fills of one order.
        _fill("sell", "1", "97", 100, None, 9, stop=True),
        _fill("sell", "4", "97", 99, None, 9, stop=True),
    ]
    events, trades = reconstruct_trades(fills)
    assert len(events) == 1
    assert events[0].stop_out and set(events[0].strategies) == {"macd_momentum", "moving_average_crossover"}
    by_strategy = {row.strategy: row.net_pnl for row in trades}
    assert by_strategy == {"macd_momentum": Decimal("-7"), "moving_average_crossover": Decimal("-9")}


def test_reconstruct_attributed_sell_closes_its_own_lots_first():
    fills = [
        _fill("buy", "3", "100", 200, "moving_average_crossover", 1),
        _fill("buy", "2", "100", 190, "macd_momentum", 2),
        _fill("sell", "2", "110", 100, "macd_momentum", 3),
        _fill("sell", "1", "90", 50, None, 4),  # orphan history beyond lots is ignored for P&L
    ]
    events, trades = reconstruct_trades(fills)
    assert [(row.strategy, row.net_pnl) for row in trades] == [("macd_momentum", Decimal("20"))]
    assert [row.strategies for row in events] == [("macd_momentum",), ("moving_average_crossover",)]


# --- loader against a real schema -------------------------------------------

@pytest.fixture
def sqlite_db():
    with tempfile.TemporaryDirectory() as tmp:
        engine = create_engine("sqlite:///" + str(Path(tmp) / "protect.sqlite"))
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            yield db
        engine.dispose()


def _seed_account(db, equity="100000", last_equity="100000"):
    account = StockPaperAccount(broker_account_id="paper", cash=Decimal(equity), buying_power=Decimal(equity),
                                equity=Decimal(equity), last_equity=Decimal(last_equity), status="reconciled",
                                raw_payload={})
    db.add(account)
    db.flush()
    return account


def _seed_order(db, account, *, id, side, strategy_id=None, leg_role=None, client=None, at=NOW):
    db.add(StockPaperOrder(id=id, account_id=account.id, strategy_id=strategy_id, client_order_id=client or f"c{id}",
                           symbol="SPY", side=side, quantity=Decimal("1"), status="filled",
                           source="scheduled_paper_signal", leg_role=leg_role, created_at=at, updated_at=at))
    db.add(StockPaperFill(account_id=account.id, order_id=id, broker_activity_id=f"a{id}", symbol="SPY", side=side,
                          quantity=Decimal("1"), price=Decimal("100" if side == "buy" else "97"),
                          filled_at=at, raw_payload={}))


def test_loader_detects_stop_legs_and_app_side_protective_stops(sqlite_db):
    db = sqlite_db
    account = _seed_account(db)
    types = {3: "macd_momentum"}
    session_key = NOW.astimezone(ET).date().isoformat()
    for index, minutes in enumerate((300, 200, 100)):
        at = NOW - timedelta(minutes=minutes)
        _seed_order(db, account, id=10 + index * 2, side="buy", strategy_id=3, at=at - timedelta(minutes=5))
        if index < 2:
            _seed_order(db, account, id=11 + index * 2, side="sell", strategy_id=3, leg_role="stop_loss", at=at)
        else:
            client = _client_order_id(protective_stop_idempotency_key(session_key, "SPY"))
            _seed_order(db, account, id=11 + index * 2, side="sell", client=client, at=at)
    # A take-profit exit is an exit (cooldown) but not a stop-out.
    _seed_order(db, account, id=30, side="buy", strategy_id=3, at=NOW - timedelta(minutes=20))
    _seed_order(db, account, id=31, side="sell", strategy_id=3, leg_role="take_profit", at=NOW - timedelta(minutes=10))
    db.commit()

    protections = load_entry_protections(db, account, now=NOW, rules={"session_drawdown_lock_pct": 0},
                                         type_by_id=types)
    assert protections.load_error is None
    assert sum(row.stop_out for row in protections.events) == 3
    block = protections.check("SPY", "macd_momentum")
    assert block["protection"] == "stoploss_guard"
    no_guard = load_entry_protections(db, account, now=NOW, type_by_id=types,
                                      rules={"stoploss_guard_count": 0, "session_drawdown_lock_pct": 0})
    assert no_guard.check("SPY", "macd_momentum")["protection"] == "exit_cooldown"
    assert no_guard.check("QQQ", "macd_momentum") is None


def test_loader_session_drawdown_from_equity_snapshots(sqlite_db):
    db = sqlite_db
    account = _seed_account(db, equity="98000", last_equity="99000")
    opened = session_bounds(NOW.astimezone(ET).date())[0]
    for minutes, equity in ((10, "100000"), (60, "100500")):
        db.add(StockPaperEquitySnapshot(account_id=account.id, cash=Decimal(equity), equity=Decimal(equity),
                                        buying_power=Decimal(equity), observed_at=opened + timedelta(minutes=minutes),
                                        raw_payload={}))
    db.add(StockPaperEquitySnapshot(account_id=account.id, cash=Decimal("1"), equity=Decimal("200000"),
                                    buying_power=Decimal("1"), observed_at=opened - timedelta(hours=20),
                                    raw_payload={}))  # yesterday: ignored
    db.commit()
    protections = load_entry_protections(db, account, now=NOW, rules={}, type_by_id={})
    assert protections.drawdown == (Decimal("100500") - Decimal("98000")) / Decimal("100500")
    assert protections.check("SPY", "macd_momentum")["protection"] == "session_drawdown"
    relaxed = load_entry_protections(db, account, now=NOW, rules={"session_drawdown_lock_pct": "0.03"},
                                     type_by_id={})
    assert relaxed.check("SPY", "macd_momentum") is None


def test_loader_failure_fails_closed_for_entries():
    class Broken:
        rolled_back = False

        def query(self, model):
            raise RuntimeError("db down")

        def rollback(self):
            Broken.rolled_back = True

    protections = load_entry_protections(Broken(), SimpleNamespace(id=1), now=NOW, rules={}, type_by_id={})
    assert protections.load_error and Broken.rolled_back
    assert protections.check("SPY", "macd_momentum")["protection"] == "unavailable"


# --- autotrader integration: entries blocked, exits never --------------------

_MACD = SimpleNamespace(id=3, strategy_type="macd_momentum")
_CANDIDATE = SimpleNamespace(id=4, strategy_type="rsi_mean_reversion", current_status="paper_trading_candidate")


def _account():
    return SimpleNamespace(id=1, status="reconciled", reconciliation_required=False, unexplained_residual=False,
                           equity=Decimal("100000"))


def _blocking(kind):
    """An evaluator whose clock is injected (independent of the wall clock)."""
    if kind == "time_of_day":
        day = date(2026, 10, 28)
        return EntryProtections(now=_et(day, 9, 35), config=CFG, bounds=session_bounds(day))
    if kind == "session_drawdown":
        return EntryProtections(now=NOW, config=CFG, bounds=session_bounds(date(2026, 10, 28)),
                                drawdown=Decimal("0.025"))
    if kind == "stoploss_guard":
        return EntryProtections(now=NOW, config=CFG, events=[
            ExitEvent("SPY", NOW - timedelta(minutes=m), f"order:{m}", True, ("macd_momentum", "rsi_mean_reversion"))
            for m in (300, 100, 60)
        ])
    if kind == "exit_cooldown":
        return EntryProtections(now=NOW, config=CFG, events=[_stop(5, stop=False)])
    if kind == "low_profit_symbol":
        return EntryProtections(now=NOW, config=CFG,
                                trades=[_trade(-1, 9, "macd_momentum")] * 4
                                + [_trade(-1, 9, "rsi_mean_reversion")] * 4)
    if kind == "unavailable":
        return EntryProtections(now=NOW, config=CFG, load_error="RuntimeError: db down")
    raise AssertionError(kind)


KINDS = ["time_of_day", "session_drawdown", "stoploss_guard", "exit_cooldown", "low_profit_symbol", "unavailable"]


def _cycle(strategies, action, protections, *, rows=None, holdings=None, research=False):
    order = SimpleNamespace(id=42, status="accepted")
    db = _ModelDb(strategies, rows or {})
    with ExitStack() as stack:
        stack.enter_context(patch.object(settings, "paper_execution_symbols", ["SPY"]))
        stack.enter_context(patch.object(settings, "paper_exploration_enabled", True))
        stack.enter_context(patch("app.services.stock_paper_autotrader.active_paper_account", return_value=_account()))
        stack.enter_context(patch("app.services.stock_paper_autotrader._is_regular_session", return_value=True))
        stack.enter_context(patch("app.services.stock_paper_autotrader.cancel_stale_exit_entries", return_value=[]))
        stack.enter_context(signal_source(research, return_value={
            "signal_id": 7, "signal_action": action, "reference_price": "100"}))
        stack.enter_context(patch("app.services.stock_paper_autotrader._strategy_holdings",
                                  return_value=(holdings or {}, True)))
        stack.enter_context(patch("app.services.stock_paper_autotrader._pending_orders", return_value=[]))
        stack.enter_context(patch("app.services.stock_paper_autotrader.broker_stop_coverage",
                                  return_value=Decimal("0")))
        stack.enter_context(patch("app.services.stock_paper_autotrader.load_entry_protections",
                                  return_value=protections))
        reserve = stack.enter_context(patch("app.services.stock_paper_autotrader.reserve_stock_paper_order",
                                            return_value=order))
        stack.enter_context(patch("app.services.stock_paper_autotrader.dispatch_reserved_order", return_value=order))
        result = run_stock_paper_signal_cycle(db)
    return result, reserve


@pytest.mark.parametrize("kind", KINDS)
def test_each_protection_skips_scheduled_buys_with_named_reason(kind):
    result, reserve = _cycle([_MACD], "BUY", _blocking(kind))
    reserve.assert_not_called()
    row = result["results"][0]
    assert row["status"] == "skipped"
    assert row["protection"] == kind and f"entry protection {kind}" in row["reason"]
    assert result["status"] == "complete"  # a protection skip is not a failure
    assert "entry_protections" in result


@pytest.mark.parametrize("kind", KINDS)
def test_each_protection_skips_exploration_buys(kind):
    result, reserve = _cycle([_CANDIDATE], "BUY", _blocking(kind), research=True)
    reserve.assert_not_called()
    row = result["results"][0]
    assert row["mode"] == "explore" and row["status"] == "skipped" and row["protection"] == kind


@pytest.mark.parametrize("kind", KINDS)
def test_protections_never_block_signal_sells(kind):
    rows = {"StockPaperPosition": [SimpleNamespace(quantity=Decimal("5"), average_entry_price=Decimal("100"))]}
    result, reserve = _cycle([_MACD], "SELL", _blocking(kind), rows=rows, holdings={"macd_momentum": Decimal("5")})
    assert reserve.call_count == 1 and reserve.call_args.kwargs["side"] == "sell"
    assert result["results"][-1]["status"] == "submitted"


@pytest.mark.parametrize("kind", KINDS)
def test_protections_never_block_the_protective_stop(kind):
    rows = {"StockPaperPosition": [SimpleNamespace(quantity=Decimal("4"), average_entry_price=Decimal("110"))]}
    result, reserve = _cycle([_MACD], "HOLD", _blocking(kind), rows=rows)
    assert reserve.call_count == 1 and reserve.call_args.kwargs["side"] == "sell"
    stop = next(row for row in result["results"] if row["strategy"] == "protective_stop")
    assert stop["status"] == "submitted"


def test_unblocked_buy_still_reaches_reservation():
    result, reserve = _cycle([_MACD], "BUY", EntryProtections(now=NOW, config=CFG))
    assert reserve.call_count == 1
    assert result["results"][0]["status"] == "submitted"


def test_protective_stop_key_matches_autotrader_reservation():
    rows = {"StockPaperPosition": [SimpleNamespace(quantity=Decimal("4"), average_entry_price=Decimal("110"))]}
    _result, reserve = _cycle([_MACD], "HOLD", EntryProtections(now=NOW, config=CFG), rows=rows)
    key = reserve.call_args.kwargs["idempotency_key"]
    session_key = key.split(":")[1]
    assert key == protective_stop_idempotency_key(session_key, "SPY")
