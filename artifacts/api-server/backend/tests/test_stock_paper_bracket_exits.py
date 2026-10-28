"""Broker-side bracket exits for scheduled/exploration stock-paper entries.

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

import app.models  # register all tables
from app.core.config import settings
from app.db.base import Base
from app.models import RiskRule, Strategy, StrategySignal
from app.models.stock_paper import StockPaperAccount, StockPaperFill, StockPaperOrder
from app.services.stock_paper_autotrader import _strategy_holdings, run_stock_paper_signal_cycle
from app.services.stock_paper_ledger import (
    EXPLORATION_SOURCE,
    StockPaperError,
    TradierPaperClient,
    build_exit_plan,
    broker_stop_coverage,
    cancel_broker_exit_orders,
    cancel_stale_exit_entries,
    dispatch_reserved_order,
    exit_cancel_targets,
    initialize_stock_paper_account,
    reconcile_stock_paper_account,
    reserve_stock_paper_order,
)
from tests.test_stock_paper_ledger import FakeAlpaca
from tests.test_stock_paper_autotrader import _ModelDb, _account

PARENT = "parent-1"
TP_LEG = "leg-tp-1"
SL_LEG = "leg-sl-1"


def _account_row(cash="100000", equity="100000"):
    return {"id": "paper-account", "currency": "USD", "status": "ACTIVE", "cash": cash,
            "buying_power": "200000", "equity": equity, "last_equity": equity,
            "updated_at": "2026-01-01T15:00:00Z"}


def _leg(broker_id, kind, status, *, qty="2", filled_qty="0"):
    row = {"id": broker_id, "client_order_id": f"alpaca-{broker_id}", "symbol": "SPY", "side": "sell",
           "qty": qty, "filled_qty": filled_qty, "time_in_force": "gtc", "status": status,
           "order_class": "bracket", "submitted_at": "2026-01-02T15:00:00Z"}
    if kind == "take_profit":
        row |= {"type": "limit", "limit_price": "103.00"}
    else:
        row |= {"type": "stop", "limit_price": None, "stop_price": "97.00"}
    return row


def _parent(client_order_id, status, *, legs=None, filled_qty="0"):
    return {"id": PARENT, "client_order_id": client_order_id, "symbol": "SPY", "side": "buy", "qty": "2",
            "filled_qty": filled_qty, "type": "limit", "time_in_force": "gtc", "limit_price": "100",
            "status": status, "order_class": "bracket", "submitted_at": "2026-01-02T15:00:00Z",
            "legs": legs}


class BracketGateway(FakeAlpaca):
    """FakeAlpaca that accepts a bracket POST and echoes its child legs."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.payloads = []

    def submit_order(self, payload):
        self.calls += 1
        self.payloads.append(payload)
        if payload.get("order_class") == "bracket":
            return _parent(payload["client_order_id"], "accepted",
                           legs=[_leg(TP_LEG, "take_profit", "held"), _leg(SL_LEG, "stop_loss", "held")])
        return {"id": f"simple-{self.calls}", "status": "accepted", "client_order_id": payload["client_order_id"]}


class ExitPlanTests(unittest.TestCase):
    def test_prices_round_away_from_entry_to_valid_ticks(self):
        plan = build_exit_plan(Decimal("100"), stop_loss_pct="0.03", take_profit_pct="0.03")
        self.assertEqual(plan, {"order_class": "bracket", "stop_price": Decimal("97.00"),
                                "take_profit_price": Decimal("103.00")})
        odd = build_exit_plan(Decimal("123.457"), stop_loss_pct="0.03", take_profit_pct="0.05")
        self.assertEqual(odd["stop_price"], Decimal("119.75"))
        self.assertEqual(odd["take_profit_price"], Decimal("129.63"))
        penny = build_exit_plan(Decimal("0.5"), stop_loss_pct="0.03", take_profit_pct="0.03")
        self.assertEqual(penny["stop_price"], Decimal("0.4850"))

    def test_single_leg_is_oto_and_zero_disables(self):
        self.assertEqual(build_exit_plan(Decimal("100"), stop_loss_pct="0.03", take_profit_pct=0)["order_class"], "oto")
        self.assertIsNone(build_exit_plan(Decimal("100"), stop_loss_pct=0, take_profit_pct=0))
        with self.assertRaises(StockPaperError):
            build_exit_plan(Decimal("100"), stop_loss_pct="1.5", take_profit_pct="0.03")

    def test_tradier_cannot_carry_broker_exits(self):
        self.assertFalse(TradierPaperClient.supports_bracket_orders)


class BracketLedgerTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(settings, "active_paper_broker", "alpaca_paper"),
            patch.object(settings, "paper_broker_account_id", ""),
            patch.object(settings, "paper_exploration_enabled", True),
        ]
        for item in self.patches:
            item.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + str(Path(self.tmp.name) / "bracket.sqlite"))
        Base.metadata.create_all(self.engine)
        self.now = datetime.now(timezone.utc)

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

    def seed(self, db):
        initialize_stock_paper_account(db, FakeAlpaca(account=_account_row()))
        strategy = Strategy(name="Explorer", strategy_type="macd_momentum", parameters={},
                            is_active=True, current_status="paper_trading_candidate")
        db.add(strategy)
        db.flush()
        signal = StrategySignal(strategy_id=strategy.id, symbol="SPY", signal_time=self.now, action="BUY",
                                probability_up=Decimal("0.8"), probability_down=Decimal("0.2"),
                                confidence=Decimal("0.8"), features={"test": True})
        db.add(signal)
        db.add(RiskRule(name="bracket", value={"max_open_positions": 10}))
        db.commit()
        return strategy, signal

    def reserve_entry(self, db, signal, quantity=Decimal("2")):
        plan = build_exit_plan(Decimal("100"), stop_loss_pct="0.03", take_profit_pct="0.03")
        return reserve_stock_paper_order(
            db, symbol="SPY", side="buy", quantity=quantity, reference_price=Decimal("100"),
            idempotency_key="bracket-entry", source=EXPLORATION_SOURCE, signal_id=signal.id, exit_plan=plan,
        )

    def submit_entry(self, db):
        strategy, signal = self.seed(db)
        gateway = BracketGateway()
        with ExitStack() as stack:
            self.gates(stack)
            order = self.reserve_entry(db, signal)
            parent = dispatch_reserved_order(db, order.id, gateway)
        return strategy, parent, gateway

    def fill_entry(self, db, *, sl_status="held"):
        """Reconcile a filled entry whose legs are live, as Alpaca lists them (nested=false)."""
        parent = db.query(StockPaperOrder).filter_by(broker_order_id=PARENT).one()
        gateway = FakeAlpaca(
            account=_account_row(cash="99800"),
            positions=[{"symbol": "SPY", "qty": "2", "avg_entry_price": "100", "market_value": "200",
                        "current_price": "100"}],
            orders=[_parent(parent.client_order_id, "filled", filled_qty="2"),
                    _leg(TP_LEG, "take_profit", "new"), _leg(SL_LEG, "stop_loss", sl_status)],
            activities=[{"id": "fill-entry", "activity_type": "FILL", "order_id": PARENT, "symbol": "SPY",
                         "side": "buy", "qty": "2", "price": "100", "commission": "0",
                         "transaction_time": (self.now + timedelta(minutes=1)).isoformat()}],
        )
        return reconcile_stock_paper_account(db, gateway)

    def test_reserve_rejects_fractional_bracket_quantity(self):
        with Session(self.engine) as db, ExitStack() as stack:
            self.gates(stack)
            _strategy, signal = self.seed(db)
            with self.assertRaisesRegex(StockPaperError, "whole-share"):
                self.reserve_entry(db, signal, quantity=Decimal("1.5"))

    def test_bracket_payload_shape_and_legs_persisted_with_strategy(self):
        with Session(self.engine) as db:
            strategy, parent, gateway = self.submit_entry(db)
            payload = gateway.payloads[0]
            self.assertEqual(payload["order_class"], "bracket")
            self.assertEqual(payload["type"], "limit")
            self.assertEqual(payload["time_in_force"], "gtc")
            self.assertEqual(payload["qty"], "2")
            self.assertEqual(payload["take_profit"], {"limit_price": "103.00"})
            self.assertEqual(payload["stop_loss"], {"stop_price": "97.00"})
            self.assertNotIn("extended_hours", payload)
            legs = db.query(StockPaperOrder).filter_by(parent_order_id=parent.id).all()
            self.assertEqual({leg.leg_role for leg in legs}, {"take_profit", "stop_loss"})
            for leg in legs:
                self.assertEqual(leg.strategy_id, strategy.id)
                self.assertEqual(leg.source, EXPLORATION_SOURCE)
                self.assertIsNone(leg.signal_id)
                self.assertEqual(leg.side, "sell")
            self.assertEqual(parent.status, "accepted")

    def test_dispatch_refuses_bracket_on_gateway_without_support(self):
        with Session(self.engine) as db, ExitStack() as stack:
            self.gates(stack)
            _strategy, signal = self.seed(db)
            order = self.reserve_entry(db, signal)
            gateway = BracketGateway()
            gateway.supports_bracket_orders = False
            with self.assertRaisesRegex(StockPaperError, "broker-side exits"):
                dispatch_reserved_order(db, order.id, gateway)
            self.assertEqual(gateway.calls, 0)
            self.assertEqual(db.get(StockPaperOrder, order.id).status, "reserved")

    def test_child_leg_fills_reconcile_and_attribute_to_the_strategy_lot(self):
        with Session(self.engine) as db:
            strategy, parent, _gateway = self.submit_entry(db)
            first = self.fill_entry(db)
            self.assertEqual(first["status"], "reconciled", first.get("reason"))
            self.assertEqual(db.query(StockPaperOrder).filter_by(source="broker_import").count(), 0)
            self.assertEqual(broker_stop_coverage(db, parent.account_id, "SPY"), Decimal("2"))

            stopped = FakeAlpaca(
                account=_account_row(cash="99994"),
                positions=[],
                orders=[_parent(parent.client_order_id, "filled", filled_qty="2"),
                        _leg(TP_LEG, "take_profit", "canceled"),
                        _leg(SL_LEG, "stop_loss", "filled", filled_qty="2")],
                activities=[
                    {"id": "fill-entry", "activity_type": "FILL", "order_id": PARENT, "symbol": "SPY",
                     "side": "buy", "qty": "2", "price": "100", "commission": "0",
                     "transaction_time": (self.now + timedelta(minutes=1)).isoformat()},
                    {"id": "fill-stop", "activity_type": "FILL", "order_id": SL_LEG, "symbol": "SPY",
                     "side": "sell", "qty": "2", "price": "97", "commission": "0",
                     "transaction_time": (self.now + timedelta(minutes=5)).isoformat()},
                ],
            )
            second = reconcile_stock_paper_account(db, stopped)
            self.assertEqual(second["status"], "reconciled", second.get("reason"))
            stop_fill = db.query(StockPaperFill).filter_by(broker_activity_id="fill-stop").one()
            stop_order = db.get(StockPaperOrder, stop_fill.order_id)
            self.assertEqual(stop_order.leg_role, "stop_loss")
            self.assertEqual(stop_order.strategy_id, strategy.id)
            holdings, attributed = _strategy_holdings(db, parent.account_id, "SPY", {strategy.id: "macd_momentum"})
            self.assertTrue(attributed)
            self.assertEqual(holdings, {"macd_momentum": Decimal("0")})

    def test_uncertain_bracket_submission_recovers_legs_without_external_halt(self):
        with Session(self.engine) as db, ExitStack() as stack:
            self.gates(stack)
            _strategy, signal = self.seed(db)
            order = self.reserve_entry(db, signal)
            parent = dispatch_reserved_order(db, order.id, FakeAlpaca())  # POST times out
            self.assertEqual(parent.status, "unknown")
            nested = _parent(parent.client_order_id, "accepted",
                             legs=[_leg(TP_LEG, "take_profit", "held"), _leg(SL_LEG, "stop_loss", "held")])
            gateway = FakeAlpaca(
                account=_account_row(),
                orders=[{**nested, "legs": None}, _leg(TP_LEG, "take_profit", "held"),
                        _leg(SL_LEG, "stop_loss", "held")],
                lookup_rows={parent.client_order_id: nested},
            )
            reconcile_stock_paper_account(db, gateway)
            self.assertEqual(db.query(StockPaperOrder).filter_by(source="broker_import").count(), 0)
            legs = db.query(StockPaperOrder).filter_by(parent_order_id=parent.id).all()
            self.assertEqual(len(legs), 2)
            account = db.query(StockPaperAccount).one()
            self.assertNotIn("Externally submitted", account.halt_reason or "")

    def test_app_side_sell_cannot_double_sell_while_a_leg_is_live(self):
        with Session(self.engine) as db:
            _strategy, parent, _gateway = self.submit_entry(db)
            self.assertEqual(self.fill_entry(db)["status"], "reconciled")
            legs = db.query(StockPaperOrder).filter_by(parent_order_id=parent.id).all()
            with ExitStack() as stack:
                self.gates(stack)
                with self.assertRaisesRegex(StockPaperError, "short"):
                    reserve_stock_paper_order(
                        db, symbol="SPY", side="sell", quantity=Decimal("2"), reference_price=Decimal("100"),
                        idempotency_key="app-exit-blocked", source="scheduled_paper_signal",
                    )
                sell = reserve_stock_paper_order(
                    db, symbol="SPY", side="sell", quantity=Decimal("2"), reference_price=Decimal("100"),
                    idempotency_key="app-exit", source="scheduled_paper_signal",
                    replacing_exit_order_ids={leg.id for leg in legs},
                )
                gateway = BracketGateway()
                with self.assertRaisesRegex(StockPaperError, "current inventory"):
                    dispatch_reserved_order(db, sell.id, gateway)
                self.assertEqual(gateway.calls, 0)

                # Cancel the group (one DELETE, on the active TP leg), confirm, then sell.
                cancelling = FakeAlpaca(lookup_rows={
                    leg.client_order_id: _leg(leg.broker_order_id, leg.leg_role, "canceled") for leg in legs
                })
                outcome = cancel_broker_exit_orders(db, legs, cancelling)
                self.assertEqual(cancelling.cancelled_order_ids, [TP_LEG])
                self.assertTrue(outcome["clear"])
                submitted = dispatch_reserved_order(db, sell.id, gateway)
                self.assertEqual(submitted.status, "accepted")
                self.assertEqual(gateway.calls, 1)

    def test_leg_fill_seen_during_cancel_blocks_the_app_sell(self):
        with Session(self.engine) as db:
            _strategy, parent, _gateway = self.submit_entry(db)
            self.fill_entry(db)
            legs = db.query(StockPaperOrder).filter_by(parent_order_id=parent.id).all()
            gateway = FakeAlpaca(lookup_rows={
                leg.client_order_id: _leg(leg.broker_order_id, leg.leg_role,
                                          "filled" if leg.leg_role == "stop_loss" else "canceled",
                                          filled_qty="2" if leg.leg_role == "stop_loss" else "0")
                for leg in legs
            })
            outcome = cancel_broker_exit_orders(db, legs, gateway)
            self.assertTrue(outcome["leg_filled"])
            self.assertFalse(outcome["clear"])

    def test_cancel_targets_one_order_per_group(self):
        with Session(self.engine) as db:
            _strategy, parent, _gateway = self.submit_entry(db)
            group = db.query(StockPaperOrder).filter(
                (StockPaperOrder.id == parent.id) | (StockPaperOrder.parent_order_id == parent.id)
            ).all()
            # Open entry: cancelling the entry cancels its held legs.
            self.assertEqual([row.id for row in exit_cancel_targets(group)], [parent.id])
            self.fill_entry(db)
            legs = db.query(StockPaperOrder).filter_by(parent_order_id=parent.id).all()
            targets = exit_cancel_targets(legs)
            self.assertEqual([row.broker_order_id for row in targets], [TP_LEG])

    def test_recovery_cancel_all_handles_legs_with_one_delete_per_group(self):
        from app.services.stock_recovery import cancel_open_stock_orders
        with Session(self.engine) as db:
            _strategy, parent, _gateway = self.submit_entry(db)
            self.fill_entry(db)
            gateway = FakeAlpaca(
                account=_account_row(cash="99800"),
                positions=[{"symbol": "SPY", "qty": "2", "avg_entry_price": "100", "market_value": "200",
                            "current_price": "100"}],
                orders=[_parent(parent.client_order_id, "filled", filled_qty="2"),
                        _leg(TP_LEG, "take_profit", "canceled"), _leg(SL_LEG, "stop_loss", "canceled")],
                activities=[{"id": "fill-entry", "activity_type": "FILL", "order_id": PARENT, "symbol": "SPY",
                             "side": "buy", "qty": "2", "price": "100", "commission": "0",
                             "transaction_time": (self.now + timedelta(minutes=1)).isoformat()}],
            )
            result = cancel_open_stock_orders(db, actor="test", gateway=gateway)
            self.assertEqual(gateway.cancelled_order_ids, [TP_LEG])
            self.assertNotEqual(result["status"], "failed")
            live = db.query(StockPaperOrder).filter(StockPaperOrder.parent_order_id == parent.id,
                                                    StockPaperOrder.status == "canceled").count()
            self.assertEqual(live, 2)

    def test_stale_unfilled_gtc_entry_is_cancelled_next_session(self):
        with Session(self.engine) as db:
            _strategy, parent, _gateway = self.submit_entry(db)
            parent.submitted_at = self.now - timedelta(days=1)
            db.commit()
            account = db.query(StockPaperAccount).one()
            gateway = FakeAlpaca()
            with patch("app.services.stock_paper_ledger.session_bounds",
                       return_value=(self.now - timedelta(hours=1), self.now + timedelta(hours=1))):
                cancelled = cancel_stale_exit_entries(db, account, self.now, gateway)
            self.assertEqual(cancelled, [parent.id])
            self.assertEqual(gateway.cancelled_order_ids, [PARENT])


