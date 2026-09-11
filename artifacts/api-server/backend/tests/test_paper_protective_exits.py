import unittest
from decimal import Decimal
from sqlalchemy import select, func
from sqlalchemy.orm import Session
import test_freqtrade_execution as fixtures

from app.models.execution import PaperExecutionAccount, PaperExternalFill
from app.services.paper_execution import execution_transaction, audit_account, PaperExecutionError
from app.services.freqtrade_dispatch import dispatch_intent
from app.services.paper_protective_exits import reconcile_provider_exits


class ProtectiveExitTests(unittest.TestCase):
    setUp = fixtures.DispatchTests.setUp
    tearDown = fixtures.DispatchTests.tearDown
    decision = fixtures.DispatchTests.decision
    reserve = fixtures.DispatchTests.reserve
    prepare = fixtures.DispatchTests.prepare

    def enter(self):
        intent = self.prepare()
        self.order.update(filled=2, cost=200, status="closed", is_open=False)
        dispatch_intent(self.factory, self.client, intent)
        self.exit_order = dict(self.order, order_id="protective", ft_order_side="sell",
                               filled=1, cost=95, safe_price=95, status="open", is_open=True)
        self.trade["orders"].append(self.exit_order)
        return intent

    def test_partial_exit_replay_and_full_exit_accounting(self):
        self.enter()
        self.assertEqual(reconcile_provider_exits(self.factory, self.client)["imported_external_fills"], 1)
        self.assertEqual(reconcile_provider_exits(self.factory, self.client)["imported_external_fills"], 0)
        self.exit_order.update(filled=2, cost=190, status="closed", is_open=False)
        reconcile_provider_exits(self.factory, self.client)
        with Session(self.engine) as db, execution_transaction(db):
            state = audit_account(db)
            self.assertEqual(Decimal(state["cash"]), Decimal("986.10"))
            self.assertEqual(Decimal(state["quantity"]), 0)
            self.assertTrue(db.get(PaperExecutionAccount, 1).kill_switch)
            self.assertEqual(db.scalar(select(func.count()).select_from(PaperExternalFill)), 2)
        self.client.exit.assert_not_called()

    def test_foreign_trade_or_overfill_never_changes_cash(self):
        self.enter()
        self.trade["enter_tag"] = "foreign"
        with self.assertRaises(PaperExecutionError):
            reconcile_provider_exits(self.factory, self.client)
        self.trade["enter_tag"] = "paper:unique-1"
        self.exit_order.update(filled=3, cost=285)
        with self.assertRaises(PaperExecutionError):
            reconcile_provider_exits(self.factory, self.client)
        with Session(self.engine) as db:
            self.assertEqual(db.get(PaperExecutionAccount, 1).cash, 798)
            self.assertEqual(db.scalar(select(func.count()).select_from(PaperExternalFill)), 0)

    def test_revision_rejected_and_reservation_released_on_external_exit(self):
        self.enter()
        with Session(self.engine) as db, execution_transaction(db):
            db.add(self.decision(2, "sell"))
            db.flush()
            reserved = self.reserve(db, decision_id=2, side="sell", client_order_id="sale").id
        reconcile_provider_exits(self.factory, self.client)
        from app.models.execution import PaperOrderIntent
        with Session(self.engine) as db:
            self.assertEqual(db.get(PaperOrderIntent, reserved).status, "abandoned")
            self.assertEqual(db.get(PaperExecutionAccount, 1).reserved_quantity, 0)
        self.exit_order.update(filled=.5, cost=47.5)
        with self.assertRaises(PaperExecutionError):
            reconcile_provider_exits(self.factory, self.client)

    def test_known_entry_cost_revision_is_rejected(self):
        self.enter()
        self.order.update(cost=201, safe_price=100.5)
        with self.assertRaises(PaperExecutionError):
            reconcile_provider_exits(self.factory, self.client)
        with Session(self.engine) as db:
            self.assertEqual(db.get(PaperExecutionAccount, 1).cash, 798)

    def test_duplicate_provider_order_identity_is_rejected(self):
        self.enter()
        self.trade["orders"].append(dict(self.exit_order, filled=2, cost=190, status="closed", is_open=False))
        with self.assertRaises(PaperExecutionError):
            reconcile_provider_exits(self.factory, self.client)

    def test_mutated_persisted_tag_cannot_rebind_owned_trade(self):
        entry_id = self.enter()
        from app.models.execution import PaperOrderIntent
        with Session(self.engine) as db, execution_transaction(db):
            entry = db.get(PaperOrderIntent, entry_id)
            entry.provider_context = dict(entry.provider_context, tag="foreign")
        self.trade["enter_tag"] = "foreign"
        with self.assertRaises(PaperExecutionError):
            reconcile_provider_exits(self.factory, self.client)

    def test_exhausted_trade_cannot_consume_later_trade_inventory(self):
        self.enter()
        self.exit_order.update(filled=2, cost=190, status="closed", is_open=False)
        reconcile_provider_exits(self.factory, self.client)
        old_trade = self.trade
        from app.services.paper_execution import set_kill_switch
        with Session(self.engine) as db, execution_transaction(db):
            set_kill_switch(db, False)  # Explicit operator review in this fixture.
            db.add(self.decision(2, "buy"))
            db.flush()
            later = self.reserve(db, decision_id=2, client_order_id="next-position").id
        new_order = dict(self.order, order_id="new-entry", filled=2, cost=200, status="closed", is_open=False)
        new_trade = dict(old_trade, trade_id=20, enter_tag="paper:next-position", amount=2, orders=[new_order])
        self.client.trades.return_value = []
        self.client.enter.return_value = {"trade_id": 20}
        self.client.trade.side_effect = lambda ident: {10: old_trade, 20: new_trade}[ident]
        dispatch_intent(self.factory, self.client, later)
        old_trade["orders"].append(dict(self.exit_order, order_id="spurious-extra", filled=1, cost=95))
        with self.assertRaises(PaperExecutionError):
            reconcile_provider_exits(self.factory, self.client)
        with Session(self.engine) as db:
            self.assertEqual(db.get(PaperExecutionAccount, 1).quantity, Decimal(2))
            self.assertEqual(db.scalar(select(func.count()).select_from(PaperExternalFill)), 1)
