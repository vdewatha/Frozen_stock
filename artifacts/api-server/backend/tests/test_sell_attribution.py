"""App-side sells and broker exit legs are booked against the lot's strategy.

Covers sell attribution in ``reserve_stock_paper_order``, the autotrader's
owner lookup, exit-leg inheritance in reconciliation, scorecard round trips,
and the exploration lot count freeing up only after a lot is fully sold.
Every test uses fake Alpaca gateways; nothing reaches a broker.
"""
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401  register all tables
from app.core.config import settings
from app.db.base import Base
from app.models import RiskRule, Strategy, StrategySignal
from app.models.stock_paper import StockPaperAccount, StockPaperFill, StockPaperOrder, StockPaperPosition
from app.services.paper_trade_scorecard import reconstruct_round_trips
from app.services.stock_paper_autotrader import _sell_owner_id, _strategy_lots
from app.services.stock_paper_ledger import (
    EXPLORATION_SOURCE,
    StockPaperError,
    _open_exploration_lots,
    initialize_stock_paper_account,
    reconcile_stock_paper_account,
    reserve_stock_paper_order,
)
from tests.test_paper_trade_scorecard import Ledger, ScorecardFixture, at, client_id
from tests.test_stock_paper_autotrader import _ModelDb
import tests.test_stock_paper_bracket_exits as bracket
from tests.test_stock_paper_bracket_exits import (
    PARENT,
    SL_LEG,
    TP_LEG,
    _account_row,
    _cycle,
    _leg,
    _parent,
)
from tests.test_stock_paper_ledger import FakeAlpaca

PRICE = Decimal("100")


class _LedgerCase(unittest.TestCase):
    max_positions = 1

    def setUp(self):
        self.patches = [
            patch.object(settings, "active_paper_broker", "alpaca_paper"),
            patch.object(settings, "paper_broker_account_id", ""),
            patch.object(settings, "paper_exploration_enabled", True),
            patch.object(settings, "paper_exploration_max_positions", self.max_positions),
        ]
        for item in self.patches:
            item.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + str(Path(self.tmp.name) / "attribution.sqlite"))
        Base.metadata.create_all(self.engine)
        self.now = datetime.now(timezone.utc)
        self.fill_counter = 0

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.engine.dispose()
        self.tmp.cleanup()

    def gates(self, stack):
        now = self.now
        for item in (
            patch("app.services.stock_paper_ledger.session_bounds",
                  return_value=(now - timedelta(hours=1), now + timedelta(hours=1))),
            patch("app.services.stock_paper_ledger.feed_status", return_value={"status": "ready"}),
            patch("app.services.stock_paper_ledger._validate_reference_price"),
            patch("app.services.stock_learning_cycle.evaluate_launch_admission_prerequisites",
                  return_value={"all": {"status": "pass"}}),
        ):
            stack.enter_context(item)

    def strategy(self, db, name, symbol, status="paper_trading_candidate"):
        if not db.query(StockPaperAccount).first():
            initialize_stock_paper_account(db, FakeAlpaca(account=_account_row()))
            db.add(RiskRule(name="attribution", value={"max_open_positions": 10}))
        strategy = Strategy(name=name, strategy_type=f"type_{name}", parameters={}, is_active=True,
                            current_status=status)
        db.add(strategy)
        db.flush()
        signal = StrategySignal(strategy_id=strategy.id, symbol=symbol, signal_time=self.now, action="BUY",
                                probability_up=Decimal("0.8"), probability_down=Decimal("0.2"),
                                confidence=Decimal("0.8"), features={"test": True})
        db.add(signal)
        db.commit()
        return strategy, signal

    @property
    def quantity(self):
        # 0.25% of the fake account's 100k equity at $100 a share.
        return Decimal("2.5")

    def explore_buy(self, db, signal, symbol, key):
        return reserve_stock_paper_order(
            db, symbol=symbol, side="buy", quantity=self.quantity, reference_price=PRICE,
            idempotency_key=key, source=EXPLORATION_SOURCE, signal_id=signal.id,
        )

    def fill(self, db, order, quantity, *, minutes):
        """Mark ``order`` filled by ``quantity`` and move the position accordingly."""
        self.fill_counter += 1
        order.status = "filled"
        order.reserved_cash = Decimal("0")
        order.broker_order_id = order.broker_order_id or f"broker-{order.id}"
        db.add(StockPaperFill(
            account_id=order.account_id, order_id=order.id, broker_activity_id=f"act-{self.fill_counter}",
            broker_order_id=order.broker_order_id, symbol=order.symbol, side=order.side, quantity=quantity,
            price=PRICE, fee=Decimal("0"), cost_known=True,
            filled_at=self.now + timedelta(minutes=minutes), raw_payload={},
        ))
        position = db.query(StockPaperPosition).filter_by(account_id=order.account_id, symbol=order.symbol).one_or_none()
        sign = Decimal("1") if order.side == "buy" else Decimal("-1")
        if position is None:
            position = StockPaperPosition(account_id=order.account_id, symbol=order.symbol, quantity=Decimal("0"),
                                          average_entry_price=PRICE, current_price=PRICE, market_value=Decimal("0"),
                                          observed_at=self.now, raw_payload={})
            db.add(position)
        position.quantity += sign * quantity
        position.market_value = position.quantity * PRICE
        db.commit()

    def sell(self, db, symbol, quantity, key, **kwargs):
        kwargs.setdefault("source", "scheduled_paper_signal")
        return reserve_stock_paper_order(
            db, symbol=symbol, side="sell", quantity=quantity, reference_price=PRICE,
            idempotency_key=key, **kwargs,
        )