# --- autotrader cooperation (mocked db, no broker) -------------------------

_MACD = SimpleNamespace(id=3, strategy_type="macd_momentum")


def _cycle(db, action, *, holdings=None, pending=(), coverage=Decimal("0"), exit_orders=(), cancel=None,
           price="100", rules=None):
    order = SimpleNamespace(id=42, status="accepted")
    rule = SimpleNamespace(value=rules or {})
    db.rows_by_model.setdefault("RiskRule", [rule])
    with ExitStack() as stack:
        stack.enter_context(patch.object(settings, "paper_execution_symbols", ["SPY"]))
        stack.enter_context(patch("app.services.stock_paper_autotrader.active_paper_account", return_value=_account()))
        stack.enter_context(patch("app.services.stock_paper_autotrader._is_regular_session", return_value=True))
        stack.enter_context(patch("app.services.stock_paper_autotrader.cancel_stale_exit_entries", return_value=[]))
        stack.enter_context(patch("app.services.stock_paper_autotrader.create_stock_paper_signal", return_value={
            "signal_id": 7, "signal_action": action, "reference_price": price}))
        stack.enter_context(patch("app.services.stock_paper_autotrader._strategy_holdings",
                                  return_value=(holdings or {}, True)))
        stack.enter_context(patch("app.services.stock_paper_autotrader._pending_orders", return_value=list(pending)))
        stack.enter_context(patch("app.services.stock_paper_autotrader.broker_stop_coverage", return_value=coverage))
        stack.enter_context(patch("app.services.stock_paper_autotrader.live_exit_orders", return_value=list(exit_orders)))
        cancel_mock = stack.enter_context(patch("app.services.stock_paper_autotrader.cancel_broker_exit_orders",
                                                return_value=cancel or {"cancelled_order_ids": [], "clear": True,
                                                                        "leg_filled": False}))
        release = stack.enter_context(patch("app.services.stock_paper_autotrader.release_unsubmitted_reservation"))
        reserve = stack.enter_context(patch("app.services.stock_paper_autotrader.reserve_stock_paper_order",
                                            return_value=order))
        dispatch = stack.enter_context(patch("app.services.stock_paper_autotrader.dispatch_reserved_order",
                                             return_value=order))
        result = run_stock_paper_signal_cycle(db)
    return result, SimpleNamespace(reserve=reserve, dispatch=dispatch, cancel=cancel_mock, release=release)


