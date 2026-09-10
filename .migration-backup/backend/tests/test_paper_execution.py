import concurrent.futures
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
import hashlib
import json

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
import app.models  # register full migration metadata
from app.models.models import ResearchModelRun
from app.models.shadow import ShadowModelBinding, ShadowDecision
from app.models.execution import PaperExecutionAccount, PaperOrderIntent, PaperTrialApproval
from app.services.paper_execution import (PaperExecutionError, execution_transaction,
    create_account, approve_trial, set_kill_switch, reserve_intent, mark_submitting,
    record_submission, apply_fill, reconcile, abandon_unsubmitted, audit_account)
from app.services.shadow_pipeline import INSTRUMENT, NAMES, canonical, feature_config_id, register_shadow_model


class PaperExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + str(Path(self.tmp.name) / "ledger.sqlite"),
            connect_args={"timeout": 10})
        Base.metadata.create_all(self.engine)
        self.now = datetime.now(timezone.utc)
        spec = dict(version=1, run_id="a"*64, feature_config_id=feature_config_id(), feature_names=NAMES,
            mean=[0.]*10, scale=[1.]*10, coef=[.01]*10, intercept=0.,
            training_cutoff="2024-01-01T00:00:00+00:00", instrument=INSTRUMENT,
            timeframe_minutes=60, horizon_bars=1)
        self.spec_hash = hashlib.sha256(canonical(spec)).hexdigest()
        meta = dict(instrument_id=INSTRUMENT, timeframe_minutes=60, feature_config_id=feature_config_id(),
            horizon_bars=1, train_label_end=spec["training_cutoff"],
            files={"logistic_regression.json": self.spec_hash})
        with Session(self.engine) as db:
            db.add(ResearchModelRun(run_id="a" * 64, manifest_sha256=hashlib.sha256(canonical(meta)).hexdigest(),
                artifact_path="unused", status="experimental", eligible_for_trading=False,
                training_metadata=meta))
            db.flush()
            register_shadow_model(db, "a" * 64, spec, "test")
            db.add(self.decision(1, "buy"))
            db.commit()
        with Session(self.engine) as db, execution_transaction(db):
            create_account(db, "1000")
            self.approval_id = approve_trial(db, binding_id=1, actor="operator:test",
                max_notional="500", max_exposure="500", fee_rate="0.01").id
            set_kill_switch(db, False)

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def decision(self, ident, side):
        return ShadowDecision(id=ident, binding_id=1,
            bar_close=self.now.replace(tzinfo=None) + timedelta(seconds=ident),
            observed_at=self.now.replace(tzinfo=None), data_sha256="d" * 64,
            spec_sha256=self.spec_hash, probability=.8 if side == "buy" else .2,
            reference_price=100, intended_action=side, backfilled=False,
            eligible_for_qualification=False, latency_seconds=1)

    def reserve(self, db, **overrides):
        args = dict(decision_id=1, approval_id=self.approval_id, side="buy",
            quantity="2", limit_price="100", client_order_id="unique-1", now=self.now)
        args.update(overrides)
        return reserve_intent(db, **args)

    def submitted(self):
        with Session(self.engine) as db, execution_transaction(db):
            ident = self.reserve(db).id
            mark_submitting(db, ident)
            record_submission(db, ident, provider_order_id="remote1")
            return ident

    def test_duplicate_intent_is_idempotent_and_conflict_rejected(self):
        with Session(self.engine) as db, execution_transaction(db):
            first = self.reserve(db)
            self.assertEqual(self.reserve(db).id, first.id)
            with self.assertRaises(PaperExecutionError):
                self.reserve(db, quantity="3")
            with self.assertRaises(PaperExecutionError):
                self.reserve(db, client_order_id="different")
            self.assertEqual(db.get(PaperExecutionAccount, 1).reserved_cash, Decimal("202"))

    def test_partial_fills_round_trip_cash_and_inventory(self):
        ident = self.submitted()
        with Session(self.engine) as db, execution_transaction(db):
            apply_fill(db, ident, provider_fill_id="f1", quantity="1", price="99", fee=".5")
            apply_fill(db, ident, provider_fill_id="f1", quantity="1", price="99", fee=".5")
            account = db.get(PaperExecutionAccount, 1)
            self.assertEqual(account.cash, Decimal("900.5"))
            self.assertEqual(account.reserved_cash, Decimal("102.5"))
            self.assertEqual(account.quantity, Decimal("1"))
        with Session(self.engine) as db, execution_transaction(db):
            apply_fill(db, ident, provider_fill_id="f2", quantity="1", price="100", fee="1")
            reconcile(db, ident, final_status="filled")
            db.add(self.decision(2, "sell"))
        with Session(self.engine) as db, execution_transaction(db):
            sale = self.reserve(db, decision_id=2, side="sell", client_order_id="sell-1")
            mark_submitting(db, sale.id)
            record_submission(db, sale.id, provider_order_id="remote2")
            apply_fill(db, sale.id, provider_fill_id="f3", quantity="2", price="101", fee="1")
            reconcile(db, sale.id, final_status="filled")
            account = db.get(PaperExecutionAccount, 1)
            self.assertEqual(account.cash, Decimal("1000.5"))
            self.assertEqual(account.quantity, 0)
            self.assertEqual(account.reserved_cash, 0)
            self.assertEqual(account.reserved_quantity, 0)

    def test_rejection_releases_reserve(self):
        with Session(self.engine) as db, execution_transaction(db):
            order = self.reserve(db)
            mark_submitting(db, order.id)
            record_submission(db, order.id, outcome="rejected")
            self.assertEqual(db.get(PaperExecutionAccount, 1).reserved_cash, 0)
            self.assertEqual(order.status, "rejected")

    def test_unknown_blocks_new_orders_and_no_resubmit(self):
        with Session(self.engine) as db, execution_transaction(db):
            order = self.reserve(db)
            mark_submitting(db, order.id, provider_context={"entry_tag": "unique-1"})
            record_submission(db, order.id, outcome="unknown")
            db.add(self.decision(2, "buy"))
            db.flush()
            with self.assertRaises(PaperExecutionError):
                mark_submitting(db, order.id)
            with self.assertRaises(PaperExecutionError):
                self.reserve(db, decision_id=2, client_order_id="unique-2")

    def test_kill_switch_blocks_entry_but_reconciliation_allowed(self):
        ident = self.submitted()
        with Session(self.engine) as db, execution_transaction(db):
            set_kill_switch(db, True)
            apply_fill(db, ident, provider_fill_id="f1", quantity="1", price="100", fee="0")
            reconcile(db, ident, final_status="cancelled")
            db.add(self.decision(2, "buy"))
            db.flush()
            with self.assertRaises(PaperExecutionError):
                self.reserve(db, decision_id=2, client_order_id="unique-2")

    def test_model_mismatch_backfill_stale_and_bad_price_fail(self):
        with Session(self.engine) as db, execution_transaction(db):
            decision = db.get(ShadowDecision, 1)
            for field, bad in (("spec_sha256", "x" * 64), ("backfilled", True),
                               ("observed_at", self.now.replace(tzinfo=None) - timedelta(hours=1))):
                previous = getattr(decision, field)
                setattr(decision, field, bad)
                with self.assertRaises(PaperExecutionError):
                    self.reserve(db)
                setattr(decision, field, previous)
            with self.assertRaises(PaperExecutionError):
                self.reserve(db, limit_price="102")

    def test_limits_oversell_and_nan(self):
        with Session(self.engine) as db, execution_transaction(db):
            with self.assertRaises(PaperExecutionError):
                self.reserve(db, quantity="6")
            with self.assertRaises(PaperExecutionError):
                self.reserve(db, quantity="NaN")
            db.get(ShadowDecision, 1).intended_action = "sell"
            with self.assertRaises(PaperExecutionError):
                self.reserve(db, side="sell")

    def test_conflicting_and_excess_fills_roll_back(self):
        ident = self.submitted()
        with Session(self.engine) as db, execution_transaction(db):
            apply_fill(db, ident, provider_fill_id="f1", quantity="1", price="100", fee="0")
        with Session(self.engine) as db, self.assertRaises(PaperExecutionError), execution_transaction(db):
            apply_fill(db, ident, provider_fill_id="f1", quantity="2", price="100", fee="0")
        with Session(self.engine) as db, self.assertRaises(PaperExecutionError), execution_transaction(db):
            apply_fill(db, ident, provider_fill_id="f2", quantity="2", price="100", fee="0")
        with Session(self.engine) as db:
            self.assertEqual(db.get(PaperExecutionAccount, 1).cash, Decimal("900"))

    def test_parallel_duplicate_reserves_once(self):
        def submit():
            with Session(self.engine) as db, execution_transaction(db):
                return self.reserve(db).id
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(ids[0], ids[1])
        with Session(self.engine) as db:
            self.assertEqual(len(db.scalars(select(PaperOrderIntent)).all()), 1)
            self.assertEqual(db.get(PaperExecutionAccount, 1).reserved_cash, Decimal("202"))

    def test_mutation_requires_serialized_transaction(self):
        with Session(self.engine) as db, self.assertRaises(PaperExecutionError):
            set_kill_switch(db, False)

    def test_changed_approved_policy_is_rejected(self):
        with Session(self.engine) as db, execution_transaction(db):
            db.get(PaperTrialApproval, self.approval_id).max_notional = Decimal("600")
            with self.assertRaises(PaperExecutionError):
                self.reserve(db)

    def test_fractional_fill_roundtrip_has_exact_storage(self):
        with Session(self.engine) as db, execution_transaction(db):
            order = self.reserve(db, quantity="0.12345678", limit_price="100.00000001")
            ident = order.id
            mark_submitting(db, ident)
            record_submission(db, ident, provider_order_id="fractional")
        with Session(self.engine) as db, execution_transaction(db):
            apply_fill(db, ident, provider_fill_id="fractional1", quantity="0.12345678",
                price="100.00000001", fee="0.01234567")
            reconcile(db, ident, final_status="filled")
        with Session(self.engine) as db:
            account = db.get(PaperExecutionAccount, 1)
            self.assertEqual(account.cash, Decimal("987.64197633"))
            self.assertEqual(account.quantity, Decimal("0.12345678"))
            self.assertEqual(account.reserved_cash, Decimal("0"))

    def test_submit_rechecks_approval_age_and_binding(self):
        with Session(self.engine) as db, execution_transaction(db):
            order = self.reserve(db)
            with self.assertRaises(PaperExecutionError):
                mark_submitting(db, order.id, now=self.now + timedelta(minutes=16))
            approval = db.get(PaperTrialApproval, self.approval_id)
            approval.active = False
            with self.assertRaises(PaperExecutionError):
                mark_submitting(db, order.id)
            approval.active = True
            binding = db.get(ShadowModelBinding, 1)
            binding.spec = dict(binding.spec, intercept=100)
            with self.assertRaises(PaperExecutionError):
                mark_submitting(db, order.id)

    def test_unsubmitted_order_cannot_claim_remote_terminal_state(self):
        with Session(self.engine) as db, execution_transaction(db):
            order = self.reserve(db)
            for state in ("filled", "cancelled", "rejected"):
                with self.assertRaises(PaperExecutionError):
                    reconcile(db, order.id, final_status=state)

    def test_autoflush_disabled_preserves_kill_switch_during_reconcile(self):
        ident = self.submitted()
        with Session(self.engine, autoflush=False) as db, execution_transaction(db):
            set_kill_switch(db, True)
            apply_fill(db, ident, provider_fill_id="kill-f1", quantity="1", price="100", fee="0")
            reconcile(db, ident, final_status="cancelled")
        with Session(self.engine) as db:
            self.assertTrue(db.get(PaperExecutionAccount, 1).kill_switch)

    def test_abandon_only_before_any_submission(self):
        with Session(self.engine) as db, execution_transaction(db):
            order = self.reserve(db)
            abandon_unsubmitted(db, order.id)
            self.assertEqual(db.get(PaperExecutionAccount, 1).reserved_cash, 0)
            self.assertEqual(order.status, "abandoned")
            db.add(self.decision(2, "buy"))
            db.flush()
            second = self.reserve(db, decision_id=2, client_order_id="later")
            mark_submitting(db, second.id)
            with self.assertRaises(PaperExecutionError):
                abandon_unsubmitted(db, second.id)

    def test_audit_recomputes_fill_accounting_and_detects_corruption(self):
        ident = self.submitted()
        with Session(self.engine) as db, execution_transaction(db):
            apply_fill(db, ident, provider_fill_id="audit-f1", quantity="1", price="99", fee=".1")
            self.assertTrue(audit_account(db)["accounting_consistent"])
            db.get(PaperExecutionAccount, 1).cash += Decimal("1")
            with self.assertRaises(PaperExecutionError):
                audit_account(db)

    def test_full_profitable_exit_may_exceed_entry_notional_cap(self):
        ident = self.submitted()
        with Session(self.engine) as db, execution_transaction(db):
            apply_fill(db, ident, provider_fill_id="profit-buy", quantity="2", price="100", fee="0")
            reconcile(db, ident, final_status="filled")
            decision = self.decision(2, "sell")
            decision.reference_price = 300
            db.add(decision)
            db.flush()
            sale = self.reserve(db, decision_id=2, side="sell", client_order_id="profit-exit", limit_price="300")
            self.assertGreater(sale.quantity * sale.limit_price, Decimal("500"))
            mark_submitting(db, sale.id)
            record_submission(db, sale.id, provider_order_id="profit-sale")
            apply_fill(db, sale.id, provider_fill_id="profit-sell", quantity="2", price="300", fee="0")
            reconcile(db, sale.id, final_status="filled")
            self.assertEqual(db.get(PaperExecutionAccount, 1).quantity, 0)
            self.assertEqual(db.get(PaperExecutionAccount, 1).cash, Decimal("1400"))

    def test_old_version_one_approval_is_not_silently_upgraded(self):
        with Session(self.engine) as db, execution_transaction(db):
            approval = db.get(PaperTrialApproval, self.approval_id)
            old_policy = dict(version=1, binding_id=1, model_run_id="a"*64,
                binding_hash=self.spec_hash, max_notional="500", max_exposure="500",
                fee_rate="0.01", nonqualifying=True, max_decision_age_seconds=900)
            approval.policy_hash = hashlib.sha256(json.dumps(old_policy, sort_keys=True).encode()).hexdigest()
            with self.assertRaises(PaperExecutionError):
                self.reserve(db)


if __name__ == "__main__":
    unittest.main()