class SellReservationAttributionTests(_LedgerCase):
    max_positions = 5

    def test_sell_records_the_lot_strategy_and_buys_take_it_from_the_signal(self):
        with Session(self.engine) as db, ExitStack() as stack:
            self.gates(stack)
            strategy, signal = self.strategy(db, "a", "SPY")
            entry = self.explore_buy(db, signal, "SPY", "buy-a")
            self.assertEqual(entry.strategy_id, strategy.id)
            self.fill(db, entry, self.quantity, minutes=1)
            order = self.sell(db, "SPY", self.quantity, "sell-a", source=EXPLORATION_SOURCE,
                              strategy_id=strategy.id)
            self.assertEqual(order.strategy_id, strategy.id)
            self.assertIsNone(order.signal_id)
            self.fill(db, order, self.quantity, minutes=2)
            fill = db.query(StockPaperFill).filter_by(order_id=order.id).one()
            self.assertEqual(db.get(StockPaperOrder, fill.order_id).strategy_id, strategy.id)

    def test_unattributed_sell_is_unchanged(self):
        with Session(self.engine) as db, ExitStack() as stack:
            self.gates(stack)
            _strategy, signal = self.strategy(db, "a", "SPY")
            entry = self.explore_buy(db, signal, "SPY", "buy-a")
            self.fill(db, entry, self.quantity, minutes=1)
            self.assertIsNone(self.sell(db, "SPY", self.quantity, "sell-a").strategy_id)

    def test_attribution_does_not_relax_sell_validation(self):
        with Session(self.engine) as db, ExitStack() as stack:
            self.gates(stack)
            strategy, signal = self.strategy(db, "a", "SPY")
            entry = self.explore_buy(db, signal, "SPY", "buy-a")
            self.fill(db, entry, self.quantity, minutes=1)
            with self.assertRaisesRegex(StockPaperError, "short"):
                self.sell(db, "SPY", self.quantity + 1, "too-much", strategy_id=strategy.id)
            self.sell(db, "SPY", self.quantity, "first", strategy_id=strategy.id)
            # The first sell is still committed inventory.
            with self.assertRaisesRegex(StockPaperError, "short"):
                self.sell(db, "SPY", Decimal("1"), "second", strategy_id=strategy.id)
            with self.assertRaisesRegex(StockPaperError, "unknown strategy"):
                self.sell(db, "SPY", Decimal("1"), "ghost", strategy_id=999_999)
            with self.assertRaisesRegex(StockPaperError, "explicit attribution is for sells"):
                reserve_stock_paper_order(
                    db, symbol="SPY", side="buy", quantity=self.quantity, reference_price=PRICE,
                    idempotency_key="buy-with-id", source=EXPLORATION_SOURCE, signal_id=signal.id,
                    strategy_id=strategy.id,
                )


