from decimal import Decimal
from datetime import timedelta
from unittest.mock import Mock, patch
import unittest

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
import test_paper_execution as fixtures

from app.models.execution import PaperExecutionAccount, PaperOrderIntent, PaperTrialApproval
from app.models.shadow import ShadowDecision
from app.services.paper_execution import execution_transaction, mark_submitting, record_submission, apply_fill, reconcile
from app.services.paper_trial_cycle import run_paper_trial_cycle


class TrialCycleTests(unittest.TestCase):
    decision = fixtures.PaperExecutionTests.decision
    tearDown = fixtures.PaperExecutionTests.tearDown

    def setUp(self):
        fixtures.PaperExecutionTests.setUp(self)
        self.factory = sessionmaker(self.engine, autoflush=False)
        self.client = Mock()
        self.recovery = patch("app.services.paper_trial_cycle.run_recovery_cycle",
            return_value={"status": "success", "kill_switch": False})
        self.recovery.start()
        self.addCleanup(self.recovery.stop)
        self.posts = 0
        def dispatch(factory, client, ident):
            self.posts += 1
            with factory() as db, execution_transaction(db):
                mark_submitting(db, ident)
                record_submission(db, ident, outcome="unknown")
            return {"status": "unknown"}
        self.dispatch = patch("app.services.paper_trial_cycle.dispatch_intent", side_effect=dispatch)
        self.dispatch_mock = self.dispatch.start()
        self.addCleanup(self.dispatch.stop)

    def cycle(self):
        return run_paper_trial_cycle(self.factory, self.client, self.approval_id)

    def test_once_only_deterministic_intent_and_bounded_size(self):
        result = self.cycle()
        self.assertEqual(result["status"], "processed")
        self.assertFalse(result["eligible_for_qualification"])
        self.assertFalse(result["live_authorized"])
        again = self.cycle()
        self.assertEqual(again["status"], "waiting")
        self.assertEqual(self.posts, 1)
        with self.factory() as db:
            intent = db.scalar(select(PaperOrderIntent))
            self.assertEqual(intent.client_order_id, f"trial-{self.approval_id}-decision-1")
            self.assertEqual(intent.limit_price, Decimal("100.1"))
            self.assertLessEqual(intent.quantity * intent.limit_price, Decimal(500))
            self.assertLessEqual(intent.reserved_cash, Decimal(1000))

    def test_hold_and_no_position_sell_never_dispatch(self):
        for action, reason in (("hold", "model_hold"), ("sell", "no_position_no_shorting")):
            with self.factory.begin() as db:
                db.get(ShadowDecision, 1).intended_action = action
            self.assertEqual(self.cycle()["reason"], reason)
        self.assertEqual(self.posts, 0)

    def test_backfilled_and_revoked_approval_block(self):
        with self.factory.begin() as db:
            db.get(ShadowDecision, 1).backfilled = True
        self.assertEqual(self.cycle()["status"], "waiting")
        with self.factory.begin() as db:
            db.get(ShadowDecision, 1).backfilled = False
            db.get(PaperTrialApproval, self.approval_id).active = False
        self.assertEqual(self.cycle()["status"], "blocked")
        self.assertEqual(self.posts, 0)

    def test_recovery_halt_is_not_cleared(self):
        with patch("app.services.paper_trial_cycle.run_recovery_cycle",
                   return_value={"status": "success", "kill_switch": True}):
            self.assertEqual(self.cycle()["status"], "blocked")
        with self.factory.begin() as db:
            db.get(PaperExecutionAccount, 1).kill_switch = True
        self.assertEqual(self.cycle()["status"], "blocked")
        self.assertEqual(self.posts, 0)

    def test_never_submitted_reservation_resumes_without_duplicate(self):
        with patch("app.services.paper_trial_cycle.dispatch_intent", side_effect=RuntimeError("process stopped before dispatch")):
            with self.assertRaises(RuntimeError):
                self.cycle()
        self.assertEqual(self.cycle()["status"], "processed")
        self.assertEqual(self.posts, 1)
        with self.factory() as db:
            self.assertEqual(len(db.scalars(select(PaperOrderIntent)).all()), 1)

    def test_no_pyramiding_and_sell_uses_only_existing_inventory(self):
        result = self.cycle()
        with self.factory() as db, execution_transaction(db):
            order = db.get(PaperOrderIntent, result["intent_id"])
            record_submission(db, order.id, provider_order_id="confirmed-test-buy")
            apply_fill(db, order.id, provider_fill_id="confirmed-fill", quantity=order.quantity,
                price=order.limit_price, fee="0")
            reconcile(db, order.id, final_status="filled")
        self.assertEqual(self.cycle()["reason"], "decision_already_consumed")
        with self.factory.begin() as db:
            db.add(self.decision(2, "buy"))
        self.assertEqual(self.cycle()["reason"], "existing_position_no_pyramiding")
        with self.factory.begin() as db:
            decision = self.decision(3, "sell")
            decision.reference_price = 200
            db.add(decision)
        sale = self.cycle()
        with self.factory() as db:
            order = db.get(PaperOrderIntent, sale["intent_id"])
            self.assertEqual(order.side, "sell")
            self.assertEqual(order.limit_price, Decimal("199.8"))
            self.assertEqual(order.quantity, db.get(PaperExecutionAccount, 1).quantity)
            self.assertGreater(order.quantity * order.limit_price, Decimal(500))
        self.assertEqual(self.posts, 2)

    def test_stale_hold_is_waiting_for_fresh_data(self):
        with self.factory.begin() as db:
            decision = db.get(ShadowDecision, 1)
            decision.intended_action = "hold"
            decision.observed_at -= timedelta(hours=1)
        self.assertEqual(self.cycle()["reason"], "no_current_forward_decision")
        self.assertEqual(self.posts, 0)