def _leg_row(role="stop_loss"):
    return SimpleNamespace(id=11, side="sell", leg_role=role, status="held", strategy_id=3, parent_order_id=10)


def test_entry_is_whole_share_bracket_from_risk_rules():
    db = _ModelDb([_MACD], {})
    result, calls = _cycle(db, "BUY", rules={"max_risk_per_trade": "0.01", "position_take_profit_pct": "0.05"},
                           price="333")
    kwargs = calls.reserve.call_args.kwargs
    assert kwargs["quantity"] == Decimal("3")  # 1000 / 333 = 3.003 -> 3 whole shares
    assert kwargs["exit_plan"]["order_class"] == "bracket"
    assert kwargs["exit_plan"]["stop_price"] == Decimal("323.01")
    assert kwargs["exit_plan"]["take_profit_price"] == Decimal("349.65")
    assert result["results"][0]["status"] == "submitted"


def test_entry_below_one_share_is_skipped_with_reason():
    db = _ModelDb([_MACD], {})
    result, calls = _cycle(db, "BUY", price="2500")  # 1% of 100k = $1000 < one share
    calls.reserve.assert_not_called()
    row = result["results"][0]
    assert row["status"] == "skipped"
    assert "whole share" in row["reason"]


def test_disabling_both_exit_legs_keeps_fractional_simple_entry():
    db = _ModelDb([_MACD], {})
    _result, calls = _cycle(db, "BUY", price="2500",
                            rules={"position_stop_loss_pct": 0, "position_take_profit_pct": 0})
    kwargs = calls.reserve.call_args.kwargs
    assert kwargs["quantity"] == Decimal("0.4")
    assert "exit_plan" not in kwargs


