"""Reconciliation asks the broker about an open order only when the history cannot answer.

Every pass used to look up each open order by client id inside the account
lock (2026-11-13: 11,752 lookups for 28 orders). The fetched order history
already holds nearly all of them. These tests hold three things:

* an order the history fully answers for is not looked up;
* every case the history cannot answer for still is (the fail-closed path);
* skipping leaves the ledger exactly as looking every order up would.

Every test uses fake gateways; nothing reaches a broker.
"""
import tempfile
import unittest
from contextlib import ExitStack
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401  register all tables
from app.db.base import Base
from app.models.stock_paper import StockPaperOrder
from app.services import stock_paper_ledger as ledger
from app.services.stock_paper_ledger import (
    AlpacaPaperClient,
    StockPaperUnavailable,
    broker_stop_coverage,
    dispatch_reserved_order,
    reconcile_stock_paper_account,
)
from tests.test_stock_paper_bracket_exits import (
    PARENT, SL_LEG, TP_LEG, BracketGateway, BracketLedgerTests, _account_row, _leg, _parent,
)
from tests.test_stock_paper_ledger import FakeAlpaca

TP_CLIENT, SL_CLIENT = f"alpaca-{TP_LEG}", f"alpaca-{SL_LEG}"


class Recording(FakeAlpaca):
    """FakeAlpaca that remembers which client ids were looked up."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.looked_up = []

    def order_by_client_id(self, client_order_id):
        self.looked_up.append(client_order_id)
        return super().order_by_client_id(client_order_id)


class LegLessGateway(BracketGateway):
    """Accepts a bracket but answers the POST without its child legs."""

    def submit_order(self, payload):
        self.calls += 1
        return _parent(payload["client_order_id"], "accepted", legs=None)


def _order(**fields):
    base = {"id": 1, "client_order_id": "c1", "broker_order_id": "b1", "uncertain_submission": False,
            "leg_role": None, "parent_order_id": None, "order_class": "simple"}
    return SimpleNamespace(**(base | fields))


class HistoryAnswersTests(unittest.TestCase):
    """The decision itself, case by case."""

    HISTORY = {"c1": {"id": "b1", "client_order_id": "c1"}}

    def answers(self, order, roles=None, history=None):
        return ledger._history_already_answers(order, self.HISTORY if history is None else history, roles or {})

    def test_a_child_leg_the_history_shows_needs_no_lookup(self):
        self.assertTrue(self.answers(_order(leg_role="stop_loss", parent_order_id=7, order_class="bracket")))

    def test_a_simple_order_the_history_shows_needs_no_lookup(self):
        self.assertTrue(self.answers(_order()))

    def test_a_bracket_entry_needs_both_exits_linked(self):
        entry = _order(order_class="bracket")
        self.assertFalse(self.answers(entry))
        self.assertFalse(self.answers(entry, {1: {"take_profit"}}))
        self.assertFalse(self.answers(entry, {1: {"stop_loss"}}))
        self.assertTrue(self.answers(entry, {1: {"take_profit", "stop_loss"}}))

    def test_an_oco_root_needs_its_stop_linked(self):
        root = _order(order_class="oco", leg_role="take_profit")
        self.assertFalse(self.answers(root))
        self.assertTrue(self.answers(root, {1: {"stop_loss"}}))

    def test_other_order_classes_are_always_asked(self):
        for order_class in ("oto", "", None, "mleg"):
            self.assertFalse(self.answers(_order(order_class=order_class), {1: {"take_profit", "stop_loss"}}), order_class)

    def test_anything_the_history_cannot_vouch_for_is_asked(self):
        leg = {"leg_role": "stop_loss", "parent_order_id": 7}
        self.assertFalse(self.answers(_order(client_order_id="absent", **leg)))
        self.assertFalse(self.answers(_order(broker_order_id=None, **leg)))
        self.assertFalse(self.answers(_order(broker_order_id="", **leg)))
        self.assertFalse(self.answers(_order(broker_order_id="another-broker-order", **leg)))
        self.assertFalse(self.answers(_order(uncertain_submission=True, **leg)))
        self.assertFalse(self.answers(_order(**leg), history={"c1": None}))

    def test_two_history_rows_with_one_client_id_vouch_for_neither(self):
        rows = [{"id": "b1", "client_order_id": "c1"}, {"id": "b2", "client_order_id": "c1"},
                {"id": "b3", "client_order_id": "c3"}, {"id": "b4"}]
        history = ledger._history_by_client_id(rows)
        self.assertIsNone(history["c1"])
        self.assertEqual(history["c3"]["id"], "b3")
        self.assertEqual(set(history), {"c1", "c3"})


class ReconcileLookupTests(unittest.TestCase):
    setUp = BracketLedgerTests.setUp
    tearDown = BracketLedgerTests.tearDown
    gates = BracketLedgerTests.gates
    seed = BracketLedgerTests.seed
    reserve_entry = BracketLedgerTests.reserve_entry
    submit_entry = BracketLedgerTests.submit_entry

    # The three broker states a bracket passes through, as Alpaca lists them (nested=false).
    def open_state(self, parent):
        return dict(account=_account_row(), orders=[
            _parent(parent.client_order_id, "accepted"), _leg(TP_LEG, "take_profit", "held"),
            _leg(SL_LEG, "stop_loss", "held"),
        ])

    def filled_state(self, parent):
        return dict(
            account=_account_row(cash="99800"),
            positions=[{"symbol": "SPY", "qty": "2", "avg_entry_price": "100", "market_value": "200",
                        "current_price": "100"}],
            orders=[_parent(parent.client_order_id, "filled", filled_qty="2"),
                    _leg(TP_LEG, "take_profit", "new"), _leg(SL_LEG, "stop_loss", "held")],
            activities=[self.entry_fill()],
        )

    def stopped_state(self, parent):
        return dict(
            account=_account_row(cash="99994"), positions=[],
            orders=[_parent(parent.client_order_id, "filled", filled_qty="2"),
                    _leg(TP_LEG, "take_profit", "canceled"), _leg(SL_LEG, "stop_loss", "filled", filled_qty="2")],
            activities=[self.entry_fill(), {
                "id": "fill-stop", "activity_type": "FILL", "order_id": SL_LEG, "symbol": "SPY", "side": "sell",
                "qty": "2", "price": "97", "commission": "0",
                "transaction_time": (self.now + timedelta(minutes=5)).isoformat()}],
        )

    def entry_fill(self):
        return {"id": "fill-entry", "activity_type": "FILL", "order_id": PARENT, "symbol": "SPY", "side": "buy",
                "qty": "2", "price": "100", "commission": "0",
                "transaction_time": (self.now + timedelta(minutes=1)).isoformat()}

    @staticmethod
    def lookup_rows(state, parent):
        """What a client-id lookup of each order would return in this broker state."""
        entry, take_profit, stop = state["orders"]
        return {parent.client_order_id: {**entry, "legs": [take_profit, stop]},
                TP_CLIENT: take_profit, SL_CLIENT: stop}

    @staticmethod
    def ledger_rows(db):
        by_id = {row.id: row for row in db.query(StockPaperOrder).all()}
        return sorted(
            (row.client_order_id, row.broker_order_id, row.status, row.leg_role, row.order_class, row.source,
             str(row.quantity), row.strategy_id, row.uncertain_submission,
             by_id[row.parent_order_id].client_order_id if row.parent_order_id else None)
            for row in by_id.values()
        )

    def test_a_bracket_the_history_shows_is_never_looked_up(self):
        with Session(self.engine) as db:
            strategy, parent, _gateway = self.submit_entry(db)
            for state in (self.open_state, self.filled_state, self.stopped_state):
                gateway = Recording(**state(parent))
                result = reconcile_stock_paper_account(db, gateway)
                self.assertEqual(result["status"], "reconciled", result.get("reason"))
                self.assertEqual(gateway.looked_up, [], state.__name__)
                self.assertEqual(db.query(StockPaperOrder).filter_by(source="broker_import").count(), 0)
                if state == self.filled_state:
                    # The position keeps its broker-side stop.
                    self.assertEqual(broker_stop_coverage(db, parent.account_id, "SPY"), Decimal("2"))
            stop = db.query(StockPaperOrder).filter_by(broker_order_id=SL_LEG).one()
            self.assertEqual((stop.status, stop.leg_role, stop.strategy_id, stop.parent_order_id),
                             ("filled", "stop_loss", strategy.id, parent.id))
            self.assertEqual(db.query(StockPaperOrder).filter_by(broker_order_id=TP_LEG).one().status, "canceled")

    def test_skipping_leaves_the_ledger_as_looking_every_order_up_would(self):
        def run(always_ask):
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            engine = create_engine("sqlite:///" + str(Path(tmp.name) / "compare.sqlite"))
            self.addCleanup(engine.dispose)
            Base.metadata.create_all(engine)
            seen, lookups = [], 0
            with Session(engine) as db, ExitStack() as stack:
                if always_ask:
                    stack.enter_context(patch.object(ledger, "_history_already_answers", return_value=False))
                _strategy, parent, _gateway = self.submit_entry(db)
                for state in (self.open_state, self.filled_state, self.stopped_state):
                    broker = state(parent)
                    gateway = Recording(**broker, lookup_rows=self.lookup_rows(broker, parent))
                    result = reconcile_stock_paper_account(db, gateway)
                    lookups += len(gateway.looked_up)
                    seen.append((result["status"], result.get("reason"), self.ledger_rows(db)))
            return seen, lookups

        skipped, few = run(always_ask=False)
        asked, many = run(always_ask=True)
        self.assertEqual(skipped, asked)
        self.assertEqual(few, 0)
        # Entry and both legs while open, both legs once filled, one live leg at most after the stop.
        self.assertGreaterEqual(many, 5)

    def test_an_order_missing_from_the_history_is_still_looked_up(self):
        with Session(self.engine) as db:
            _strategy, parent, _gateway = self.submit_entry(db)
            broker = self.open_state(parent)
            rows = self.lookup_rows(broker, parent)
            broker["orders"] = broker["orders"][:2]  # the page does not show the stop leg
            gateway = Recording(**broker, lookup_rows=rows)
            result = reconcile_stock_paper_account(db, gateway)
            self.assertEqual(result["status"], "reconciled", result.get("reason"))
            # The stop itself, and its entry (one of the entry's exits is not in the history).
            self.assertEqual(sorted(gateway.looked_up), sorted([parent.client_order_id, SL_CLIENT]))
            stop = db.query(StockPaperOrder).filter_by(broker_order_id=SL_LEG).one()
            self.assertEqual((stop.status, stop.leg_role, stop.parent_order_id), ("held", "stop_loss", parent.id))
            self.assertEqual(db.query(StockPaperOrder).filter_by(source="broker_import").count(), 0)

    def test_an_order_neither_the_history_nor_the_lookup_shows_still_halts(self):
        with Session(self.engine) as db:
            _strategy, parent, _gateway = self.submit_entry(db)
            broker = self.open_state(parent)
            broker["orders"] = broker["orders"][:2]
            gateway = Recording(**broker)  # the lookup finds nothing either
            result = reconcile_stock_paper_account(db, gateway)
            self.assertIn(SL_CLIENT, gateway.looked_up)
            self.assertEqual(result["status"], "halted")

    def test_a_history_row_with_another_broker_id_is_not_trusted(self):
        with Session(self.engine) as db:
            _strategy, parent, _gateway = self.submit_entry(db)
            stop = db.query(StockPaperOrder).filter_by(broker_order_id=SL_LEG).one()
            stop.broker_order_id = "stale-broker-id"
            db.commit()
            broker = self.open_state(parent)
            gateway = Recording(**broker, lookup_rows=self.lookup_rows(broker, parent))
            reconcile_stock_paper_account(db, gateway)
            self.assertIn(SL_CLIENT, gateway.looked_up)
            self.assertNotIn(TP_CLIENT, gateway.looked_up)

    def test_an_uncertain_submission_is_looked_up_and_recovers_its_exits(self):
        with Session(self.engine) as db, ExitStack() as stack:
            self.gates(stack)
            _strategy, signal = self.seed(db)
            order = self.reserve_entry(db, signal)
            parent = dispatch_reserved_order(db, order.id, FakeAlpaca())  # the POST times out
            self.assertEqual(parent.status, "unknown")
            broker = self.open_state(parent)
            gateway = Recording(**broker, lookup_rows=self.lookup_rows(broker, parent))
            reconcile_stock_paper_account(db, gateway)
            self.assertEqual(gateway.looked_up, [parent.client_order_id])
            legs = db.query(StockPaperOrder).filter_by(parent_order_id=parent.id).all()
            self.assertEqual({leg.leg_role for leg in legs}, {"take_profit", "stop_loss"})
            self.assertEqual(db.query(StockPaperOrder).filter_by(source="broker_import").count(), 0)

    def test_an_entry_whose_exits_are_not_linked_is_looked_up_until_they_are(self):
        with Session(self.engine) as db:
            _strategy, signal = self.seed(db)
            with ExitStack() as stack:
                self.gates(stack)
                order = self.reserve_entry(db, signal)
                parent = dispatch_reserved_order(db, order.id, LegLessGateway())
            self.assertEqual(db.query(StockPaperOrder).filter_by(parent_order_id=parent.id).count(), 0)
            broker = self.open_state(parent)
            first = Recording(**broker, lookup_rows=self.lookup_rows(broker, parent))
            reconcile_stock_paper_account(db, first)
            self.assertEqual(first.looked_up, [parent.client_order_id])
            legs = db.query(StockPaperOrder).filter_by(parent_order_id=parent.id).all()
            self.assertEqual({leg.leg_role for leg in legs}, {"take_profit", "stop_loss"})
            self.assertEqual(db.query(StockPaperOrder).filter_by(source="broker_import").count(), 0)
            # Linked now: the next pass has nothing left to ask.
            second = Recording(**broker)
            self.assertEqual(reconcile_stock_paper_account(db, second)["status"], "reconciled")
            self.assertEqual(second.looked_up, [])


class OneConnectionTests(unittest.TestCase):
    def client(self, stack, built):
        real_client = httpx.Client

        def build(**kwargs):
            made = real_client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})),
                               **kwargs)
            built.append(made)
            return made

        stack.enter_context(patch.object(AlpacaPaperClient, "_headers", return_value={}))
        stack.enter_context(patch("app.services.stock_paper_ledger.httpx.Client", side_effect=build))
        return AlpacaPaperClient()

    def test_requests_open_their_own_connection_by_default(self):
        built = []
        with ExitStack() as stack:
            client = self.client(stack, built)
            client._request("GET", "/v2/account")
            client._request("GET", "/v2/positions")
        self.assertEqual(len(built), 2)
        self.assertTrue(all(made.is_closed for made in built))

    def test_requests_inside_the_block_share_one_connection_closed_at_its_end(self):
        built = []
        with ExitStack() as stack:
            client = self.client(stack, built)
            with client.one_connection():
                self.assertEqual(built, [])  # opened by the first request, not by the block
                for path in ("/v2/account", "/v2/positions", "/v2/orders"):
                    self.assertEqual(client._request("GET", path), {"ok": True})
                with client.one_connection():  # a nested block joins the outer one
                    client._request("GET", "/v2/account")
                self.assertEqual(len(built), 1)
                self.assertFalse(built[0].is_closed)
            self.assertTrue(built[0].is_closed)
            self.assertIsNone(client._kept_clients)
            client._request("GET", "/v2/account")
        self.assertEqual(len(built), 2)

    def test_the_connection_is_closed_when_the_block_fails(self):
        built = []
        with ExitStack() as stack:
            client = self.client(stack, built)
            with self.assertRaises(RuntimeError):
                with client.one_connection():
                    client._request("GET", "/v2/account")
                    raise RuntimeError("reconcile failed")
            self.assertTrue(built[0].is_closed)
            self.assertIsNone(client._kept_clients)

    def test_missing_credentials_fail_at_the_request_not_at_the_block(self):
        client = AlpacaPaperClient()
        with patch.object(type(ledger.settings), "paper_broker_credentials", return_value=("", "")):
            with client.one_connection():
                with self.assertRaises(StockPaperUnavailable):
                    client._request("GET", "/v2/account")
        self.assertIsNone(client._kept_clients)

    def test_two_clients_do_not_share_a_connection(self):
        first, second = AlpacaPaperClient(), AlpacaPaperClient()
        with first.one_connection():
            self.assertIsNotNone(first._kept_clients)
            self.assertIsNone(second._kept_clients)


class ReconcileSharesOneConnectionTests(unittest.TestCase):
    setUp = BracketLedgerTests.setUp
    tearDown = BracketLedgerTests.tearDown

    def test_every_broker_read_of_a_reconcile_runs_inside_one_connection_block(self):
        inside = []

        class Canned(AlpacaPaperClient):
            def account(self):
                inside.append(self._kept_clients is not None)
                return _account_row()

            def positions(self):
                inside.append(self._kept_clients is not None)
                return []

            def orders(self, after=None):
                inside.append(self._kept_clients is not None)
                return []

            def fills(self, after=None):
                inside.append(self._kept_clients is not None)
                return []

        with Session(self.engine) as db:
            ledger.initialize_stock_paper_account(db, FakeAlpaca(account=_account_row()))
            gateway = Canned()
            result = reconcile_stock_paper_account(db, gateway)
            self.assertEqual(result["status"], "reconciled", result.get("reason"))
            # Account, positions, orders and activities are each read at least once.
            self.assertGreaterEqual(len(inside), 4)
            self.assertTrue(all(inside), inside)
            self.assertIsNone(gateway._kept_clients)


if __name__ == "__main__":
    unittest.main()
