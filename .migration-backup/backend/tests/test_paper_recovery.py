import unittest
from unittest.mock import patch
from sqlalchemy import select
from sqlalchemy.orm import Session
import test_freqtrade_execution as fixtures
from app.models.execution import PaperOrderIntent, PaperExecutionAccount
from app.models.models import AuditLog
from app.services.freqtrade_dispatch import dispatch_intent
from app.services.paper_execution import PaperExecutionError, execution_transaction
from app.services.paper_recovery import recover_intent, run_recovery_cycle


class RecoveryTests(unittest.TestCase):
    setUp = fixtures.DispatchTests.setUp
    tearDown = fixtures.DispatchTests.tearDown
    decision = fixtures.DispatchTests.decision
    reserve = fixtures.DispatchTests.reserve
    prepare = fixtures.DispatchTests.prepare

    def ambiguous(self):
        ident = self.prepare()
        self.client.enter.side_effect = TimeoutError()
        with self.assertRaises(PaperExecutionError):
            dispatch_intent(self.factory, self.client, ident)
        self.client.history.return_value = [self.trade]
        return ident

    def test_restart_recovers_exact_tag_without_post(self):
        ident = self.ambiguous()
        self.assertEqual(recover_intent(self.factory, self.client, ident)["status"], "submitted")
        recover_intent(self.factory, self.client, ident)
        self.assertEqual(self.client.enter.call_count, 1)
        self.client.exit.assert_not_called()
        with Session(self.engine) as db:
            self.assertEqual(db.get(PaperOrderIntent, ident).filled_quantity, 1)

    def test_missing_and_duplicate_tag_do_not_release_reserve(self):
        ident = self.ambiguous()
        for history in ([], [self.trade, dict(self.trade, trade_id=11)]):
            self.client.history.return_value = history
            with self.assertRaises(PaperExecutionError):
                recover_intent(self.factory, self.client, ident)
        with Session(self.engine) as db:
            self.assertEqual(db.get(PaperOrderIntent, ident).status, "unknown")
            self.assertGreater(db.get(PaperExecutionAccount, 1).reserved_cash, 0)

    def test_unresolved_cycle_halts_and_persists_sanitized_audit(self):
        ident = self.ambiguous()
        self.client.history.side_effect = RuntimeError("secret-token-123")
        result = run_recovery_cycle(self.factory, self.client)
        self.assertEqual(result["status"], "blocked")
        with Session(self.engine) as db:
            self.assertTrue(db.get(PaperExecutionAccount, 1).kill_switch)
            audits = db.scalars(select(AuditLog).where(AuditLog.event_type == "paper_recovery")).all()
            self.assertTrue(audits)
            self.assertNotIn("secret-token-123", str([(a.message, a.payload) for a in audits]))
        self.assertEqual(self.client.enter.call_count, 1)

    def test_inventory_drift_halts_without_pending_orders(self):
        self.prepare()
        self.client.trades.return_value = [self.trade]
        self.assertEqual(run_recovery_cycle(self.factory, self.client)["status"], "blocked")
        with Session(self.engine) as db:
            self.assertTrue(db.get(PaperExecutionAccount, 1).kill_switch)
        self.client.enter.assert_not_called()

    def test_successful_restart_cycle_never_clears_existing_halt(self):
        ident = self.ambiguous()
        self.order.update(filled=2, cost=200, status="closed", is_open=False)
        self.trade["amount"] = 2
        self.client.trades.return_value = [self.trade]
        with Session(self.engine) as db, execution_transaction(db):
            db.get(PaperExecutionAccount, 1).kill_switch = True
        self.assertEqual(run_recovery_cycle(self.factory, self.client)["status"], "success")
        self.assertEqual(run_recovery_cycle(self.factory, self.client)["status"], "success")
        with Session(self.engine) as db:
            self.assertTrue(db.get(PaperExecutionAccount, 1).kill_switch)
            self.assertEqual(db.get(PaperExecutionAccount, 1).quantity, 2)
        self.assertEqual(self.client.enter.call_count, 1)