def test_new_entry_waits_while_exit_legs_are_live_for_the_symbol():
    db = _ModelDb([_MACD], {})
    result, calls = _cycle(db, "BUY", pending=[_leg_row()])
    calls.reserve.assert_not_called()
    assert "exit legs are live" in result["results"][0]["reason"]


def test_protective_stop_defers_to_a_live_broker_stop_leg():
    position = SimpleNamespace(quantity=Decimal("5"), average_entry_price=Decimal("110"))
    db = _ModelDb([_MACD], {"StockPaperPosition": [position]})
    result, calls = _cycle(db, "HOLD", pending=[_leg_row()], coverage=Decimal("5"))
    calls.reserve.assert_not_called()
    stop = next(row for row in result["results"] if row["strategy"] == "protective_stop")
    assert stop["status"] == "skipped"
    assert "broker-side stop-loss" in stop["reason"]


def test_protective_stop_sells_only_the_uncovered_remainder():
    position = SimpleNamespace(quantity=Decimal("5.5"), average_entry_price=Decimal("110"))
    db = _ModelDb([_MACD], {"StockPaperPosition": [position]})
    _result, calls = _cycle(db, "HOLD", pending=[_leg_row()], coverage=Decimal("5"))
    assert calls.reserve.call_args.kwargs["quantity"] == Decimal("0.5")