class ExplorationLotReleaseTests(_LedgerCase):
    """paper_exploration_max_positions=1: a second lot needs the first fully sold."""

    def open_first_lot(self, db):
        first, first_signal = self.strategy(db, "a", "SPY")
        entry = self.explore_buy(db, first_signal, "SPY", "buy-a")
        self.fill(db, entry, self.quantity, minutes=1)
        _second, second_signal = self.strategy(db, "b", "QQQ")
        with self.assertRaisesRegex(StockPaperError, "lot limit"):
            self.explore_buy(db, second_signal, "QQQ", "buy-b-blocked")
        return first, second_signal

    def test_full_app_side_exit_frees_the_slot_but_a_partial_does_not(self):
        with Session(self.engine) as db, ExitStack() as stack:
            self.gates(stack)
            first, second_signal = self.open_first_lot(db)
            half = self.quantity / 2
            partial = self.sell(db, "SPY", half, "sell-half", source=EXPLORATION_SOURCE, strategy_id=first.id)
            self.fill(db, partial, half, minutes=2)
            account = db.query(StockPaperAccount).one()
            self.assertEqual(_open_exploration_lots(db, account), 1)
            with self.assertRaisesRegex(StockPaperError, "lot limit"):
                self.explore_buy(db, second_signal, "QQQ", "buy-b-after-partial")
            rest = self.sell(db, "SPY", half, "sell-rest", source=EXPLORATION_SOURCE, strategy_id=first.id)
            self.fill(db, rest, half, minutes=3)
            self.assertEqual(_open_exploration_lots(db, account), 0)
            order = self.explore_buy(db, second_signal, "QQQ", "buy-b")
            self.assertEqual(order.status, "reserved")

    def test_protective_stop_and_manual_close_release_the_lot(self):
        with Session(self.engine) as db, ExitStack() as stack:
            self.gates(stack)
            _first, second_signal = self.open_first_lot(db)
            # A scheduled-source sell without attribution (manual close or an
            # ambiguous protective stop) still closes the symbol's lot.
            stop = self.sell(db, "SPY", self.quantity, "stop", source="manual_close")
            self.fill(db, stop, self.quantity, minutes=2)
            self.assertEqual(self.explore_buy(db, second_signal, "QQQ", "buy-b").status, "reserved")

    def test_another_strategys_sell_of_its_own_shares_keeps_the_lot_open(self):
        with Session(self.engine) as db, ExitStack() as stack:
            self.gates(stack)
            first, second_signal = self.open_first_lot(db)
            other = Strategy(name="c", strategy_type="type_c", parameters={}, is_active=True,
                             current_status="paper_trading_active")
            db.add(other)
            db.flush()
            account = db.query(StockPaperAccount).one()
            other_buy = StockPaperOrder(account_id=account.id, client_order_id="other-buy", symbol="SPY", side="buy",
                                        quantity=Decimal("3"), order_type="limit", time_in_force="day",
                                        limit_price=PRICE, reserved_cash=Decimal("0"), status="accepted",
                                        source="scheduled_paper_signal", strategy_id=other.id)
            db.add(other_buy)
            db.commit()
            self.fill(db, other_buy, Decimal("3"), minutes=2)
            other_sell = self.sell(db, "SPY", Decimal("3"), "other-sell", strategy_id=other.id)
            self.fill(db, other_sell, Decimal("3"), minutes=3)
            self.assertEqual(_open_exploration_lots(db, account), 1)
            with self.assertRaisesRegex(StockPaperError, "lot limit"):
                self.explore_buy(db, second_signal, "QQQ", "buy-b")
            mine = self.sell(db, "SPY", self.quantity, "mine", source=EXPLORATION_SOURCE, strategy_id=first.id)
            self.fill(db, mine, self.quantity, minutes=4)
            self.assertEqual(_open_exploration_lots(db, account), 0)


