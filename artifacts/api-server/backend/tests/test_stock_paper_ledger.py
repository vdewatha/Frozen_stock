import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # register all tables
from app.api.stock_paper import router
from app.core.config import Settings
from app.core.security import AuthenticationMiddleware, required_role
from app.db.base import Base
from app.db.session import get_db
from app.models import RiskRule
from app.services.stock_paper_ledger import (
    AlpacaPaperClient,
    StockPaperError,
    dispatch_reserved_order,
    initialize_stock_paper_account,
    reconcile_stock_paper_account,
    reserve_stock_paper_order,
    stock_paper_status,
)
from app.services.stock_recovery import run_stock_watchdog, resume_stock_paper_after_revalidation


class FakeAlpaca:
    def __init__(self, positions=None, orders=None, activities=None, account=None, lookup_rows=None):
        self.position_rows = positions or []
        self.order_rows = orders or []
        self.activity_rows = activities or []
        self.account_row = account
        self.lookup_rows = lookup_rows or {}
        self.calls = 0
        self.lookups = 0
        self.orders_after = []
        self.fills_after = []
        self.cancelled_order_ids = []
        self.cancel_error = None

    def account(self):
        return self.account_row or {"id": "paper-account", "currency": "USD", "status": "ACTIVE",
                "cash": "1000.00000000", "buying_power": "2000.00000000",
                "equity": "1000.00000000", "last_equity": "999.00000000",
                "updated_at": "2026-01-01T15:00:00Z"}

    def positions(self):
        return self.position_rows

    def orders(self, after=None):
        self.orders_after.append(after)
        return self.order_rows

    def fills(self, after=None):
        self.fills_after.append(after)
        return self.activity_rows

    def submit_order(self, payload):
        self.calls += 1
        raise TimeoutError("network lost")

    def order_by_client_id(self, client_order_id):
        self.lookups += 1
        return self.lookup_rows.get(client_order_id)

    def cancel_order(self, broker_order_id):
        if self.cancel_error:
            raise self.cancel_error
        self.cancelled_order_ids.append(broker_order_id)


class StockPaperLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + str(Path(self.tmp.name) / "ledger.sqlite"))
        Base.metadata.create_all(self.engine)

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    @staticmethod
    def account_payload(*, cash="1000", equity="1000", last_equity="999"):
        return {"id": "paper-account", "currency": "USD", "status": "ACTIVE",
                "cash": cash, "buying_power": "2000", "equity": equity, "last_equity": last_equity,
                "updated_at": "2026-01-01T15:00:00Z"}

    def reserve_context(self, now):
        return patch("app.services.stock_paper_ledger.session_bounds", return_value=(now.replace(hour=0), now.replace(hour=23))), \
            patch("app.services.stock_paper_ledger.feed_status", return_value={"status": "ready"}), \
            patch("app.services.stock_paper_ledger._validate_reference_price")

    def add_approved_signal(self, db, strategy, symbol, suffix, now):
        from app.models import StrategySignal, StockPaperStrategyEvidence
        if not db.query(StockPaperStrategyEvidence).filter_by(strategy_id=strategy.id).one_or_none():
            db.add(StockPaperStrategyEvidence(strategy_id=strategy.id, evidence_id=f"evidence-{strategy.id}",
                status="approved", verified_drawdown=Decimal("0.01"), consecutive_losses=0, observed_at=now,
                expires_at=now + timedelta(days=1), provenance={"kind": "forward_evidence"}))
        signal = StrategySignal(strategy_id=strategy.id, symbol=symbol, signal_time=now, action="BUY",
            probability_up=Decimal("0.8"), probability_down=Decimal("0.2"), confidence=Decimal("0.8"), features={"test": suffix})
        db.add(signal)
        db.flush()
        return signal

    def test_explicit_import_is_decimal_and_costs_unknown(self):
        with Session(self.engine) as db:
            result = initialize_stock_paper_account(db, FakeAlpaca())
            self.assertEqual(result["status"], "reconciled")
            self.assertEqual(result["account"]["cash"], "1000.00000000")
            self.assertFalse(result["costs_known"])
            self.assertTrue(result["legacy_nonqualifying"])
            with self.assertRaises(StockPaperError):
                initialize_stock_paper_account(db, FakeAlpaca())

    def test_short_and_position_drift_halt_account(self):
        short = FakeAlpaca([{"symbol": "SPY", "qty": "-1"}])
        with Session(self.engine) as db:
            with self.assertRaises(StockPaperError):
                initialize_stock_paper_account(db, short)
        initial = FakeAlpaca([{"symbol": "SPY", "qty": "1", "market_value": "100"}])
        changed = FakeAlpaca([{"symbol": "SPY", "qty": "2", "market_value": "200"}])
        with Session(self.engine) as db:
            initialize_stock_paper_account(db, initial)
            result = reconcile_stock_paper_account(db, changed)
            self.assertEqual(result["status"], "halted")
            self.assertTrue(result["account"]["reconciliation_required"])

    def test_broker_orders_and_activities_are_imported_without_inventing_costs(self):
        broker = FakeAlpaca(
            orders=[{"id": "broker-1", "client_order_id": "historical-1", "symbol": "SPY",
                     "side": "buy", "qty": "1", "type": "limit", "time_in_force": "day", "status": "filled"}],
            activities=[
                {"id": "fill-1", "activity_type": "FILL", "order_id": "broker-1", "symbol": "SPY",
                 "side": "buy", "qty": "1", "price": "100", "transaction_time": "2026-01-01T15:00:00Z"},
                {"id": "activity-1", "activity_type": "DIV", "transaction_time": "2026-01-01T16:00:00Z"},
            ],
        )
        with Session(self.engine) as db:
            result = initialize_stock_paper_account(db, broker)
            self.assertEqual(len(result["orders"]), 1)
            self.assertEqual(len(result["fills"]), 1)
            self.assertIsNone(result["fills"][0]["fee"])
            self.assertFalse(result["fills"][0]["cost_known"])
            from app.models.stock_paper import StockPaperBrokerActivity
            self.assertEqual(db.query(StockPaperBrokerActivity).count(), 2)

    def test_external_nonterminal_and_late_fill_are_fail_closed(self):
        external = FakeAlpaca(orders=[{"id": "external-1", "symbol": "SPY", "side": "buy", "qty": "1",
                                       "type": "limit", "time_in_force": "day", "status": "accepted"}])
        with Session(self.engine) as db:
            self.assertEqual(initialize_stock_paper_account(db, external)["status"], "halted")
        with Session(self.engine) as db:
            from app.models.stock_paper import (
                StockPaperAccount, StockPaperBrokerActivity, StockPaperEquitySnapshot,
                StockPaperFill, StockPaperLedgerEvent, StockPaperOrder, StockPaperPosition,
            )
            for model in (StockPaperLedgerEvent, StockPaperEquitySnapshot, StockPaperFill,
                          StockPaperBrokerActivity, StockPaperOrder, StockPaperPosition, StockPaperAccount):
                db.query(model).delete()
            db.commit()
            initialize_stock_paper_account(db, FakeAlpaca())
            late = FakeAlpaca(activities=[{"id": "late-fill", "activity_type": "FILL", "symbol": "SPY", "side": "buy",
                                            "qty": "1", "price": "100", "commission": "0", "transaction_time": "2000-01-01T00:00:00Z"}])
            self.assertEqual(reconcile_stock_paper_account(db, late)["status"], "halted")
            self.assertEqual(late.fills_after, [None])

    def test_alpaca_bare_order_lists_follow_until_cursor_without_dropping_evidence(self):
        client = AlpacaPaperClient()
        page_one = [{"id": f"order-{index}", "updated_at": f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}Z"}
                    for index in range(500)]
        page_two = [{"id": "order-500", "updated_at": "2025-12-31T23:59:59Z"}]
        requests = []

        def request(method, path, *, params=None, payload=None):
            requests.append((method, path, params))
            return page_one if len(requests) == 1 else page_two

        with patch.object(client, "_request", side_effect=request):
            rows = client.orders()
        self.assertEqual([row["id"] for row in rows], [f"order-{index}" for index in range(501)])
        self.assertEqual(requests[0][1:], ("/v2/orders", {"status": "all", "nested": "false", "direction": "desc", "limit": "500"}))
        self.assertEqual(requests[1][2]["until"], "2026-01-01T00:00:00+00:00")

    def test_activities_page_and_late_fill_are_fully_backfilled_not_lookback_filtered(self):
        client = AlpacaPaperClient()
        calls = []
        pages = iter([
            ({"activities": [{"id": "recent", "activity_type": "FILL"}], "next_page_token": "older"}, {}),
            ({"activities": [{"id": "late", "activity_type": "FILL"}]}, {}),
        ])
        with patch.object(client, "_request_response", side_effect=lambda method, path, **kwargs: (calls.append(kwargs["params"]) or next(pages))):
            rows = client.fills(datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertEqual([row["id"] for row in rows], ["recent", "late"])
        self.assertEqual(calls, [{"direction": "desc", "page_size": "100"}, {"direction": "desc", "page_size": "100", "page_token": "older"}])

    def test_partial_fill_explains_position_delta_without_claiming_unknown_costs(self):
        fill_time = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        initial = FakeAlpaca(positions=[{"symbol": "SPY", "qty": "1", "market_value": "100"}])
        updated = FakeAlpaca(
            positions=[{"symbol": "SPY", "qty": "2", "market_value": "200"}],
            activities=[{"id": "partial-fill", "activity_type": "FILL", "symbol": "SPY", "side": "buy",
                         "qty": "1", "price": "100", "commission": "0", "transaction_time": fill_time}],
            account=self.account_payload(cash="900", equity="1000", last_equity="999"),
        )
        with Session(self.engine) as db:
            initialize_stock_paper_account(db, initial)
            result = reconcile_stock_paper_account(db, updated)
            self.assertEqual(result["status"], "reconciled")
            self.assertFalse(result["costs_known"])
            self.assertFalse(result["account"]["accounting_verified"])
            self.assertEqual(result["positions"][0]["quantity"], "2.00000000")

    def test_missing_commission_never_bypasses_inventory_or_cash_reconciliation(self):
        fill_time = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        with Session(self.engine) as db:
            initialize_stock_paper_account(db, FakeAlpaca())
            wrong_inventory = FakeAlpaca(
                positions=[{"symbol": "SPY", "qty": "2", "market_value": "200"}],
                activities=[{"id": "unknown-fee-wrong-qty", "activity_type": "FILL", "symbol": "SPY", "side": "buy",
                             "qty": "1", "price": "100", "transaction_time": fill_time}],
                account=self.account_payload(cash="900", equity="1000", last_equity="999"),
            )
            result = reconcile_stock_paper_account(db, wrong_inventory)
            self.assertEqual(result["status"], "halted")
            self.assertIn("position quantity", result["reason"])
            self.assertTrue(result["account"]["unexplained_residual"])
        with Session(self.engine) as db:
            db.query(__import__("app.models.stock_paper", fromlist=["StockPaperAccount"]).StockPaperAccount).delete()
            db.commit()
            initialize_stock_paper_account(db, FakeAlpaca())
            cash_residual = FakeAlpaca(
                positions=[{"symbol": "SPY", "qty": "1", "market_value": "100"}],
                activities=[{"id": "unknown-fee-cash-residual", "activity_type": "FILL", "symbol": "SPY", "side": "buy",
                             "qty": "1", "price": "100", "transaction_time": fill_time}],
                account=self.account_payload(cash="899", equity="999", last_equity="999"),
            )
            result = reconcile_stock_paper_account(db, cash_residual)
            self.assertEqual(result["status"], "halted")
            self.assertIn("unverified residual", result["reason"])
            self.assertTrue(result["account"]["unexplained_residual"])

    def test_unexplained_residual_remains_halted_on_a_later_reconcile(self):
        residual = FakeAlpaca(account=self.account_payload(cash="999", equity="999", last_equity="999"))
        with Session(self.engine) as db:
            initialize_stock_paper_account(db, FakeAlpaca())
            first = reconcile_stock_paper_account(db, residual)
            self.assertEqual(first["status"], "halted")
            self.assertTrue(first["account"]["reconciliation_required"])
            self.assertTrue(first["account"]["unexplained_residual"])
            second = reconcile_stock_paper_account(db, residual)
            self.assertEqual(second["status"], "halted")
            self.assertTrue(second["account"]["reconciliation_required"])
            self.assertTrue(second["account"]["unexplained_residual"])
            self.assertIn("cash changed", second["reason"].lower())


    def test_submitting_lookup_and_provider_pagination_are_read_only(self):
        with Session(self.engine) as db:
            initialize_stock_paper_account(db, FakeAlpaca())
            from app.models.stock_paper import StockPaperAccount, StockPaperOrder
            account = db.query(StockPaperAccount).one()
            db.add(StockPaperOrder(account_id=account.id, client_order_id="submitting-1", symbol="SPY", side="sell",
                quantity=Decimal("1"), order_type="limit", time_in_force="day", limit_price=Decimal("100"),
                reserved_cash=Decimal("0"), status="submitting", source="manual_close"))
            db.commit()
            gateway = FakeAlpaca()
            self.assertEqual(reconcile_stock_paper_account(db, gateway)["status"], "halted")
            self.assertEqual(gateway.lookups, 1)
        client = AlpacaPaperClient()
        pages = iter([
            {"orders": [{"id": "1"}], "next_page_token": "next"},
            {"orders": [{"id": "2"}]},
        ])
        with patch.object(client, "_request_response", side_effect=lambda *_args, **_kwargs: (next(pages), {})):
            self.assertEqual([row["id"] for row in client._pages("/v2/orders", {"limit": "2"})], ["1", "2"])

    def test_uncertain_post_halts_and_never_blind_retries(self):
        with Session(self.engine) as db:
            initialize_stock_paper_account(db, FakeAlpaca())
            # Set up a current position for sell; tests dispatch mechanics without
            # allowing an external broker POST to succeed.
            from app.models.stock_paper import StockPaperPosition
            from app.models.stock_paper import StockPaperAccount
            account = db.query(StockPaperAccount).one()
            db.add(StockPaperPosition(account_id=account.id, symbol="SPY", quantity=Decimal("1"),
                   observed_at=datetime.now(timezone.utc), raw_payload={}))
            db.commit()
            now = datetime.now(timezone.utc)
            with patch("app.services.stock_paper_ledger.session_bounds", return_value=(now.replace(hour=0), now.replace(hour=23))), \
                 patch("app.services.stock_paper_ledger.feed_status", return_value={"status": "ready"}), \
                 patch("app.services.stock_paper_ledger._validate_reference_price"):
                order = reserve_stock_paper_order(db, symbol="SPY", side="sell", quantity=Decimal("1"),
                    reference_price=Decimal("100"), idempotency_key="exit-1", source="manual_close")
            gateway = FakeAlpaca()
            with patch("app.services.stock_paper_ledger.session_bounds", return_value=(now.replace(hour=0), now.replace(hour=23))), \
                 patch("app.services.stock_paper_ledger.feed_status", return_value={"status": "ready"}), \
                 patch("app.services.stock_paper_ledger._validate_reference_price"):
                first = dispatch_reserved_order(db, order.id, gateway)
                second = dispatch_reserved_order(db, order.id, gateway)
            self.assertEqual(first.status, "unknown")
            self.assertEqual(second.status, "unknown")
            self.assertEqual(gateway.calls, 1)
            self.assertEqual(stock_paper_status(db)["status"], "halted")

    def test_submitting_recovery_uses_lookup_and_never_repeats_post(self):
        raw_order = {"id": "recovered-1", "client_order_id": "submitting-1", "symbol": "SPY", "side": "sell",
                     "qty": "1", "type": "limit", "time_in_force": "day", "limit_price": "100", "status": "accepted"}
        with Session(self.engine) as db:
            initialize_stock_paper_account(db, FakeAlpaca())
            from app.models.stock_paper import StockPaperAccount, StockPaperOrder
            account = db.query(StockPaperAccount).one()
            db.add(StockPaperOrder(account_id=account.id, client_order_id="submitting-1", symbol="SPY", side="sell",
                quantity=Decimal("1"), order_type="limit", time_in_force="day", limit_price=Decimal("100"),
                reserved_cash=Decimal("0"), status="submitting", source="manual_close"))
            db.commit()
            gateway = FakeAlpaca(lookup_rows={"submitting-1": raw_order})
            result = reconcile_stock_paper_account(db, gateway)
            self.assertEqual(result["status"], "reconciled")
            recovered = db.query(StockPaperOrder).filter_by(client_order_id="submitting-1").one()
            self.assertEqual(recovered.broker_order_id, "recovered-1")
            self.assertEqual(recovered.status, "accepted")
            self.assertEqual(gateway.lookups, 1)
            self.assertEqual(gateway.calls, 0)

    def test_unsubmitted_sell_reservation_reconciles_then_dispatches_once_with_audited_actor(self):
        position = {"symbol": "SPY", "qty": "1", "market_value": "100"}
        with Session(self.engine) as db:
            initialize_stock_paper_account(db, FakeAlpaca(positions=[position]))
            now = datetime.now(timezone.utc)
            with ExitStack() as stack:
                for patcher in self.reserve_context(now):
                    stack.enter_context(patcher)
                order = reserve_stock_paper_order(
                    db, symbol="SPY", side="sell", quantity=Decimal("1"), reference_price=Decimal("100"),
                    idempotency_key="unsubmitted-sell-reservation", source="manual_close",
                )
            self.assertLessEqual(len(order.client_order_id), 48)
            self.assertTrue(order.client_order_id.startswith("sp-"))

            reconciliation_gateway = FakeAlpaca(positions=[position])
            result = reconcile_stock_paper_account(db, reconciliation_gateway)
            self.assertEqual(result["status"], "reconciled")
            self.assertEqual(reconciliation_gateway.lookups, 0)
            self.assertEqual(db.get(type(order), order.id).status, "reserved")

            dispatch_gateway = FakeAlpaca()
            dispatch_gateway.payloads = []

            def accept_submission(payload):
                dispatch_gateway.calls += 1
                dispatch_gateway.payloads.append(payload)
                return {"id": "accepted-sell-1", "status": "accepted"}

            dispatch_gateway.submit_order = accept_submission
            db.info["stock_paper_actor"] = "operator-test"
            with ExitStack() as stack:
                for patcher in self.reserve_context(now):
                    stack.enter_context(patcher)
                accepted = dispatch_reserved_order(db, order.id, dispatch_gateway)
            self.assertEqual(accepted.status, "accepted")
            self.assertEqual(dispatch_gateway.calls, 1)
            self.assertLessEqual(len(dispatch_gateway.payloads[0]["client_order_id"]), 48)

            from app.models.stock_paper import StockPaperLedgerEvent
            event = db.query(StockPaperLedgerEvent).filter_by(
                account_id=accepted.account_id, event_type="order_submission", status="accepted",
            ).one()
            self.assertEqual(event.actor, "operator-test")
            self.assertEqual(dispatch_reserved_order(db, order.id, dispatch_gateway).status, "accepted")
            self.assertEqual(dispatch_gateway.calls, 1)

    def test_reserved_sell_cannot_dispatch_after_external_terminal_sell_consumes_inventory(self):
        position = {"symbol": "SPY", "qty": "1", "market_value": "100"}
        external_order = {
            "id": "external-terminal-sell", "client_order_id": "external-sell", "symbol": "SPY", "side": "sell",
            "qty": "1", "type": "limit", "time_in_force": "day", "limit_price": "100", "status": "filled",
        }
        with Session(self.engine) as db:
            initialize_stock_paper_account(db, FakeAlpaca(positions=[position]))
            now = datetime.now(timezone.utc)
            with ExitStack() as stack:
                for patcher in self.reserve_context(now):
                    stack.enter_context(patcher)
                order = reserve_stock_paper_order(
                    db, symbol="SPY", side="sell", quantity=Decimal("1"), reference_price=Decimal("100"),
                    idempotency_key="inventory-consumed-before-dispatch", source="manual_close",
                )
            consumed = FakeAlpaca(
                orders=[external_order],
                activities=[{
                    "id": "external-terminal-sell-fill", "activity_type": "FILL", "order_id": external_order["id"],
                    "symbol": "SPY", "side": "sell", "qty": "1", "price": "100", "commission": "0",
                    "transaction_time": (now + timedelta(minutes=1)).isoformat(),
                }],
                account=self.account_payload(cash="1100"),
            )
            self.assertEqual(reconcile_stock_paper_account(db, consumed)["status"], "reconciled")
            self.assertEqual(db.get(type(order), order.id).status, "reserved")

            dispatch_gateway = FakeAlpaca()
            with ExitStack() as stack:
                for patcher in self.reserve_context(now):
                    stack.enter_context(patcher)
                with self.assertRaisesRegex(StockPaperError, "current inventory"):
                    dispatch_reserved_order(db, order.id, dispatch_gateway)
            self.assertEqual(dispatch_gateway.calls, 0)

    def test_dispatch_rechecks_current_cash_and_symbol_exposure_before_posting(self):
        def reserve_buy(db, suffix):
            from app.models import Strategy
            now = datetime.now(timezone.utc)
            strategy = Strategy(name=f"Dispatch recheck {suffix}", strategy_type="moving_average_crossover",
                                parameters={}, is_active=True, current_status="paper_trading_active")
            db.add(strategy)
            db.flush()
            signal = self.add_approved_signal(db, strategy, "SPY", suffix, now)
            db.add(RiskRule(name=f"dispatch-recheck-{suffix}", value={
                "max_risk_per_trade": "0.10", "max_symbol_exposure": "0.10", "max_open_positions": 3,
            }))
            db.commit()
            with ExitStack() as stack:
                for patcher in self.reserve_context(now):
                    stack.enter_context(patcher)
                order = reserve_stock_paper_order(
                    db, symbol="SPY", side="buy", quantity=Decimal("0.5"), reference_price=Decimal("100"),
                    idempotency_key=f"dispatch-recheck-{suffix}", source="manual_control_room", signal_id=signal.id,
                )
            return order, now

        with Session(self.engine) as db:
            initialize_stock_paper_account(db, FakeAlpaca())
            order, now = reserve_buy(db, "cash")
            from app.models.stock_paper import StockPaperAccount
            account = db.query(StockPaperAccount).one()
            account.cash = Decimal("40")
            db.commit()
            gateway = FakeAlpaca()
            with ExitStack() as stack:
                for patcher in self.reserve_context(now):
                    stack.enter_context(patcher)
                with self.assertRaisesRegex(StockPaperError, "current cash"):
                    dispatch_reserved_order(db, order.id, gateway)
            self.assertEqual(gateway.calls, 0)

            account.cash = Decimal("1000")
            db.commit()
            order, now = reserve_buy(db, "equity")
            account.equity = Decimal("400")
            db.commit()
            gateway = FakeAlpaca()
            with ExitStack() as stack:
                for patcher in self.reserve_context(now):
                    stack.enter_context(patcher)
                with self.assertRaisesRegex(StockPaperError, "symbol exposure"):
                    dispatch_reserved_order(db, order.id, gateway)
            self.assertEqual(gateway.calls, 0)

    def test_unknown_cost_positions_do_not_publish_cost_basis_or_unrealized_pl(self):
        position = {
            "symbol": "SPY", "qty": "1", "avg_entry_price": "90", "current_price": "100", "market_value": "100",
            "cost_basis": "90", "unrealized_pl": "10",
        }
        with Session(self.engine) as db:
            result = initialize_stock_paper_account(db, FakeAlpaca(positions=[position]))
            self.assertEqual(result["positions"][0]["average_entry_price"], "90.00000000")
            self.assertEqual(result["positions"][0]["current_price"], "100.00000000")
            self.assertIsNone(result["positions"][0]["cost_basis"])
            self.assertIsNone(result["positions"][0]["unrealized_pl"])

    def test_old_client_order_terminal_transition_is_full_backfilled_and_looked_up(self):
        raw_order = {"id": "old-order", "client_order_id": "old-client", "symbol": "SPY", "side": "buy",
                     "qty": "1", "type": "limit", "time_in_force": "day", "limit_price": "100", "status": "canceled"}
        with Session(self.engine) as db:
            initialize_stock_paper_account(db, FakeAlpaca())
            from app.models.stock_paper import StockPaperAccount, StockPaperOrder
            account = db.query(StockPaperAccount).one()
            db.add(StockPaperOrder(account_id=account.id, client_order_id="old-client", broker_order_id="old-order",
                symbol="SPY", side="buy", quantity=Decimal("1"), order_type="limit", time_in_force="day",
                limit_price=Decimal("100"), reserved_cash=Decimal("100"), status="accepted",
                submitted_at=datetime.now(timezone.utc) - timedelta(days=7), source="manual_control_room"))
            db.commit()
            gateway = FakeAlpaca(orders=[raw_order], lookup_rows={"old-client": raw_order})
            result = reconcile_stock_paper_account(db, gateway)
            self.assertEqual(result["status"], "reconciled")
            self.assertEqual(db.query(StockPaperOrder).filter_by(client_order_id="old-client").one().status, "canceled")
            self.assertEqual(gateway.orders_after, [None])
            self.assertEqual(gateway.lookups, 1)

    def test_stale_data_and_kill_switch_block_reservations(self):
        with Session(self.engine) as db:
            initialize_stock_paper_account(db, FakeAlpaca())
            now = datetime.now(timezone.utc)
            with patch("app.services.stock_paper_ledger.session_bounds", return_value=(now.replace(hour=0), now.replace(hour=23))), \
                 patch("app.services.stock_paper_ledger.feed_status", return_value={"status": "stale"}):
                with self.assertRaisesRegex(StockPaperError, "stale"):
                    reserve_stock_paper_order(db, symbol="SPY", side="buy", quantity=Decimal("1"),
                        reference_price=Decimal("10"), idempotency_key="stale-key", source="test")
            db.add(RiskRule(name="test-kill", value={"kill_switch_enabled": True}))
            db.commit()
            with patch("app.services.stock_paper_ledger.session_bounds", return_value=(now.replace(hour=0), now.replace(hour=23))), \
                 patch("app.services.stock_paper_ledger.feed_status", return_value={"status": "ready"}), \
                 patch("app.services.stock_paper_ledger._validate_reference_price"):
                with self.assertRaisesRegex(StockPaperError, "kill switch"):
                    reserve_stock_paper_order(db, symbol="SPY", side="buy", quantity=Decimal("1"),
                        reference_price=Decimal("10"), idempotency_key="kill-key", source="test")

    def test_same_idempotency_key_and_pending_sell_reservation_cannot_oversell(self):
        with Session(self.engine) as db:
            initialize_stock_paper_account(db, FakeAlpaca())
            from app.models.stock_paper import StockPaperAccount, StockPaperPosition
            account = db.query(StockPaperAccount).one()
            db.add(StockPaperPosition(account_id=account.id, symbol="SPY", quantity=Decimal("1"),
                   observed_at=datetime.now(timezone.utc), raw_payload={}))
            db.commit()
            now = datetime.now(timezone.utc)
            with patch("app.services.stock_paper_ledger.session_bounds", return_value=(now.replace(hour=0), now.replace(hour=23))), \
                 patch("app.services.stock_paper_ledger.feed_status", return_value={"status": "ready"}), \
                 patch("app.services.stock_paper_ledger._validate_reference_price"):
                first = reserve_stock_paper_order(db, symbol="SPY", side="sell", quantity=Decimal("0.6"),
                    reference_price=Decimal("100"), idempotency_key="race-key-1", source="manual_close")
                self.assertEqual(reserve_stock_paper_order(db, symbol="SPY", side="sell", quantity=Decimal("0.6"),
                    reference_price=Decimal("100"), idempotency_key="race-key-1", source="manual_close").id, first.id)
                with self.assertRaisesRegex(StockPaperError, "short"):
                    reserve_stock_paper_order(db, symbol="SPY", side="sell", quantity=Decimal("0.5"),
                        reference_price=Decimal("100"), idempotency_key="race-key-2", source="manual_close")

    def test_pending_buys_apply_cumulative_symbol_notional_and_strategy_limits(self):
        with Session(self.engine) as db:
            initialize_stock_paper_account(db, FakeAlpaca())
            from app.models import Strategy
            now = datetime.now(timezone.utc)
            strategy = Strategy(name="Pending strategy", strategy_type="moving_average_crossover", parameters={},
                                is_active=True, current_status="paper_trading_active")
            db.add(strategy)
            db.flush()
            first_signal = self.add_approved_signal(db, strategy, "SPY", "one", now)
            second_signal = self.add_approved_signal(db, strategy, "SPY", "two", now)
            third_signal = self.add_approved_signal(db, strategy, "QQQ", "three", now)
            fourth_signal = self.add_approved_signal(db, strategy, "IWM", "four", now)
            db.add(RiskRule(name="pending-limits", value={"max_risk_per_trade": "0.02",
                "max_symbol_exposure": "0.015", "max_open_positions_per_strategy": 2}))
            db.commit()
            with ExitStack() as stack:
                for patcher in self.reserve_context(now):
                    stack.enter_context(patcher)
                reserve_stock_paper_order(db, symbol="SPY", side="buy", quantity=Decimal("0.1"),
                    reference_price=Decimal("100"), idempotency_key="pending-spy-one",
                    source="manual_control_room", signal_id=first_signal.id)
                with self.assertRaisesRegex(StockPaperError, "symbol risk"):
                    reserve_stock_paper_order(db, symbol="SPY", side="buy", quantity=Decimal("0.1"),
                        reference_price=Decimal("100"), idempotency_key="pending-spy-two",
                        source="manual_control_room", signal_id=second_signal.id)
                reserve_stock_paper_order(db, symbol="QQQ", side="buy", quantity=Decimal("0.1"),
                    reference_price=Decimal("100"), idempotency_key="pending-qqq",
                    source="manual_control_room", signal_id=third_signal.id)
                with self.assertRaisesRegex(StockPaperError, "too many open positions"):
                    reserve_stock_paper_order(db, symbol="IWM", side="buy", quantity=Decimal("0.1"),
                        reference_price=Decimal("100"), idempotency_key="pending-iwm",
                        source="manual_control_room", signal_id=fourth_signal.id)

    def test_filled_and_pending_positions_count_by_strategy_and_unknown_attribution_blocks(self):
        with Session(self.engine) as db:
            initialize_stock_paper_account(db, FakeAlpaca())
            from app.models import Strategy
            from app.models.stock_paper import StockPaperAccount, StockPaperFill, StockPaperOrder, StockPaperPosition
            now = datetime.now(timezone.utc)
            strategy = Strategy(name="Owned fill strategy", strategy_type="moving_average_crossover", parameters={},
                                is_active=True, current_status="paper_trading_active")
            db.add(strategy)
            db.flush()
            signals = [self.add_approved_signal(db, strategy, symbol, symbol, now) for symbol in ("SPY", "QQQ", "IWM")]
            account = db.query(StockPaperAccount).one()
            filled = StockPaperOrder(account_id=account.id, strategy_id=strategy.id, signal_id=signals[0].id,
                client_order_id="owned-fill", broker_order_id="owned-fill-broker", symbol="SPY", side="buy",
                quantity=Decimal("1"), order_type="limit", time_in_force="day", limit_price=Decimal("100"),
                reserved_cash=Decimal("0"), status="filled", source="manual_control_room")
            db.add(filled)
            db.flush()
            db.add_all([
                StockPaperFill(account_id=account.id, order_id=filled.id, broker_activity_id="owned-fill-activity",
                    broker_order_id=filled.broker_order_id, symbol="SPY", side="buy", quantity=Decimal("1"),
                    price=Decimal("100"), fee=None, cost_known=False, filled_at=now, raw_payload={}),
                StockPaperPosition(account_id=account.id, symbol="SPY", quantity=Decimal("1"),
                    market_value=Decimal("100"), observed_at=now, raw_payload={}),
                RiskRule(name="filled-plus-pending", value={"max_risk_per_trade": "0.02",
                    "max_open_positions": 3, "max_open_positions_per_strategy": 2}),
            ])
            db.commit()
            with ExitStack() as stack:
                for patcher in self.reserve_context(now):
                    stack.enter_context(patcher)
                reserve_stock_paper_order(db, symbol="QQQ", side="buy", quantity=Decimal("0.1"),
                    reference_price=Decimal("100"), idempotency_key="filled-pending-qqq",
                    source="manual_control_room", signal_id=signals[1].id)
                with self.assertRaisesRegex(StockPaperError, "too many open positions"):
                    reserve_stock_paper_order(db, symbol="IWM", side="buy", quantity=Decimal("0.1"),
                        reference_price=Decimal("100"), idempotency_key="filled-pending-iwm",
                        source="manual_control_room", signal_id=signals[2].id)
        with Session(self.engine) as db:
            from app.models.stock_paper import (
                StockPaperAccount, StockPaperBrokerActivity, StockPaperEquitySnapshot,
                StockPaperFill, StockPaperLedgerEvent, StockPaperOrder, StockPaperPosition,
            )
            for model in (StockPaperLedgerEvent, StockPaperEquitySnapshot, StockPaperFill,
                          StockPaperBrokerActivity, StockPaperOrder, StockPaperPosition, StockPaperAccount):
                db.query(model).delete()
            db.commit()
            initialize_stock_paper_account(db, FakeAlpaca())
            from app.models import Strategy
            from app.models.stock_paper import StockPaperAccount, StockPaperPosition
            now = datetime.now(timezone.utc)
            strategy = Strategy(name="Unknown ownership", strategy_type="rsi_mean_reversion", parameters={},
                                is_active=True, current_status="paper_trading_active")
            db.add(strategy)
            db.flush()
            signal = self.add_approved_signal(db, strategy, "QQQ", "unknown", now)
            account = db.query(StockPaperAccount).one()
            db.add(StockPaperPosition(account_id=account.id, symbol="SPY", quantity=Decimal("1"),
                market_value=Decimal("100"), observed_at=now, raw_payload={}))
            db.commit()
            with ExitStack() as stack:
                for patcher in self.reserve_context(now):
                    stack.enter_context(patcher)
                with self.assertRaisesRegex(StockPaperError, "unknown strategy attribution"):
                    reserve_stock_paper_order(db, symbol="QQQ", side="buy", quantity=Decimal("0.1"),
                        reference_price=Decimal("100"), idempotency_key="unknown-attribution",
                        source="manual_control_room", signal_id=signal.id)

    def test_signal_bound_buy_requires_active_evidence_and_deterministic_policy(self):
        with Session(self.engine) as db:
            initialize_stock_paper_account(db, FakeAlpaca())
            from app.models import Strategy, StrategySignal, StockPaperStrategyEvidence
            now = datetime.now(timezone.utc)
            strategy = Strategy(name="Signal strategy", strategy_type="moving_average_crossover", parameters={},
                                is_active=True, current_status="paper_trading_active")
            db.add(strategy)
            db.flush()
            db.add(StockPaperStrategyEvidence(strategy_id=strategy.id, evidence_id="evidence-1", status="approved",
                verified_drawdown=Decimal("0.01"), consecutive_losses=0, observed_at=now,
                expires_at=now + timedelta(days=1), provenance={"kind": "forward_evidence"}))
            good = StrategySignal(strategy_id=strategy.id, symbol="SPY", signal_time=now, action="BUY",
                probability_up=Decimal("0.8"), probability_down=Decimal("0.2"), confidence=Decimal("0.8"), features={})
            db.add(good)
            db.commit()
            with patch("app.services.stock_paper_ledger.session_bounds", return_value=(now.replace(hour=0), now.replace(hour=23))), \
                 patch("app.services.stock_paper_ledger.feed_status", return_value={"status": "ready"}), \
                 patch("app.services.stock_paper_ledger._validate_reference_price"):
                order = reserve_stock_paper_order(db, symbol="SPY", side="buy", quantity=Decimal("0.01"),
                    reference_price=Decimal("100"), idempotency_key="signal-buy-1", source="manual_control_room", signal_id=good.id)
                self.assertEqual(order.signal_id, good.id)
                strategy.current_status = "paused"
                db.commit()
                with self.assertRaisesRegex(StockPaperError, "not active"):
                    dispatch_reserved_order(db, order.id, FakeAlpaca())
                self.assertEqual(order.status, "reserved")
                strategy.current_status = "paper_trading_active"
                db.commit()
                low = StrategySignal(strategy_id=strategy.id, symbol="SPY", signal_time=now, action="BUY",
                    confidence=Decimal("0.1"), features={})
                db.add(low)
                db.commit()
                with self.assertRaisesRegex(StockPaperError, "Confidence"):
                    reserve_stock_paper_order(db, symbol="SPY", side="buy", quantity=Decimal("1"),
                        reference_price=Decimal("100"), idempotency_key="signal-buy-low", source="manual_control_room", signal_id=low.id)
                strategy.current_status = "paused"
                db.commit()
                with self.assertRaisesRegex(StockPaperError, "not active"):
                    reserve_stock_paper_order(db, symbol="SPY", side="buy", quantity=Decimal("1"),
                        reference_price=Decimal("100"), idempotency_key="signal-buy-paused", source="manual_control_room", signal_id=low.id)
                strategy.current_status = "paper_trading_active"
                db.query(StockPaperStrategyEvidence).filter_by(strategy_id=strategy.id).one().verified_drawdown = Decimal("0.5")
                drawdown_signal = StrategySignal(strategy_id=strategy.id, symbol="SPY", signal_time=now, action="BUY",
                    confidence=Decimal("0.8"), features={})
                db.add(drawdown_signal)
                db.commit()
                with self.assertRaisesRegex(StockPaperError, "drawdown"):
                    reserve_stock_paper_order(db, symbol="SPY", side="buy", quantity=Decimal("1"),
                        reference_price=Decimal("100"), idempotency_key="signal-buy-drawdown", source="manual_control_room", signal_id=drawdown_signal.id)


class StockPaperRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + str(Path(self.tmp.name) / "recovery.sqlite"))
        Base.metadata.create_all(self.engine)

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def test_independent_watchdog_pauses_when_monitor_evidence_is_missing(self):
        from app.models import StockPaperRecoveryState

        with Session(self.engine) as db:
            result = run_stock_watchdog(db)
            self.assertEqual(result["status"], "paused")
            state = db.get(StockPaperRecoveryState, 1)
            self.assertEqual(state.status, "cooldown")
            self.assertIn("heartbeat", state.pause_reason)

    def test_resume_requires_cooldown_and_fresh_monitoring_evidence(self):
        with Session(self.engine) as db:
            initialize_stock_paper_account(db, FakeAlpaca())
            run_stock_watchdog(db)
            with self.assertRaises(StockPaperError):
                resume_stock_paper_after_revalidation(db, actor="operator-test")

    def test_broker_cancellation_outage_leaves_paper_account_halted(self):
        from app.models.stock_paper import StockPaperAccount, StockPaperOrder
        from app.services.stock_recovery import cancel_open_stock_orders

        with Session(self.engine) as db:
            initialize_stock_paper_account(db, FakeAlpaca())
            account = db.query(StockPaperAccount).one()
            db.add(StockPaperOrder(
                account_id=account.id,
                client_order_id="sp-cancel-outage",
                broker_order_id="broker-order-1",
                symbol="SPY",
                side="sell",
                quantity=Decimal("1"),
                order_type="limit",
                time_in_force="day",
                limit_price=Decimal("100"),
                reserved_cash=Decimal("0"),
                status="accepted",
                source="manual_close",
            ))
            db.commit()
            gateway = FakeAlpaca()
            gateway.cancel_error = StockPaperError("broker unavailable")
            result = cancel_open_stock_orders(db, actor="operator-test", gateway=gateway)
            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["failures"])
            self.assertEqual(db.query(StockPaperAccount).one().status, "halted")


class StockPaperLegacyQuarantineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + str(Path(self.tmp.name) / "legacy.sqlite"))
        Base.metadata.create_all(self.engine)

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def test_legacy_mutation_entrypoints_are_quarantined_without_database_writes(self):
        from app.models import Strategy, StrategyMemory
        from app.services.candidate_activation import review_candidate_activation
        from app.services.decision_journal import update_strategy_memory_from_journal
        from app.services.memory_replay import run_memory_replay_gate_monitor
        from app.tasks import jobs
        with Session(self.engine) as db:
            strategy = Strategy(name="Quarantined strategy", strategy_type="moving_average_crossover",
                                parameters={}, is_active=True, current_status="research")
            db.add(strategy)
            db.commit()
            self.assertEqual(update_strategy_memory_from_journal(db)["status"], "quarantined")
            self.assertEqual(run_memory_replay_gate_monitor(db)["status"], "quarantined")
            activation = review_candidate_activation(db, "SPY", strategy.strategy_type, "activate", "legacy test")
            self.assertEqual(activation["status"], "quarantined")
            self.assertEqual(db.get(Strategy, strategy.id).current_status, "research")
            self.assertEqual(db.query(StrategyMemory).count(), 0)
        with patch.object(jobs, "_run_job", side_effect=lambda _name, work: work(None)):
            self.assertEqual(jobs.risk_monitor_job()["status"], "quarantined")
            self.assertEqual(jobs.memory_replay_gate_monitor_job()["status"], "quarantined")


class StockPaperRoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + str(Path(self.tmp.name) / "api.sqlite"), connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        app = FastAPI()
        app.add_middleware(AuthenticationMiddleware, configuration=Settings(_env_file=None,
            auth_viewer_key="v" * 32, auth_researcher_key="r" * 32,
            auth_operator_key="o" * 32, auth_admin_key="a" * 32))
        app.include_router(router, prefix="/api")
        def db_session():
            with Session(self.engine) as db:
                yield db
        app.dependency_overrides[get_db] = db_session
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        self.tmp.cleanup()

    def call(self, method, path, role="v", **kwargs):
        return getattr(self.client, method)("/api/stock-paper" + path, headers={"Authorization": "Bearer " + role * 32}, **kwargs)

    def test_role_matrix_and_uninitialized_status(self):
        self.assertEqual(required_role("GET", "/stock-paper/status"), "viewer")
        self.assertEqual(required_role("POST", "/stock-paper/initialize"), "admin")
        self.assertEqual(required_role("POST", "/stock-paper/reconcile"), "operator")
        self.assertEqual(required_role("POST", "/stock-paper/halt"), "operator")
        self.assertEqual(required_role("POST", "/stock-paper/resume"), "admin")
        self.assertEqual(required_role("POST", "/stock-paper/orders"), "operator")
        self.assertEqual(required_role("POST", "/stock-paper/orders/1/dispatch"), "operator")
        self.assertEqual(required_role("POST", "/stock-paper/positions/SPY/close"), "operator")
        self.assertEqual(self.call("get", "/status").status_code, 200)
        self.assertEqual(self.call("post", "/initialize", "o").status_code, 403)
        self.assertEqual(self.call("post", "/reconcile", "v").status_code, 403)
        self.assertEqual(self.call("post", "/halt", "r", json={"reason": "test"}).status_code, 403)
        self.assertEqual(self.call("post", "/resume", "o").status_code, 403)
        self.assertEqual(self.call("post", "/orders", "v", json={
            "symbol": "SPY", "side": "buy", "quantity": "1", "reference_price": "1", "idempotency_key": "order-key",
        }).status_code, 403)


if __name__ == "__main__":
    unittest.main()