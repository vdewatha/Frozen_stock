"""Broker-side exit re-arm (OCO / GTC stop) for uncovered long shares.

Every test uses fake Alpaca gateways; nothing reaches a broker.
"""
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401  register all tables
from app.core.config import settings
from app.db.base import Base
from app.models import RiskRule, StockPaperRecoveryState, Strategy, StrategySignal
from app.models.stock_paper import StockPaperAccount, StockPaperFill, StockPaperOrder
from app.services.stock_exit_rearm import exit_percentages, rearm_broker_exits
from app.services.stock_paper_autotrader import _strategy_holdings
from app.services.stock_paper_ledger import (
    EXPLORATION_SOURCE,
    StockPaperError,
    StockPaperUnavailable,
    broker_stop_coverage,
    build_exit_plan,
    cancel_broker_exit_orders,
    dispatch_reserved_order,
    exit_cancel_targets,
    initialize_stock_paper_account,
    reconcile_stock_paper_account,
    reserve_stock_paper_order,
)
from tests.test_stock_paper_bracket_exits import PARENT, SL_LEG, TP_LEG, BracketGateway, _leg, _parent
from tests.test_stock_paper_ledger import FakeAlpaca

ROOT = "oco-root-1"
CHILD = "oco-stop-1"


def _account_row(cash="100000", equity="100000"):
    return {"id": "paper-account", "currency": "USD", "status": "ACTIVE", "cash": cash,
            "buying_power": "200000", "equity": equity, "last_equity": equity,
            "updated_at": "2026-01-01T15:00:00Z"}


def _position(qty="10", *, available=None, price="101", avg="100", symbol="SPY"):
    return {"symbol": symbol, "qty": qty, "qty_available": qty if available is None else available,
            "avg_entry_price": avg, "current_price": price,
            "market_value": str(Decimal(qty) * Decimal(price))}


def _oco_root(client_order_id, *, qty="10", take="103.00", status="new", legs=True, stop="97.00"):
    row = {"id": ROOT, "client_order_id": client_order_id, "symbol": "SPY", "side": "sell", "qty": qty,
           "filled_qty": "0", "type": "limit", "limit_price": take, "time_in_force": "gtc",
           "status": status, "order_class": "oco", "submitted_at": "2026-01-02T15:00:00Z"}
    if legs:
        row["legs"] = [_oco_child(qty=qty, stop=stop)]
    return row


def _oco_child(*, qty="10", stop="97.00", status="held", filled_qty="0"):
    return {"id": CHILD, "client_order_id": "alpaca-oco-stop-1", "symbol": "SPY", "side": "sell", "qty": qty,
            "filled_qty": filled_qty, "type": "stop", "stop_price": stop, "limit_price": None,
            "time_in_force": "gtc", "status": status, "order_class": "oco",
            "submitted_at": "2026-01-02T15:00:00Z"}


class RearmGateway(FakeAlpaca):
    """Accepts one exit POST and echoes Alpaca's OCO response shape."""

    supports_bracket_orders = True

    def __init__(self, *, reject_status=None, timeout=False, **kwargs):
        super().__init__(**kwargs)
        self.payloads = []
        self.reject_status = reject_status
        self.timeout = timeout

    def submit_order(self, payload):
        self.calls += 1
        self.payloads.append(payload)
        if self.reject_status:
            raise StockPaperUnavailable("rejected", status_code=self.reject_status)
        if self.timeout:
            raise TimeoutError("network lost")
        if payload.get("order_class") == "oco":
            return _oco_root(payload["client_order_id"], qty=payload["qty"],
                             take=payload["take_profit"]["limit_price"],
                             stop=payload["stop_loss"]["stop_price"])
        return {"id": f"stop-{self.calls}", "client_order_id": payload["client_order_id"], "symbol": "SPY",
                "side": "sell", "qty": payload["qty"], "type": "stop", "stop_price": payload["stop_price"],
                "time_in_force": "gtc", "status": "new", "order_class": "simple"}


class ExitRearmTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(settings, "active_paper_broker", "alpaca_paper"),
            patch.object(settings, "paper_broker_account_id", ""),
            patch.object(settings, "paper_exploration_enabled", True),
            patch.object(settings, "paper_exit_rearm_enabled", True),
            patch.object(settings, "paper_exit_rearm_while_halted", True),
        ]
        for item in self.patches:
            item.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + str(Path(self.tmp.name) / "rearm.sqlite"))
        Base.metadata.create_all(self.engine)
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.engine.dispose()
        self.tmp.cleanup()

    # ----------------------------------------------------------- helpers
    def legacy(self, db, qty="10", **position):
        """A position imported before bracket exits existed (no fills, no legs)."""
        initialize_stock_paper_account(db, FakeAlpaca(account=_account_row(), positions=[_position(qty, **position)]))
        db.add(RiskRule(name="rearm", value={"max_open_positions": 10}))
        db.commit()
        account = db.query(StockPaperAccount).one()
        account.status, account.reconciliation_required = "reconciled", False
        db.commit()
        return account

    def rearm(self, db, gateway, **kwargs):
        return rearm_broker_exits(db, gateway, now=kwargs.pop("now", self.now))

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

    def set_rules(self, db, **values):
        rule = db.query(RiskRule).filter(RiskRule.is_active.is_(True)).order_by(RiskRule.id).first()
        rule.value = dict(rule.value or {}) | values
        db.commit()

    # ------------------------------------------------- legacy positions
    def test_legacy_position_gets_a_whole_share_oco_and_is_then_covered(self):
        with Session(self.engine) as db:
            account = self.legacy(db)
            gateway = RearmGateway(positions=[_position("10")])
            result = self.rearm(db, gateway)
            self.assertEqual(result["submitted_count"], 1, result)
            payload = gateway.payloads[0]
            self.assertEqual(payload["order_class"], "oco")
            self.assertEqual(payload["type"], "limit")
            self.assertEqual(payload["side"], "sell")
            self.assertEqual(payload["time_in_force"], "gtc")
            self.assertEqual(payload["qty"], "10")
            self.assertEqual(payload["take_profit"], {"limit_price": "103.00"})
            self.assertEqual(payload["stop_loss"], {"stop_price": "97.00"})
            self.assertTrue(payload["client_order_id"].startswith("rearm-"))
            root = db.query(StockPaperOrder).filter_by(broker_order_id=ROOT).one()
            child = db.query(StockPaperOrder).filter_by(broker_order_id=CHILD).one()
            self.assertEqual((root.order_class, root.leg_role, root.parent_order_id), ("oco", "take_profit", None))
            self.assertEqual((child.leg_role, child.parent_order_id, child.side), ("stop_loss", root.id, "sell"))
            self.assertEqual(child.stop_price, Decimal("97.00"))
            self.assertEqual(broker_stop_coverage(db, account.id, "SPY"), Decimal("10"))
            # One DELETE cancels the whole OCO group.
            self.assertEqual([row.id for row in exit_cancel_targets([root, child])], [root.id])

            again = self.rearm(db, RearmGateway(positions=[_position("10", available="0")]))
            self.assertEqual(again["submitted_count"], 0)
            self.assertEqual(again["results"][0]["status"], "covered")

    def test_oco_holds_inventory_so_an_app_sell_cannot_double_sell(self):
        with Session(self.engine) as db, ExitStack() as stack:
            self.legacy(db)
            self.rearm(db, RearmGateway(positions=[_position("10")]))
            self.gates(stack)
            with self.assertRaisesRegex(StockPaperError, "short"):
                reserve_stock_paper_order(
                    db, symbol="SPY", side="sell", quantity=Decimal("1"), reference_price=Decimal("101"),
                    idempotency_key="manual-exit", source="manual_close",
                )

    def test_fractional_remainder_is_reported_not_covered(self):
        with Session(self.engine) as db:
            self.legacy(db, qty="10.5")
            gateway = RearmGateway(positions=[_position("10.5")])
            result = self.rearm(db, gateway)
            self.assertEqual(gateway.payloads[0]["qty"], "10")
            self.assertEqual(result["results"][0]["fractional_uncovered"], "0.5")

    def test_sub_share_position_is_left_to_the_app_side_stop(self):
        with Session(self.engine) as db:
            self.legacy(db, qty="0.4")
            gateway = RearmGateway(positions=[_position("0.4")])
            result = self.rearm(db, gateway)
            self.assertEqual(gateway.calls, 0)
            lot = result["results"][0]["lots"][0]
            self.assertEqual(lot["status"], "skipped")
            self.assertIn("fractional", lot["reason"])

    def test_price_already_through_the_stop_is_left_to_the_protective_stop(self):
        with Session(self.engine) as db:
            self.legacy(db, price="96")
            gateway = RearmGateway(positions=[_position("10", price="96")])
            result = self.rearm(db, gateway)
            self.assertEqual(gateway.calls, 0)
            self.assertIn("app-side protective stop", result["results"][0]["lots"][0]["reason"])

    def test_price_above_take_profit_arms_a_stop_only_order(self):
        with Session(self.engine) as db:
            account = self.legacy(db, price="104")
            gateway = RearmGateway(positions=[_position("10", price="104")])
            self.rearm(db, gateway)
            payload = gateway.payloads[0]
            self.assertEqual(payload["type"], "stop")
            self.assertEqual(payload["stop_price"], "97.00")
            self.assertNotIn("order_class", payload)
            self.assertEqual(broker_stop_coverage(db, account.id, "SPY"), Decimal("10"))

    def test_quantity_is_capped_by_broker_available_shares(self):
        with Session(self.engine) as db:
            self.legacy(db)
            gateway = RearmGateway(positions=[_position("10", available="4")])
            self.rearm(db, gateway)
            self.assertEqual(gateway.payloads[0]["qty"], "4")

    def test_ledger_broker_mismatch_skips(self):
        with Session(self.engine) as db:
            self.legacy(db)
            gateway = RearmGateway(positions=[_position("12")])
            result = self.rearm(db, gateway)
            self.assertEqual(gateway.calls, 0)
            self.assertIn("differ", result["results"][0]["reason"])

    def test_risk_rule_and_strategy_percentages(self):
        rules = {"position_stop_loss_pct": 0.05, "position_take_profit_pct": 0.08}
        self.assertEqual(exit_percentages(rules, None), (Decimal("0.05"), Decimal("0.08")))
        # Re-arm resolves exits exactly like the entry path: the declared
        # policy wins (rsi2_pullback: 10% catastrophe stop, no take-profit) ...
        rsi2 = Strategy(name="x", strategy_type="rsi2_pullback", parameters={})
        self.assertEqual(exit_percentages(rules, rsi2), (Decimal("0.1"), Decimal("0")))
        # ... with row-parameter overrides applied ...
        tuned = Strategy(name="y", strategy_type="rsi2_pullback", parameters={"stop_loss_pct": 0.05})
        self.assertEqual(exit_percentages(rules, tuned), (Decimal("0.05"), Decimal("0")))
        # ... and strategies without a declared policy keep the risk rules.
        plain = Strategy(name="z", strategy_type="macd_momentum", parameters={})
        self.assertEqual(exit_percentages(rules, plain), (Decimal("0.05"), Decimal("0.08")))
        with Session(self.engine) as db:
            self.legacy(db)
            self.set_rules(db, position_stop_loss_pct=0.05, position_take_profit_pct=0.08)
            gateway = RearmGateway(positions=[_position("10")])
            self.rearm(db, gateway)
            self.assertEqual(gateway.payloads[0]["stop_loss"], {"stop_price": "95.00"})
            self.assertEqual(gateway.payloads[0]["take_profit"], {"limit_price": "108.00"})

    def test_stop_disabled_skips(self):
        with Session(self.engine) as db:
            self.legacy(db)
            self.set_rules(db, position_stop_loss_pct=0)
            gateway = RearmGateway(positions=[_position("10")])
            self.rearm(db, gateway)
            self.assertEqual(gateway.calls, 0)

    # ------------------------------------------- halts and recovery gates
    def test_runs_with_the_kill_switch_on_but_never_during_recovery(self):
        with Session(self.engine) as db:
            self.legacy(db)
            self.set_rules(db, kill_switch_enabled=True)
            db.add(StockPaperRecoveryState(id=1, status="cooldown", flatten_policy="none", updated_by="test"))
            db.commit()
            gateway = RearmGateway(positions=[_position("10")])
            result = self.rearm(db, gateway)
            self.assertEqual(result["status"], "skipped")
            self.assertIn("recovery", result["reason"])
            self.assertEqual(gateway.calls, 0)

            db.get(StockPaperRecoveryState, 1).status = "armed"
            db.commit()
            result = self.rearm(db, gateway)
            self.assertTrue(result["kill_switch"])
            self.assertEqual(result["submitted_count"], 1)

    def test_halt_policy_setting_can_block_rearm(self):
        with Session(self.engine) as db, patch.object(settings, "paper_exit_rearm_while_halted", False):
            self.legacy(db)
            self.set_rules(db, kill_switch_enabled=True)
            gateway = RearmGateway(positions=[_position("10")])
            result = self.rearm(db, gateway)
            self.assertEqual(result["status"], "skipped")
            self.assertEqual(gateway.calls, 0)

    def test_stale_broker_snapshot_and_disabled_setting_skip(self):
        with Session(self.engine) as db:
            self.legacy(db)
            gateway = RearmGateway(positions=[_position("10")])
            stale = self.rearm(db, gateway, now=self.now + timedelta(minutes=30))
            self.assertIn("stale", stale["reason"])
            with patch.object(settings, "paper_exit_rearm_enabled", False):
                self.assertEqual(self.rearm(db, gateway)["status"], "skipped")
            self.assertEqual(gateway.calls, 0)

    def test_tradier_gateway_is_never_used(self):
        with Session(self.engine) as db:
            self.legacy(db)
            gateway = RearmGateway(positions=[_position("10")])
            gateway.supports_bracket_orders = False
            self.assertEqual(self.rearm(db, gateway)["status"], "skipped")
            self.assertEqual(gateway.calls, 0)

    # ----------------------------------------------- submission outcomes
    def test_broker_rejection_is_recorded_without_halting_and_backs_off(self):
        with Session(self.engine) as db:
            account = self.legacy(db)
            gateway = RearmGateway(positions=[_position("10")], reject_status=422)
            result = self.rearm(db, gateway)
            self.assertEqual(result["results"][0]["lots"][0]["status"], "rejected")
            db.refresh(account)
            self.assertEqual(account.status, "reconciled")
            again = self.rearm(db, gateway)
            self.assertEqual(gateway.calls, 1)
            self.assertIn("backing off", again["results"][0]["reason"])

    def test_uncertain_submission_halts_and_reconciliation_adopts_the_oco(self):
        with Session(self.engine) as db:
            account = self.legacy(db)
            result = self.rearm(db, RearmGateway(positions=[_position("10")], timeout=True))
            self.assertEqual(result["status"], "halted")
            row = db.query(StockPaperOrder).filter_by(status="unknown").one()
            db.refresh(account)
            self.assertEqual(account.status, "halted")
            # Nothing more is armed while the outcome is uncertain.
            blocked = self.rearm(db, RearmGateway(positions=[_position("10")]))
            self.assertIn("uncertain", blocked["reason"])

            nested = _oco_root(row.client_order_id)
            gateway = FakeAlpaca(
                account=_account_row(), positions=[_position("10")],
                orders=[{**nested, "legs": None}, _oco_child()],
                lookup_rows={row.client_order_id: nested},
            )
            reconcile_stock_paper_account(db, gateway)
            self.assertEqual(db.query(StockPaperOrder).filter_by(source="broker_import").count(), 0)
            child = db.query(StockPaperOrder).filter_by(broker_order_id=CHILD).one()
            self.assertEqual(child.parent_order_id, row.id)
            db.refresh(account)
            self.assertNotIn("Externally submitted", account.halt_reason or "")

    # -------------------------------------- strategy lots and reconciliation
    def seed_strategy(self, db):
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

    def bracket_lot(self, db):
        strategy, signal = self.seed_strategy(db)
        with ExitStack() as stack:
            self.gates(stack)
            plan = build_exit_plan(Decimal("100"), stop_loss_pct="0.03", take_profit_pct="0.03")
            order = reserve_stock_paper_order(
                db, symbol="SPY", side="buy", quantity=Decimal("2"), reference_price=Decimal("100"),
                idempotency_key="bracket-entry", source=EXPLORATION_SOURCE, signal_id=signal.id, exit_plan=plan,
            )
            parent = dispatch_reserved_order(db, order.id, BracketGateway())
        entry_fill = {"id": "fill-entry", "activity_type": "FILL", "order_id": PARENT, "symbol": "SPY",
                      "side": "buy", "qty": "2", "price": "100", "commission": "0",
                      "transaction_time": (self.now + timedelta(minutes=1)).isoformat()}
        position = {"symbol": "SPY", "qty": "2", "avg_entry_price": "100", "market_value": "200",
                    "current_price": "100"}
        reconcile_stock_paper_account(db, FakeAlpaca(
            account=_account_row(cash="99800"), positions=[position],
            orders=[_parent(parent.client_order_id, "filled", filled_qty="2"),
                    _leg(TP_LEG, "take_profit", "new"), _leg(SL_LEG, "stop_loss", "held")],
            activities=[entry_fill],
        ))
        return strategy, parent, entry_fill, position

    def test_live_bracket_legs_mean_nothing_to_rearm(self):
        with Session(self.engine) as db:
            _strategy, _parent_row, _fill, position = self.bracket_lot(db)
            gateway = RearmGateway(positions=[position | {"qty_available": "0"}])
            result = self.rearm(db, gateway)
            self.assertEqual(gateway.calls, 0)
            self.assertEqual(result["results"][0]["status"], "covered")

    def test_cancelled_legs_are_rearmed_for_the_owning_strategy_and_fills_attribute(self):
        """Gap (c): legs cancelled for an app-side SELL that never went out."""
        with Session(self.engine) as db:
            strategy, parent, entry_fill, position = self.bracket_lot(db)
            legs = db.query(StockPaperOrder).filter_by(parent_order_id=parent.id).all()
            cancel = cancel_broker_exit_orders(db, legs, FakeAlpaca(lookup_rows={
                leg.client_order_id: _leg(leg.broker_order_id, leg.leg_role, "canceled") for leg in legs
            }))
            self.assertTrue(cancel["clear"])
            self.assertEqual(broker_stop_coverage(db, parent.account_id, "SPY"), Decimal("0"))
            # The kill switch flips before the replacing sell is dispatched.
            self.set_rules(db, kill_switch_enabled=True)
            account = db.query(StockPaperAccount).one()
            account.source_timestamp = self.now
            db.commit()
            gateway = RearmGateway(positions=[position | {"qty_available": "2"}])
            result = self.rearm(db, gateway)
            self.assertEqual(result["submitted_count"], 1, result)
            root = db.query(StockPaperOrder).filter_by(broker_order_id=ROOT).one()
            child = db.query(StockPaperOrder).filter_by(broker_order_id=CHILD).one()
            for row in (root, child):
                self.assertEqual(row.strategy_id, strategy.id)
                self.assertEqual(row.source, EXPLORATION_SOURCE)
            self.assertEqual(gateway.payloads[0]["qty"], "2")

            canceled_legs = [_leg(TP_LEG, "take_profit", "canceled"), _leg(SL_LEG, "stop_loss", "canceled")]
            listed = [_parent(parent.client_order_id, "filled", filled_qty="2"), *canceled_legs,
                      _oco_root(root.client_order_id, qty="2", take="103.00", legs=False),
                      _oco_child(qty="2")]
            reconciled = reconcile_stock_paper_account(db, FakeAlpaca(
                account=_account_row(cash="99800"), positions=[position], orders=listed,
                activities=[entry_fill], lookup_rows={root.client_order_id: _oco_root(root.client_order_id, qty="2")},
            ))
            self.assertEqual(db.query(StockPaperOrder).filter_by(source="broker_import").count(), 0)
            self.assertNotIn("Externally submitted", reconciled.get("reason") or "")

            stopped = [_parent(parent.client_order_id, "filled", filled_qty="2"), *canceled_legs,
                       _oco_root(root.client_order_id, qty="2", status="canceled", legs=False),
                       _oco_child(qty="2", status="filled", filled_qty="2")]
            stop_fill = {"id": "fill-oco-stop", "activity_type": "FILL", "order_id": CHILD, "symbol": "SPY",
                         "side": "sell", "qty": "2", "price": "97", "commission": "0",
                         "transaction_time": (self.now + timedelta(minutes=9)).isoformat()}
            reconcile_stock_paper_account(db, FakeAlpaca(
                account=_account_row(cash="99994"), positions=[], orders=stopped,
                activities=[entry_fill, stop_fill],
            ))
            fill = db.query(StockPaperFill).filter_by(broker_activity_id="fill-oco-stop").one()
            self.assertEqual(db.get(StockPaperOrder, fill.order_id).strategy_id, strategy.id)
            holdings, attributed = _strategy_holdings(db, parent.account_id, "SPY", {strategy.id: "macd_momentum"})
            self.assertTrue(attributed)
            self.assertEqual(holdings, {"macd_momentum": Decimal("0")})

    def test_fresh_app_sell_reservation_defers_while_trading_is_enabled(self):
        with Session(self.engine) as db:
            self.legacy(db)
            db.add(StockPaperOrder(account_id=1, client_order_id="sp-reserved-sell", symbol="SPY", side="sell",
                                   quantity=Decimal("10"), order_type="limit", time_in_force="day",
                                   limit_price=Decimal("101"), reserved_cash=Decimal("0"), status="reserved",
                                   source="scheduled_paper_signal"))
            db.commit()
            gateway = RearmGateway(positions=[_position("10")])
            result = self.rearm(db, gateway)
            self.assertEqual(gateway.calls, 0)
            self.assertIn("reservation", result["results"][0]["reason"])
            # With the kill switch on the reservation cannot dispatch: arm.
            self.set_rules(db, kill_switch_enabled=True)
            self.rearm(db, gateway)
            self.assertEqual(gateway.calls, 1)

    def test_partially_filled_entry_is_cancelled_after_grace_not_overlapped(self):
        """Gap (b): held legs never activate on a partial fill."""
        with Session(self.engine) as db:
            strategy, signal = self.seed_strategy(db)
            with ExitStack() as stack:
                self.gates(stack)
                plan = build_exit_plan(Decimal("100"), stop_loss_pct="0.03", take_profit_pct="0.03")
                order = reserve_stock_paper_order(
                    db, symbol="SPY", side="buy", quantity=Decimal("2"), reference_price=Decimal("100"),
                    idempotency_key="bracket-entry", source=EXPLORATION_SOURCE, signal_id=signal.id,
                    exit_plan=plan,
                )
                parent = dispatch_reserved_order(db, order.id, BracketGateway())
            fill_time = self.now - timedelta(minutes=10)
            position = {"symbol": "SPY", "qty": "1", "avg_entry_price": "100", "market_value": "100",
                        "current_price": "100"}
            reconcile_stock_paper_account(db, FakeAlpaca(
                account=_account_row(cash="99900"), positions=[position],
                orders=[_parent(parent.client_order_id, "partially_filled", filled_qty="1"),
                        _leg(TP_LEG, "take_profit", "held"), _leg(SL_LEG, "stop_loss", "held")],
                activities=[{"id": "fill-part", "activity_type": "FILL", "order_id": PARENT, "symbol": "SPY",
                             "side": "buy", "qty": "1", "price": "100", "commission": "0",
                             "transaction_time": fill_time.isoformat()}],
            ))
            account = db.query(StockPaperAccount).one()
            account.source_timestamp = self.now
            db.commit()
            gateway = RearmGateway(positions=[position | {"qty_available": "1"}])
            result = self.rearm(db, gateway)
            self.assertEqual(gateway.cancelled_order_ids, [PARENT])
            self.assertEqual(result["cancelled_partial_entries"], [parent.id])
            self.assertEqual(gateway.calls, 0)  # no OCO on top of held legs
            self.assertEqual(db.get(StockPaperOrder, parent.id).status, "pending_cancel")

            # Within the grace period nothing is cancelled.
            with patch.object(settings, "paper_exit_rearm_partial_entry_grace_minutes", 60):
                db.get(StockPaperOrder, parent.id).status = "partially_filled"
                db.commit()
                later = RearmGateway(positions=[position | {"qty_available": "1"}])
                self.rearm(db, later)
                self.assertEqual(later.cancelled_order_ids, [])
                self.assertEqual(later.calls, 0)


if __name__ == "__main__":
    unittest.main()