class BrokerLegAttributionTests(unittest.TestCase):
    """Reuse the bracket fixtures: exploration entry with broker TP/SL legs."""

    setUp = bracket.BracketLedgerTests.setUp
    tearDown = bracket.BracketLedgerTests.tearDown
    gates = bracket.BracketLedgerTests.gates
    seed = bracket.BracketLedgerTests.seed
    reserve_entry = bracket.BracketLedgerTests.reserve_entry
    submit_entry = bracket.BracketLedgerTests.submit_entry
    fill_entry = bracket.BracketLedgerTests.fill_entry

    def stopped(self, parent, *, sl_qty="2", tp_status="canceled", sl_status="filled", positions=()):
        activities = [
            {"id": "fill-entry", "activity_type": "FILL", "order_id": PARENT, "symbol": "SPY",
             "side": "buy", "qty": "2", "price": "100", "commission": "0",
             "transaction_time": (self.now + timedelta(minutes=1)).isoformat()},
            {"id": "fill-stop", "activity_type": "FILL", "order_id": SL_LEG, "symbol": "SPY",
             "side": "sell", "qty": sl_qty, "price": "97", "commission": "0",
             "transaction_time": (self.now + timedelta(minutes=5)).isoformat()},
        ]
        return FakeAlpaca(
            account=_account_row(cash="99994"), positions=list(positions),
            orders=[_parent(parent.client_order_id, "filled", filled_qty="2"),
                    _leg(TP_LEG, "take_profit", tp_status),
                    _leg(SL_LEG, "stop_loss", sl_status, filled_qty=sl_qty)],
            activities=activities,
        )

    def test_unattributed_child_legs_inherit_the_entry_strategy(self):
        with Session(self.engine) as db:
            strategy, parent, _gateway = self.submit_entry(db)
            self.assertEqual(self.fill_entry(db)["status"], "reconciled")
            for leg in db.query(StockPaperOrder).filter_by(parent_order_id=parent.id).all():
                leg.strategy_id = None  # e.g. recorded before the entry was attributed
            db.commit()
            account = db.query(StockPaperAccount).one()
            self.assertEqual(_open_exploration_lots(db, account), 1)
            result = reconcile_stock_paper_account(db, self.stopped(parent))
            self.assertEqual(result["status"], "reconciled", result.get("reason"))
            legs = db.query(StockPaperOrder).filter_by(parent_order_id=parent.id).all()
            self.assertEqual({leg.strategy_id for leg in legs}, {strategy.id})
            self.assertEqual(_open_exploration_lots(db, account), 0)

    def test_partial_leg_fill_keeps_the_exploration_lot_open(self):
        with Session(self.engine) as db:
            _strategy, parent, _gateway = self.submit_entry(db)
            self.assertEqual(self.fill_entry(db)["status"], "reconciled")
            account = db.query(StockPaperAccount).one()
            partial = self.stopped(parent, sl_qty="1", tp_status="new", sl_status="partially_filled", positions=[
                {"symbol": "SPY", "qty": "1", "avg_entry_price": "100", "market_value": "100", "current_price": "100"},
            ])
            partial.account_row = _account_row(cash="99897")
            result = reconcile_stock_paper_account(db, partial)
            self.assertEqual(result["status"], "reconciled", result.get("reason"))
            self.assertEqual(_open_exploration_lots(db, account), 1)


class ScorecardSellAttributionTests(ScorecardFixture):
    def test_attributed_app_sell_closes_its_own_strategy_lot(self):
        L = Ledger(self.db, self.account)
        a = L.order(symbol="NVDA", side="buy", quantity=4, strategy=self.trend)
        L.fill(a, 4, 50, at(13, 35), fee="0")
        c = L.order(symbol="NVDA", side="buy", quantity=6, strategy=self.swing)
        L.fill(c, 6, 52, at(16), fee="0")
        # An app-side policy exit whose client id is not a recoverable
        # sell-signal key: only the stored strategy_id can attribute it.
        exit_c = L.order(symbol="NVDA", side="sell", quantity=6, strategy=self.swing, client="policy-exit")
        L.fill(exit_c, 6, 55, at(19, 1), fee="0")
        # The protective stop on the remaining lot is attributed as well.
        stop_a = L.order(symbol="NVDA", side="sell", quantity=4, strategy=self.trend,
                         client=client_id("scheduled-paper:2026-10-29:NVDA:protective_stop:sell"), created=at(19, 30))
        L.fill(stop_a, 4, 48, at(19, 31), fee="0")
        self.db.commit()
        book = reconstruct_round_trips(self.db, self.account.id)
        lots = {lot.strategy_id: lot for lot in book["lots"]}
        self.assertTrue(lots[self.swing.id].closed)
        self.assertEqual([row["order_id"] for row in lots[self.swing.id].exits], [exit_c.id])
        self.assertEqual(lots[self.swing.id].exits[0]["price"], Decimal("55"))
        self.assertTrue(lots[self.trend.id].closed)
        self.assertEqual([row["reason"] for row in lots[self.trend.id].exits], ["protective_stop"])
        self.assertEqual(book["unmatched_exits"], [])