def test_sell_signal_cancels_legs_before_dispatching_app_exit():
    position = SimpleNamespace(quantity=Decimal("5"), average_entry_price=Decimal("100"))
    db = _ModelDb([_MACD], {"StockPaperPosition": [position]})
    legs = [_leg_row("take_profit"), _leg_row("stop_loss")]
    result, calls = _cycle(db, "SELL", holdings={"macd_momentum": Decimal("5")}, pending=legs, exit_orders=legs,
                           cancel={"cancelled_order_ids": [11], "clear": True, "leg_filled": False})
    assert calls.reserve.call_args.kwargs["replacing_exit_order_ids"] == {11}
    calls.cancel.assert_called_once()
    calls.dispatch.assert_called_once()
    assert result["results"][0]["status"] == "submitted"


def test_sell_signal_defers_when_leg_cancel_is_not_confirmed():
    position = SimpleNamespace(quantity=Decimal("5"), average_entry_price=Decimal("100"))
    db = _ModelDb([_MACD], {"StockPaperPosition": [position]})
    legs = [_leg_row()]
    result, calls = _cycle(db, "SELL", holdings={"macd_momentum": Decimal("5")}, pending=legs, exit_orders=legs,
                           cancel={"cancelled_order_ids": [11], "clear": False, "leg_filled": False})
    calls.dispatch.assert_not_called()
    assert result["results"][0]["status"] == "deferred"


def test_sell_signal_releases_reservation_when_a_leg_already_filled():
    position = SimpleNamespace(quantity=Decimal("5"), average_entry_price=Decimal("100"))
    db = _ModelDb([_MACD], {"StockPaperPosition": [position]})
    legs = [_leg_row()]
    result, calls = _cycle(db, "SELL", holdings={"macd_momentum": Decimal("5")}, pending=legs, exit_orders=legs,
                           cancel={"cancelled_order_ids": [], "clear": False, "leg_filled": True})
    calls.dispatch.assert_not_called()
    calls.release.assert_called_once()
    assert result["results"][0]["status"] == "skipped"
