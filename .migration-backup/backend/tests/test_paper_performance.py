import unittest
from sqlalchemy.orm import Session
import test_paper_protective_exits as fixtures
from app.services.paper_performance import paper_performance
from app.services.paper_protective_exits import reconcile_provider_exits
from app.services.paper_execution import execution_transaction, approve_trial
from app.services.freqtrade_dispatch import dispatch_intent


class PerformanceTests(unittest.TestCase):
    setUp = fixtures.ProtectiveExitTests.setUp
    tearDown = fixtures.ProtectiveExitTests.tearDown
    prepare = fixtures.ProtectiveExitTests.prepare
    decision = fixtures.ProtectiveExitTests.decision
    reserve = fixtures.ProtectiveExitTests.reserve
    enter = fixtures.ProtectiveExitTests.enter

    def test_open_position_is_not_profit_and_closed_loss_is_retained(self):
        self.enter()
        with Session(self.engine) as db:
            report = paper_performance(db, self.approval_id)
            self.assertEqual(report["completed_trades"], 0)
            self.assertIsNone(report["trades"][0]["realized_net_paper_pnl"])
        self.exit_order.update(filled=2, cost=190, status="closed", is_open=False)
        reconcile_provider_exits(self.factory, self.client)
        with Session(self.engine) as db:
            report = paper_performance(db, self.approval_id)
            self.assertEqual(report["completed_trades"], 1)
            self.assertEqual(report["positive_trades"], 0)
            self.assertEqual(report["negative_trades"], 1)
            self.assertEqual(report["realized_net_paper_pnl"], "-13.90000000")
            self.assertFalse(report["eligible_for_qualification"])

    def test_positive_observed_paper_trade_does_not_authorize_live(self):
        self.enter()
        self.exit_order.update(filled=2, cost=220, safe_price=110, status="closed", is_open=False)
        reconcile_provider_exits(self.factory, self.client)
        with Session(self.engine) as db:
            report = paper_performance(db, self.approval_id)
            self.assertEqual(report["positive_trades"], 1)
            self.assertEqual(report["realized_net_paper_pnl"], "15.80000000")
            self.assertFalse(report["live_authorized"])

    def test_fully_filled_but_open_external_order_waits_for_terminal_evidence(self):
        self.enter()
        self.exit_order.update(filled=2, cost=220, safe_price=110)
        reconcile_provider_exits(self.factory, self.client)
        with Session(self.engine) as db:
            self.assertEqual(paper_performance(db, self.approval_id)["completed_trades"], 0)
        self.exit_order.update(status="closed", is_open=False)
        reconcile_provider_exits(self.factory, self.client)
        reconcile_provider_exits(self.factory, self.client)
        with Session(self.engine) as db:
            report = paper_performance(db, self.approval_id)
            self.assertEqual(report["completed_trades"], 1)
            self.assertEqual(report["positive_trades"], 1)

    def test_rotated_approval_exit_does_not_hide_entry_trade_loss(self):
        self.enter()
        self.trade["orders"] = [self.order]
        self.trade["amount"] = 2
        with Session(self.engine) as db, execution_transaction(db):
            next_approval = approve_trial(db, binding_id=1, actor="operator:reviewed-rotation",
                max_notional="500", max_exposure="500", fee_rate="0.01")
            db.add(self.decision(2, "sell"))
            db.flush()
            sale = self.reserve(db, decision_id=2, approval_id=next_approval.id,
                side="sell", client_order_id="rotated-policy-sale").id
        self.client.trades.return_value = [self.trade]
        def exit_order(**kwargs):
            self.trade["orders"].append(dict(self.order, order_id="rotated-sale", ft_order_side="sell"))
            return {"result": "Created exit order"}
        self.client.exit.side_effect = exit_order
        dispatch_intent(self.factory, self.client, sale)
        with Session(self.engine) as db:
            report = paper_performance(db, self.approval_id)
            self.assertEqual(report["completed_trades"], 1)
            self.assertEqual(report["negative_trades"], 1)
            self.assertEqual(float(report["realized_net_paper_pnl"]), -4)

    def test_fully_filled_external_order_waits_for_terminal_reconciliation(self):
        self.enter()
        self.exit_order.update(filled=2, cost=190, status="open", is_open=True)
        reconcile_provider_exits(self.factory, self.client)
        with Session(self.engine) as db:
            report = paper_performance(db, self.approval_id)
            self.assertEqual(report["completed_trades"], 0)
            self.assertTrue(report["trades"][0]["provider_reconciliation_pending"])
        self.exit_order.update(status="closed", is_open=False)
        reconcile_provider_exits(self.factory, self.client)
        with Session(self.engine) as db:
            report = paper_performance(db, self.approval_id)
            self.assertEqual(report["completed_trades"], 1)
            self.assertEqual(float(report["realized_net_paper_pnl"]), -13.9)
            self.assertFalse(report["trades"][0]["provider_reconciliation_pending"])