class AutotraderSellOwnerTests(_LedgerCase):
    def test_strategy_lots_report_the_owner_id_and_sell_owner_is_unambiguous(self):
        with Session(self.engine) as db, ExitStack() as stack:
            self.gates(stack)
            a, a_signal = self.strategy(db, "a", "SPY")
            entry = self.explore_buy(db, a_signal, "SPY", "buy-a")
            self.fill(db, entry, self.quantity, minutes=1)
            account = db.query(StockPaperAccount).one()
            types = {a.id: a.strategy_type}
            self.assertEqual(_strategy_lots(db, account.id, "SPY", types)["owner_ids"], {a.strategy_type: a.id})
            self.assertEqual(_sell_owner_id(db, account.id, "SPY", types, self.quantity), a.id)
            # More than the strategy owns is not attributed.
            self.assertIsNone(_sell_owner_id(db, account.id, "SPY", types, self.quantity + 1))
            # An unattributed fill in the episode leaves the sell unattributed.
            orphan = StockPaperOrder(account_id=account.id, client_order_id="import", symbol="SPY", side="buy",
                                     quantity=Decimal("1"), order_type="limit", time_in_force="day",
                                     limit_price=PRICE, reserved_cash=Decimal("0"), status="accepted",
                                     source="broker_import")
            db.add(orphan)
            db.commit()
            self.fill(db, orphan, Decimal("1"), minutes=2)
            self.assertIsNone(_sell_owner_id(db, account.id, "SPY", types, self.quantity))


_MACD = SimpleNamespace(id=3, strategy_type="macd_momentum")


def _lots(owner_ids, holdings):
    return {"holdings": holdings, "attributed": True, "opened_at": {}, "buyers": set(holdings),
            "owner_ids": owner_ids}


def test_signal_sell_is_reserved_with_the_lot_owner_strategy():
    position = SimpleNamespace(quantity=Decimal("5"), average_entry_price=Decimal("100"))
    db = _ModelDb([_MACD], {"StockPaperPosition": [position]})
    with patch("app.services.stock_paper_autotrader._strategy_lots",
               return_value=_lots({"macd_momentum": 17}, {"macd_momentum": Decimal("5")})):
        _result, calls = _cycle(db, "SELL", holdings={"macd_momentum": Decimal("5")})
    kwargs = calls.reserve.call_args.kwargs
    assert kwargs["side"] == "sell"
    assert kwargs["quantity"] == Decimal("5")
    assert kwargs["strategy_id"] == 17


def test_protective_stop_is_attributed_only_to_a_sole_holder():
    position = SimpleNamespace(quantity=Decimal("5"), average_entry_price=Decimal("110"))
    db = _ModelDb([_MACD], {"StockPaperPosition": [position]})
    with patch("app.services.stock_paper_autotrader._strategy_lots",
               return_value=_lots({"macd_momentum": 17}, {"macd_momentum": Decimal("5")})):
        _result, calls = _cycle(db, "HOLD")
    assert calls.reserve.call_args.kwargs["strategy_id"] == 17

    db = _ModelDb([_MACD], {"StockPaperPosition": [position]})
    shared = _lots({"macd_momentum": 17, "rsi2_pullback": 21},
                   {"macd_momentum": Decimal("2"), "rsi2_pullback": Decimal("3")})
    with patch("app.services.stock_paper_autotrader._strategy_lots", return_value=shared):
        _result, calls = _cycle(db, "HOLD")
    assert calls.reserve.call_args.kwargs["strategy_id"] is None
