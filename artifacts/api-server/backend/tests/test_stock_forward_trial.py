import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch
import numpy as np
import pandas as pd
import joblib

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.models
from app.api.stock_forward_trial import router
from app.core.security import required_role
from app.db.base import Base
from app.models import (
    IntradayBar, MarketPrice, StockDatasetSnapshot, StockModelRegistry, StockPaperAccount,
    StockPaperModelBinding, StockPaperTrial, StockPaperTrialDecision,
    StockPaperOrder, StockPaperFill, StockPaperTrialLot, Strategy,
)
from app.services.stock_forward_trial import (
    POLICY, _hash, create_trial, evaluate_trial, observe_trial, record_decision,
    start_trial, validate_trial_artifact,
    execute_pending_decisions, _trial_allocated_notional, stop_trial,
    _trial_equity_curve_max_drawdown, _evidence_allows_trade,
)
from app.tasks.jobs import stock_forward_trial_observe_job
from app.services.stock_training_jobs import StockTrainingError
from app.services.stock_training import FEATURES


class ForwardTrialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + str(Path(self.tmp.name) / "trial.sqlite"))
        Base.metadata.create_all(self.engine)

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def lineage(self, universe=("SPY",)):
        return {
            "model_run_id": "run-1", "model_hash": "m" * 64, "snapshot_id": "snap-1",
            "dataset_sha256": "d" * 64, "cutoff_date": "2025-01-01",
            "universe": list(universe), "cost_assumptions": {"commission": "unknown"},
            "binding_hash": "b" * 64, "policy_sha256": _hash(POLICY),
        }

    def trial(self, db, status="running", universe=("SPY",), trial_id=None):
        row = StockPaperTrial(
            id=trial_id or "00000000-0000-0000-0000-000000000001", binding_id=1, actor="test",
            status=status, policy=dict(POLICY), lineage=self.lineage(universe),
        )
        db.add(row)
        db.flush()
        return row

    def account(self, db, reconciled=True):
        db.add(StockPaperAccount(
            broker="alpaca_paper", broker_account_id="acct", currency="USD",
            cash=Decimal("1000"), buying_power=Decimal("1000"), equity=Decimal("1000"),
            last_equity=Decimal("1000"), status="reconciled" if reconciled else "halted",
            costs_known=False, accounting_verified=reconciled,
            reconciliation_required=not reconciled, unexplained_residual=False, raw_payload={},
        ))
        db.flush()

    def test_policy_is_exact_frozen_value(self):
        self.assertEqual(POLICY, {
            "regular_sessions": 20, "minimum_closed_trades": 30,
            "minimum_decision_coverage": "0.90", "max_allocated_notional": "10000",
            "max_risk_per_trade": "0.0025", "auto_pause_drawdown": "0.02",
            "paper_only": True, "live_authorized": False,
        })

    def test_invalid_binding_cannot_create_trial(self):
        with Session(self.engine) as db:
            with self.assertRaisesRegex(StockTrainingError, "paper-only"):
                create_trial(db, binding_id=999, actor="operator")

    def test_lineage_digest_tamper_is_rejected(self):
        with Session(self.engine) as db:
            row = self.trial(db)
            row.lineage["lineage_sha256"] = "tampered"
            snapshot = MagicMock(snapshot_id="snap-1")
            model = MagicMock(snapshot_id="snap-1", manifest_sha256="m" * 64)
            binding = MagicMock(model_run_id="run-1", snapshot_id="snap-1",
                                binding_sha256="b" * 64)
            with patch.object(db, "get", side_effect=lambda cls, key:
                              binding if cls is StockPaperModelBinding else
                              model if cls is StockModelRegistry else snapshot), \
                 patch("app.services.stock_forward_trial.validate_registered_stock_model",
                       return_value={}), \
                 patch("app.services.stock_forward_trial._dataset_from_record", return_value=MagicMock()):
                with self.assertRaisesRegex(StockTrainingError, "digest mismatch"):
                    validate_trial_artifact(db, row)

    def test_start_unreconciled_is_blocked(self):
        with Session(self.engine) as db:
            row = self.trial(db, status="approved")
            self.account(db, reconciled=False)
            with patch("app.services.stock_forward_trial.validate_trial_artifact", return_value={}):
                result = start_trial(db, row.id)
            self.assertEqual(result.status, "blocked")
            self.assertIn("not reconciled", result.blocked_reason)

    def test_start_feed_unavailable_blocks_during_session(self):
        with Session(self.engine) as db:
            row = self.trial(db, status="approved")
            self.account(db)
            now = datetime(2025, 1, 2, 15, 0, tzinfo=timezone.utc)
            with patch("app.services.stock_forward_trial._now", return_value=now), \
                 patch("app.services.stock_forward_trial.session_bounds", return_value=(
                     datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc),
                     datetime(2025, 1, 2, 21, tzinfo=timezone.utc))), \
                 patch("app.services.stock_forward_trial.feed_status",
                       return_value={"status": "unavailable", "unavailable_reason": "provider outage"}), \
                 patch("app.services.stock_forward_trial.validate_trial_artifact", return_value={}):
                result = start_trial(db, row.id)
            self.assertEqual(result.status, "blocked")
            self.assertIn("provider outage", result.blocked_reason)

    def test_transient_block_clears_but_immutable_block_does_not(self):
        with Session(self.engine) as db:
            row = self.trial(db, status="blocked")
            self.account(db)
            row.blocked_reason = "fresh_complete_feed_required:SPY:stale"
            with patch("app.services.stock_forward_trial.validate_trial_artifact", return_value={}), \
                 patch("app.services.stock_forward_trial.feed_status", return_value={"status": "ready"}), \
                 patch("app.services.stock_forward_trial.session_bounds", return_value=None):
                result = start_trial(db, row.id)
            self.assertEqual(result.status, "running")
            self.assertIsNone(result.blocked_reason)

            row.status, row.blocked_reason = "blocked", "Snapshot is not binding eligible"
            with patch("app.services.stock_forward_trial.validate_trial_artifact", return_value={}), \
                 patch("app.services.stock_forward_trial.session_bounds", return_value=None):
                result = start_trial(db, row.id)
            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.blocked_reason, "Snapshot is not binding eligible")

    def test_observe_feed_outage_pauses_trial(self):
        with Session(self.engine) as db:
            row = self.trial(db)
            with patch("app.services.stock_forward_trial.feed_status",
                       return_value={"status": "stale", "unavailable_reason": "old bar"}), \
                 patch("app.services.stock_forward_trial.validate_trial_artifact", return_value={}):
                result = observe_trial(db, row.id)
            self.assertEqual(result["status"], "paused")
            self.assertIn("old bar", row.pause_reason)

    def test_daily_model_rejects_fresh_bar_without_order(self):
        with Session(self.engine) as db:
            row = self.trial(db)
            when = datetime(2025, 1, 2, 15, 0, tzinfo=timezone.utc)
            db.add(IntradayBar(symbol="SPY", timeframe="1m", opened_at=when,
                open=Decimal("10"), high=Decimal("11"), low=Decimal("9"),
                close=Decimal("10"), volume=100, provider="test",
                feed_class="sip", exchange_timestamp=when))
            db.flush()
            with patch("app.services.stock_forward_trial.feed_status", return_value={"status": "ready"}), \
                 patch("app.services.stock_forward_trial.validate_trial_artifact",
                       return_value={"inference_cadence": "daily"}), \
                 patch("app.services.stock_forward_trial._now", return_value=when), \
                 patch("app.services.stock_forward_trial.session_bounds", return_value=(
                     datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc),
                     datetime(2025, 1, 2, 21, tzinfo=timezone.utc))):
                result = observe_trial(db, row.id)
            self.assertEqual(result["decisions"], 0)
            self.assertEqual(result["reason"], "regular_session_not_closed")
            self.assertEqual(db.query(StockPaperTrialDecision).count(), 0)
            self.assertEqual(db.query(StockPaperOrder).count(), 0)

    def _post_close_patches(self, db, row, probability):
        """Return exact-artifact/model/calibrator patches plus a no-network order mock."""
        root = Path(self.tmp.name) / "selected"
        root.mkdir()
        (root / "selected_model.joblib").touch()
        (root / "calibrator.joblib").touch()
        db.add(StockModelRegistry(run_id="run-1", snapshot_id="snap-1",
            manifest_sha256="m" * 64, artifact_path=str(root), training_metadata={}))
        self.account(db)
        db.flush()
        now = datetime(2025, 1, 2, 21, 1, tzinfo=timezone.utc)
        opened = datetime(2025, 1, 2, 20, 59, tzinfo=timezone.utc)
        db.add(IntradayBar(symbol="SPY", timeframe="1m", opened_at=opened,
            open=Decimal("100"), high=Decimal("101"), low=Decimal("99"),
            close=Decimal("100"), volume=100, provider="alpaca",
            feed_class="sip", exchange_timestamp=opened))
        db.add(MarketPrice(symbol="SPY", price_date=date(2025, 1, 2),
            open=Decimal("100"), close=Decimal("100"), adjusted_close=Decimal("100"),
            high=Decimal("101"), low=Decimal("99"), volume=100, source="yfinance"))
        db.flush()
        featured = pd.DataFrame([{**{name: 1.0 for name in FEATURES}, "date": date(2025, 1, 1)}])
        model = MagicMock()
        model.predict_proba.return_value = np.array([[1 - probability, probability]])
        calibrator = MagicMock()

        def reserve(db_session, *, symbol, side, quantity, reference_price, idempotency_key, source, **kwargs):
            order = StockPaperOrder(account_id=1, client_order_id=idempotency_key,
                symbol=symbol, side=side, quantity=quantity, reserved_cash=Decimal("0"),
                status="reserved", source=source, strategy_id=kwargs.get("strategy_id"),
                signal_id=kwargs.get("signal_id"))
            db.add(order)
            db.flush()
            return order

        return [
            patch("app.services.stock_forward_trial._now", return_value=now),
            patch("app.services.stock_forward_trial.session_bounds", return_value=(
                datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc),
                datetime(2025, 1, 2, 21, tzinfo=timezone.utc))),
            patch("app.services.stock_forward_trial.feed_status", return_value={"status": "ready"}),
            patch("app.services.stock_forward_trial.validate_trial_artifact",
                  return_value={"inference_cadence": "daily"}),
            patch("app.services.stock_forward_trial.joblib.load",
                  side_effect=lambda path: model if "selected_model" in str(path) else calibrator),
            patch("app.services.stock_forward_trial.generate_features", return_value=featured),
            patch("app.services.stock_forward_trial._calibrated_probability",
                  return_value=np.array([probability])),
            patch("app.services.stock_forward_trial._evidence_allows_trade",
                  return_value=(True, None)),
            patch("app.services.stock_forward_trial.reserve_stock_paper_order",
                  side_effect=reserve),
            patch("app.services.stock_forward_trial.dispatch_reserved_order"),
        ], now, opened, model

    def test_post_close_fresh_bar_qualifying_buy_has_lineage_and_risk_size(self):
        with Session(self.engine) as db:
            row = self.trial(db)
            patches, now, opened, _ = self._post_close_patches(db, row, 0.8)
            with self.enter_contexts(patches):
                result = observe_trial(db, row.id)
            decision = db.scalar(select(StockPaperTrialDecision))
            self.assertEqual(result["decisions"], 1)
            self.assertEqual(decision.action, "buy")
            self.assertEqual(decision.lineage["execution_reference_timestamp"], opened.isoformat())
            self.assertEqual(decision.lineage["observation_timestamp"], now.isoformat())
            self.assertEqual(decision.lineage["feature_timestamp"], "2025-01-01T00:00:00+00:00")
            self.assertEqual(db.query(StockPaperOrder).count(), 0)

    def enter_contexts(self, patches):
        from contextlib import ExitStack
        stack = ExitStack()
        for item in patches:
            stack.enter_context(item)
        return stack

    def test_post_close_nonqualifying_model_persists_rejection(self):
        with Session(self.engine) as db:
            row = self.trial(db)
            patches, _, _, _ = self._post_close_patches(db, row, 0.2)
            with self.enter_contexts(patches):
                observe_trial(db, row.id)
            decision = db.scalar(select(StockPaperTrialDecision))
            self.assertEqual(decision.action, "reject")
            self.assertEqual(decision.rejection_reason, "model_signal_not_qualifying")
            self.assertEqual(db.query(StockPaperOrder).count(), 0)

    def test_pending_decision_executes_next_session_once_with_shared_risk_cap(self):
        with Session(self.engine) as db:
            row = self.trial(db)
            patches, _, _, _ = self._post_close_patches(db, row, 0.8)
            with self.enter_contexts(patches):
                observe_trial(db, row.id)
            strategy = Strategy(name="next-session-trial", strategy_type="test",
                parameters={}, is_active=True, current_status="paper_trading_active")
            db.add(strategy); db.flush()
            row.strategy_id = strategy.id
            next_open = datetime(2025, 1, 3, 14, 59, tzinfo=timezone.utc)
            db.add(IntradayBar(symbol="SPY", timeframe="1m", opened_at=next_open,
                open=Decimal("100"), high=Decimal("100"), low=Decimal("100"),
                close=Decimal("100"), volume=1, provider="alpaca",
                feed_class="sip", exchange_timestamp=next_open))
            db.flush()
            next_now = datetime(2025, 1, 3, 15, 0, tzinfo=timezone.utc)
            with self.enter_contexts(patches), \
                 patch("app.services.stock_forward_trial._now", return_value=next_now), \
                 patch("app.services.stock_forward_trial.session_bounds", side_effect=lambda d: (
                     datetime(d.year, d.month, d.day, 14, 30, tzinfo=timezone.utc),
                     datetime(d.year, d.month, d.day, 21, tzinfo=timezone.utc))):
                first = execute_pending_decisions(db, row.id)
                second = execute_pending_decisions(db, row.id)
            self.assertEqual(first["executed"], 1)
            self.assertEqual(second["executed"], 0)
            self.assertEqual(db.query(StockPaperOrder).count(), 1)
            self.assertIsNotNone(db.scalar(select(StockPaperTrialDecision)).order_id)

    def test_previous_session_rule_expires_stale_pending_decision(self):
        with Session(self.engine) as db:
            row = self.trial(db)
            self.account(db)
            row.baseline_equity = Decimal("1000")
            row.baseline_at = datetime(2025, 1, 2, tzinfo=timezone.utc)
            row.peak_equity = Decimal("1000")
            account = db.query(StockPaperAccount).one()
            account.source_timestamp = datetime.now(timezone.utc)
            account.last_reconciled_at = account.source_timestamp
            account.accounting_verified = True
            decision = StockPaperTrialDecision(trial_id=row.id, symbol="SPY",
                bar_timestamp=datetime(2025, 1, 1, 20, 59), decision_timestamp=datetime.now(timezone.utc),
                action="buy", qualifying=True, lineage={**row.lineage, "probability": 0.8})
            db.add(decision); db.flush()
            now = datetime(2025, 1, 3, 15, tzinfo=timezone.utc)
            with patch("app.services.stock_forward_trial._now", return_value=now), \
                 patch("app.services.stock_forward_trial.session_bounds", side_effect=lambda d: (
                     (datetime(d.year, d.month, d.day, 14, 30, tzinfo=timezone.utc),
                      datetime(d.year, d.month, d.day, 21, tzinfo=timezone.utc)))):
                result = execute_pending_decisions(db, row.id)
            self.assertEqual(result["executed"], 0)
            self.assertEqual(decision.rejection_reason, "stale_decision_expired")

    def test_forward_evidence_uses_account_and_owned_lots_not_holdout(self):
        from app.services.stock_forward_trial import _evidence_allows_trade
        with Session(self.engine) as db:
            row = self.trial(db)
            self.account(db)
            row.baseline_equity = Decimal("1000")
            row.baseline_at = datetime(2025, 1, 2, tzinfo=timezone.utc)
            row.peak_equity = Decimal("1000")
            manifest = {"final_holdout_metrics": {
                "cost_aware_nonoverlapping_returns": {"max_drawdown": "0.99", "losses": 99, "sample_count": 999}}}
            allowed, reason = _evidence_allows_trade(db, row, manifest,
                datetime(2025, 1, 2, tzinfo=timezone.utc))
            self.assertTrue(allowed)
            self.assertIsNone(reason)
            evidence = db.query(__import__("app.models", fromlist=["StockPaperStrategyEvidence"]).StockPaperStrategyEvidence).one()
            self.assertEqual(evidence.provenance["source"], "alpaca_account_and_trial_owned_lots")
            self.assertGreater(evidence.expires_at.replace(tzinfo=timezone.utc),
                               datetime(2025, 1, 2, tzinfo=timezone.utc))

    def test_lot_weighted_entry_fills_determine_stop_not_external_average(self):
        with Session(self.engine) as db:
            row = self.trial(db)
            self.account(db)
            strategy = Strategy(name="weighted-lot", strategy_type="test", parameters={},
                is_active=True, current_status="paper_trading_active")
            db.add(strategy); db.flush(); row.strategy_id = strategy.id
            order = StockPaperOrder(account_id=1, client_order_id="weighted-entry",
                symbol="SPY", side="buy", quantity=Decimal("2"), strategy_id=strategy.id,
                reserved_cash=Decimal("0"), status="filled", source="manual_control_room")
            db.add(order); db.flush()
            opened = datetime(2025, 1, 2, 20, 59)
            decision = StockPaperTrialDecision(trial_id=row.id, symbol="SPY",
                bar_timestamp=opened, decision_timestamp=opened, action="buy",
                qualifying=True, lineage=row.lineage, order_id=order.id)
            db.add(decision); db.flush()
            db.add(StockPaperTrialLot(trial_id=row.id, symbol="SPY",
                entry_decision_id=decision.id, entry_order_id=order.id, quantity=Decimal("2"),
                entry_session="2025-01-02", planned_horizon_sessions=5,
                stop_fraction=Decimal("0.02")))
            db.add_all([
                StockPaperFill(account_id=1, order_id=order.id, broker_activity_id="weighted-1",
                    broker_order_id="weighted", symbol="SPY", side="buy", quantity=Decimal("1"),
                    price=Decimal("100"), fee=Decimal("0"), cost_known=True,
                    filled_at=opened, raw_payload={}),
                StockPaperFill(account_id=1, order_id=order.id, broker_activity_id="weighted-2",
                    broker_order_id="weighted", symbol="SPY", side="buy", quantity=Decimal("1"),
                    price=Decimal("200"), fee=Decimal("0"), cost_known=True,
                    filled_at=opened, raw_payload={}),
            ])
            db.add(IntradayBar(symbol="SPY", timeframe="1m", opened_at=opened,
                open=Decimal("150"), high=Decimal("150"), low=Decimal("150"),
                close=Decimal("150"), volume=1, provider="alpaca",
                feed_class="sip", exchange_timestamp=opened))
            db.flush()
            with patch("app.services.stock_forward_trial._now",
                       return_value=datetime(2025, 1, 2, 20, 0, tzinfo=timezone.utc)), \
                 patch("app.services.stock_forward_trial.validate_trial_artifact", return_value={}), \
                 patch("app.services.stock_forward_trial.feed_status", return_value={"status": "ready"}), \
                 patch("app.services.stock_forward_trial.Path.is_file", return_value=True), \
                 patch("app.services.stock_forward_trial.joblib.load", return_value=MagicMock()), \
                 patch("app.services.stock_forward_trial.session_bounds", return_value=(
                     datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc),
                     datetime(2025, 1, 2, 21, tzinfo=timezone.utc))):
                observe_trial(db, row.id)
            self.assertIsNone(db.query(StockPaperTrialLot).one().exit_reason)

    def test_shared_reserve_notional_cap_is_enforced(self):
        from app.services.stock_paper_ledger import reserve_stock_paper_order, StockPaperError
        with Session(self.engine) as db:
            self.account(db)
            account = db.query(StockPaperAccount).one()
            account.source_timestamp = datetime.now(timezone.utc)
            account.last_reconciled_at = account.source_timestamp
            account.accounting_verified = True
            now = datetime.now(timezone.utc)
            with patch("app.services.stock_paper_ledger.session_bounds",
                       return_value=(now.replace(hour=14), now.replace(hour=21))), \
                 patch("app.services.stock_paper_ledger.feed_status", return_value={"status": "ready"}), \
                 patch("app.services.stock_paper_ledger._validate_reference_price"), \
                 patch("app.services.stock_paper_ledger._validate_signal_buy",
                       return_value=(None, None)):
                accepted = reserve_stock_paper_order(db, symbol="SPY", side="buy",
                    quantity=Decimal("0.1"), reference_price=Decimal("100"),
                    idempotency_key="cap-small", source="manual_control_room")
                self.assertEqual(accepted.quantity, Decimal("0.10000000"))
                with self.assertRaises(StockPaperError):
                    reserve_stock_paper_order(db, symbol="SPY", side="buy",
                        quantity=Decimal("2"), reference_price=Decimal("10000"),
                        idempotency_key="cap-large", source="manual_control_room")

    def test_horizon_exit_is_risk_reducing_and_never_short(self):
        with Session(self.engine) as db:
            row = self.trial(db)
            patches, _, opened, _ = self._post_close_patches(db, row, 0.2)
            # Existing trial-owned buy and five later session dates trigger horizon.
            strategy = Strategy(name="other-strategy", strategy_type="test", parameters={},
                is_active=True, current_status="paper_trading_active")
            db.add(strategy); db.flush()
            row.strategy_id = strategy.id
            order = StockPaperOrder(account_id=1, client_order_id="entry",
                symbol="SPY", side="buy", quantity=Decimal("2"), strategy_id=strategy.id,
                reserved_cash=Decimal("0"), status="filled", source="manual_control_room")
            db.add(order)
            db.flush()
            db.add(StockPaperTrialDecision(trial_id=row.id, symbol="SPY",
                bar_timestamp=opened, decision_timestamp=opened, action="buy",
                qualifying=True, lineage=row.lineage, order_id=order.id))
            db.add(StockPaperTrialLot(trial_id=row.id, symbol="SPY",
                entry_decision_id=db.query(StockPaperTrialDecision).one().id,
                entry_order_id=order.id, quantity=Decimal("2"),
                entry_session="2025-01-02", planned_horizon_sessions=5,
                stop_fraction=Decimal("0.02")))
            db.add(StockPaperFill(account_id=1, order_id=order.id,
                broker_activity_id="horizon-entry", broker_order_id="horizon-broker",
                symbol="SPY", side="buy", quantity=Decimal("2"), price=Decimal("100"),
                fee=Decimal("0"), cost_known=True, filled_at=opened, raw_payload={}))
            from app.models import StockPaperPosition
            db.add(StockPaperPosition(account_id=1, symbol="SPY", quantity=Decimal("2"),
                average_entry_price=Decimal("100"), current_price=Decimal("100"),
                market_value=Decimal("200"), observed_at=opened, raw_payload={}))
            for offset in range(1, 6):
                db.add(IntradayBar(symbol="SPY", timeframe="1m",
                    opened_at=opened + pd.Timedelta(days=offset), open=Decimal("100"),
                    high=Decimal("100"), low=Decimal("100"), close=Decimal("100"), volume=1,
                    provider="alpaca", feed_class="sip", exchange_timestamp=opened))
            db.flush()
            patches[0] = patch("app.services.stock_forward_trial._now",
                return_value=datetime(2025, 1, 9, 21, 1, tzinfo=timezone.utc))
            patches[1] = patch("app.services.stock_forward_trial.session_bounds",
                side_effect=lambda d: (datetime(d.year, d.month, d.day, 14, 30, tzinfo=timezone.utc),
                                       datetime(d.year, d.month, d.day, 21, tzinfo=timezone.utc)))
            with self.enter_contexts(patches):
                observe_trial(db, row.id)
            lot = db.query(StockPaperTrialLot).one()
            self.assertEqual(lot.exit_reason, "horizon")
            self.assertIsNone(lot.exit_order_id)
            self.assertEqual(db.query(StockPaperOrder).filter(StockPaperOrder.side == "sell").count(), 0)

    def test_stop_exit_is_risk_reducing_and_never_short(self):
        with Session(self.engine) as db:
            row = self.trial(db)
            patches, _, opened, _ = self._post_close_patches(db, row, 0.2)
            strategy = Strategy(name="stop-strategy", strategy_type="test", parameters={},
                is_active=True, current_status="paper_trading_active")
            db.add(strategy); db.flush()
            row.strategy_id = strategy.id
            order = StockPaperOrder(account_id=1, client_order_id="stop-entry",
                symbol="SPY", side="buy", quantity=Decimal("3"), strategy_id=77,
                reserved_cash=Decimal("0"), status="filled", source="manual_control_room")
            db.add(order)
            db.flush()
            db.add(StockPaperTrialDecision(trial_id=row.id, symbol="SPY",
                bar_timestamp=opened, decision_timestamp=opened, action="buy",
                qualifying=True, lineage=row.lineage, order_id=order.id))
            db.add(StockPaperTrialLot(trial_id=row.id, symbol="SPY",
                entry_decision_id=db.query(StockPaperTrialDecision).one().id,
                entry_order_id=order.id, quantity=Decimal("3"),
                entry_session="2025-01-02", planned_horizon_sessions=5,
                stop_fraction=Decimal("0.02")))
            db.add(StockPaperFill(account_id=1, order_id=order.id,
                broker_activity_id="stop-entry", broker_order_id="stop-broker",
                symbol="SPY", side="buy", quantity=Decimal("3"), price=Decimal("100"),
                fee=Decimal("0"), cost_known=True, filled_at=opened, raw_payload={}))
            from app.models import StockPaperPosition
            db.add(StockPaperPosition(account_id=1, symbol="SPY", quantity=Decimal("3"),
                average_entry_price=Decimal("100"), current_price=Decimal("97"),
                market_value=Decimal("291"), observed_at=opened, raw_payload={}))
            db.query(IntradayBar).filter(IntradayBar.symbol == "SPY").update({"close": Decimal("97")})
            db.flush()
            with self.enter_contexts(patches):
                observe_trial(db, row.id)
            lot = db.query(StockPaperTrialLot).one()
            self.assertEqual(lot.exit_reason, "stop")
            self.assertIsNone(lot.exit_order_id)
            self.assertEqual(db.query(StockPaperOrder).filter(StockPaperOrder.side == "sell").count(), 0)

    def test_exit_order_is_linked_and_its_fill_closes_lot(self):
        with Session(self.engine) as db:
            row = self.trial(db)
            patches, _, opened, _ = self._post_close_patches(db, row, 0.2)
            strategy = Strategy(name="linked-exit", strategy_type="test", parameters={},
                is_active=True, current_status="paper_trading_active")
            db.add(strategy); db.flush(); row.strategy_id = strategy.id
            entry = StockPaperOrder(account_id=1, client_order_id="linked-entry",
                symbol="SPY", side="buy", quantity=Decimal("1"), strategy_id=strategy.id,
                reserved_cash=Decimal("0"), status="filled", source="manual_control_room")
            db.add(entry); db.flush()
            decision = StockPaperTrialDecision(trial_id=row.id, symbol="SPY",
                bar_timestamp=opened, decision_timestamp=opened, action="buy",
                qualifying=True, lineage=row.lineage, order_id=entry.id)
            db.add(decision); db.flush()
            db.add(StockPaperTrialLot(trial_id=row.id, symbol="SPY",
                entry_decision_id=decision.id, entry_order_id=entry.id, quantity=Decimal("1"),
                entry_session="2025-01-02", planned_horizon_sessions=5,
                stop_fraction=Decimal("0.02")))
            db.add(StockPaperFill(account_id=1, order_id=entry.id,
                broker_activity_id="linked-entry-fill", broker_order_id="entry-broker",
                symbol="SPY", side="buy", quantity=Decimal("1"), price=Decimal("100"),
                fee=Decimal("1"), cost_known=True, filled_at=opened, raw_payload={}))
            from app.models import StockPaperPosition
            db.add(StockPaperPosition(account_id=1, symbol="SPY", quantity=Decimal("1"),
                average_entry_price=Decimal("100"), current_price=Decimal("97"),
                market_value=Decimal("97"), observed_at=opened, raw_payload={}))
            db.query(IntradayBar).filter(IntradayBar.symbol == "SPY").update({"close": Decimal("97")})
            db.flush()
            with self.enter_contexts(patches):
                observe_trial(db, row.id)
            next_bar = datetime(2025, 1, 3, 14, 59, tzinfo=timezone.utc)
            db.add(IntradayBar(symbol="SPY", timeframe="1m", opened_at=next_bar,
                open=Decimal("97"), high=Decimal("97"), low=Decimal("97"),
                close=Decimal("97"), volume=1, provider="alpaca",
                feed_class="sip", exchange_timestamp=next_bar))
            db.flush()
            with self.enter_contexts(patches), \
                 patch("app.services.stock_forward_trial._now",
                       return_value=datetime(2025, 1, 3, 15, tzinfo=timezone.utc)), \
                 patch("app.services.stock_forward_trial.session_bounds",
                       side_effect=lambda d: (datetime(d.year, d.month, d.day, 14, 30, tzinfo=timezone.utc),
                                              datetime(d.year, d.month, d.day, 21, tzinfo=timezone.utc))):
                execute_pending_decisions(db, row.id)
            lot = db.query(StockPaperTrialLot).one()
            self.assertIsNotNone(lot.exit_order_id)
            db.add(StockPaperFill(account_id=1, order_id=lot.exit_order_id,
                broker_activity_id="linked-exit-fill", broker_order_id="exit-broker",
                symbol="SPY", side="sell", quantity=Decimal("1"), price=Decimal("97"),
                fee=Decimal("1"), cost_known=True, filled_at=opened, raw_payload={}))
            day = lambda d: (datetime(d.year, d.month, d.day, 14, 30, tzinfo=timezone.utc),
                             datetime(d.year, d.month, d.day, 21, tzinfo=timezone.utc))
            with patch("app.services.stock_forward_trial._now",
                       return_value=datetime(2025, 1, 21, 21, tzinfo=timezone.utc)), \
                 patch("app.services.stock_forward_trial.session_bounds", side_effect=day):
                metric = evaluate_trial(db, row.id)
            self.assertEqual(metric.payload["closed_trades"], 1)

    def test_zero_and_partial_fills_do_not_claim_closed_trade_or_known_costs(self):
        with Session(self.engine) as db:
            row = self.trial(db)
            self.account(db)
            entry = StockPaperOrder(account_id=1, client_order_id="partial-entry",
                symbol="SPY", side="buy", quantity=Decimal("2"), reserved_cash=Decimal("0"),
                status="filled", source="manual_control_room")
            db.add(entry); db.flush()
            decision = StockPaperTrialDecision(trial_id=row.id, symbol="SPY",
                bar_timestamp=datetime(2025, 1, 2, 15), decision_timestamp=datetime.now(timezone.utc),
                action="buy", qualifying=True, lineage=row.lineage, order_id=entry.id)
            db.add(decision); db.flush()
            db.add(StockPaperTrialLot(trial_id=row.id, symbol="SPY",
                entry_decision_id=decision.id, entry_order_id=entry.id, quantity=Decimal("2"),
                entry_session="2025-01-02", planned_horizon_sessions=5,
                stop_fraction=Decimal("0.02")))
            db.add(StockPaperFill(account_id=1, order_id=entry.id,
                broker_activity_id="partial-entry-fill", broker_order_id="partial",
                symbol="SPY", side="buy", quantity=Decimal("1"), price=Decimal("100"),
                fee=None, cost_known=False, filled_at=datetime.now(timezone.utc), raw_payload={}))
            day = lambda d: (datetime(d.year, d.month, d.day, 14, 30, tzinfo=timezone.utc),
                             datetime(d.year, d.month, d.day, 21, tzinfo=timezone.utc))
            with patch("app.services.stock_forward_trial._now",
                       return_value=datetime(2025, 1, 21, 21, tzinfo=timezone.utc)), \
                 patch("app.services.stock_forward_trial.session_bounds", side_effect=day):
                metric = evaluate_trial(db, row.id)
            self.assertEqual(metric.payload["closed_trades"], 0)
            self.assertFalse(metric.payload["costs_known"])

    def test_metrics_classify_under_20_sessions_accumulating_and_20_at_90_insufficient(self):
        with Session(self.engine) as db:
            row = self.trial(db)
            row.started_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
            for offset in range(20):
                when = datetime(2025, 1, 1 + offset, 15)
                db.add(IntradayBar(symbol="SPY", timeframe="1m", opened_at=when,
                    open=Decimal("100"), high=Decimal("100"), low=Decimal("100"),
                    close=Decimal("100"), volume=1, provider="alpaca",
                    feed_class="sip", exchange_timestamp=when))
                if offset < 19:
                    db.add(StockPaperTrialDecision(trial_id=row.id, symbol="SPY",
                        bar_timestamp=when, decision_timestamp=when, action="reject",
                        qualifying=False, rejection_reason="test", lineage=row.lineage))
            db.flush()
            with patch("app.services.stock_forward_trial._now",
                       return_value=datetime(2025, 1, 21, 21, tzinfo=timezone.utc)), \
                 patch("app.services.stock_forward_trial.session_bounds",
                       side_effect=lambda d: (datetime(d.year, d.month, d.day, 14, 30, tzinfo=timezone.utc),
                                              datetime(d.year, d.month, d.day, 21, tzinfo=timezone.utc))):
                metric = evaluate_trial(db, row.id)
            self.assertEqual(metric.payload["observed_sessions"], 20)
            self.assertGreaterEqual(metric.payload["decision_coverage"], Decimal("0.90"))
            self.assertEqual(metric.classification, "insufficient")

    def test_drawdown_pauses_before_model_or_order(self):
        with Session(self.engine) as db:
            row = self.trial(db)
            patches, _, _, _ = self._post_close_patches(db, row, 0.8)
            account = db.query(StockPaperAccount).one()
            account.equity = Decimal("979")
            row.peak_equity = Decimal("1000")
            with self.enter_contexts(patches), \
                 patch("app.services.stock_forward_trial._now",
                       return_value=datetime(2025, 1, 2, 20, 0, tzinfo=timezone.utc)):
                result = execute_pending_decisions(db, row.id)
            self.assertEqual(result["status"], "paused")
            self.assertEqual(row.pause_reason, "drawdown_limit_2_percent")
            self.assertEqual(db.query(StockPaperOrder).count(), 0)

    def test_decision_dedupe_returns_existing_decision(self):
        with Session(self.engine) as db:
            row = self.trial(db)
            bar = datetime(2025, 1, 2, 15, 0, tzinfo=timezone.utc)
            first = record_decision(db, row.id, symbol="SPY", bar_timestamp=bar,
                                    feature_timestamp=bar.replace(hour=14), evidence=False)
            second = record_decision(db, row.id, symbol="SPY", bar_timestamp=bar,
                                     feature_timestamp=bar.replace(hour=14), evidence=False)
            self.assertEqual(first.id, second.id)
            self.assertEqual(db.query(StockPaperTrialDecision).count(), 1)

    def test_unknown_costs_keep_metrics_null(self):
        with Session(self.engine) as db:
            row = self.trial(db)
            self.account(db)
            order = StockPaperOrder(account_id=1, client_order_id="trial-order",
                broker_order_id="broker-order", symbol="SPY", side="buy",
                quantity=Decimal("1"), reserved_cash=Decimal("100"), status="filled",
                source="manual_control_room")
            db.add(order)
            db.flush()
            db.add(StockPaperTrialDecision(trial_id=row.id, symbol="SPY",
                bar_timestamp=datetime(2025, 1, 2, 15, 0), decision_timestamp=datetime.now(timezone.utc),
                action="buy", qualifying=True, lineage=row.lineage, order_id=order.id))
            db.flush()
            db.add(StockPaperFill(account_id=1, order_id=order.id,
                broker_activity_id="fill-buy", broker_order_id="broker-order",
                symbol="SPY", side="buy", quantity=Decimal("1"), price=Decimal("10"),
                fee=None, cost_known=False, filled_at=datetime.now(timezone.utc), raw_payload={}))
            db.flush()
            metric = evaluate_trial(db, row.id)
            self.assertFalse(metric.payload["costs_known"])
            self.assertIsNone(metric.payload["net_pnl"])
            self.assertIsNone(metric.payload["expectancy"])

    def test_attribution_excludes_external_fills(self):
        with Session(self.engine) as db:
            row = self.trial(db)
            self.account(db)
            order = StockPaperOrder(account_id=1, client_order_id="trial-order",
                broker_order_id="trial-broker", symbol="SPY", side="buy",
                quantity=Decimal("1"), reserved_cash=Decimal("100"), status="filled",
                source="manual_control_room")
            db.add(order)
            db.flush()
            db.add_all([
                StockPaperFill(account_id=1, order_id=order.id, broker_activity_id="b1",
                    broker_order_id="trial-broker", symbol="SPY", side="buy",
                    quantity=Decimal("1"), price=Decimal("10"), fee=Decimal("0"),
                    cost_known=True, filled_at=datetime.now(timezone.utc), raw_payload={}),
                StockPaperFill(account_id=1, order_id=None, broker_activity_id="external",
                    broker_order_id="external-broker", symbol="SPY", side="sell",
                    quantity=Decimal("1"), price=Decimal("20"), fee=Decimal("0"),
                    cost_known=True, filled_at=datetime.now(timezone.utc), raw_payload={}),
            ])
            db.flush()
            metric = evaluate_trial(db, row.id)
            self.assertEqual(metric.payload["closed_trades"], 0)
            self.assertIsNone(metric.payload["gross_pnl"])

    def test_public_api_has_no_post_decisions_and_roles_are_classified(self):
        methods = {(route.path, method) for route in router.routes for method in route.methods}
        self.assertNotIn(("/stock/forward-trials/{trial_id}/decisions", "POST"), methods)
        trial_path = "/stock/forward-trials/00000000-0000-0000-0000-000000000001"
        self.assertEqual(required_role("GET", trial_path + "/decisions"), "viewer")
        self.assertEqual(required_role("POST", "/stock/forward-trials/00000000-0000-0000-0000-000000000001/start"), "operator")
        self.assertEqual(required_role("POST", "/stock/forward-trials"), "admin")

    def test_allocation_counts_filled_open_lot_and_unfilled_remainder_once(self):
        with Session(self.engine) as db:
            row = self.trial(db)
            self.account(db)
            strategy = Strategy(name="allocation-helper", strategy_type="test",
                parameters={}, is_active=True, current_status="paper_trading_active")
            db.add(strategy); db.flush()
            row.strategy_id = strategy.id
            decision = StockPaperTrialDecision(trial_id=row.id, symbol="SPY",
                bar_timestamp=datetime(2025, 1, 2, 15), decision_timestamp=datetime.now(timezone.utc),
                action="buy", qualifying=True, lineage=row.lineage)
            db.add(decision); db.flush()
            filled = StockPaperOrder(account_id=1, client_order_id="filled-open",
                symbol="SPY", side="buy", quantity=Decimal("2"), limit_price=Decimal("100"),
                reserved_cash=Decimal("200"), status="filled", strategy_id=strategy.id,
                source="manual_control_room")
            db.add(filled); db.flush()
            db.add(StockPaperTrialLot(trial_id=row.id, symbol="SPY",
                entry_decision_id=decision.id, entry_order_id=filled.id, quantity=Decimal("2"),
                entry_session="2025-01-02", planned_horizon_sessions=5,
                stop_fraction=Decimal("0.02")))
            db.add(StockPaperFill(account_id=1, order_id=filled.id, broker_activity_id="alloc-fill",
                broker_order_id="alloc-broker", symbol="SPY", side="buy", quantity=Decimal("2"),
                price=Decimal("100"), fee=Decimal("0"), cost_known=True,
                filled_at=datetime.now(timezone.utc), raw_payload={}))
            pending = StockPaperOrder(account_id=1, client_order_id="pending-open",
                symbol="QQQ", side="buy", quantity=Decimal("3"), limit_price=Decimal("50"),
                reserved_cash=Decimal("150"), status="reserved", strategy_id=strategy.id,
                source="manual_control_room")
            cancelled = StockPaperOrder(account_id=1, client_order_id="cancelled-open",
                symbol="IWM", side="buy", quantity=Decimal("100"), limit_price=Decimal("99"),
                reserved_cash=Decimal("9900"), status="cancelled", strategy_id=strategy.id,
                source="manual_control_room")
            db.add_all([pending, cancelled]); db.flush()
            db.add(StockPaperFill(account_id=1, order_id=pending.id,
                broker_activity_id="pending-partial", broker_order_id="pending-broker",
                symbol="QQQ", side="buy", quantity=Decimal("1"), price=Decimal("50"),
                fee=None, cost_known=False, filled_at=datetime.now(timezone.utc), raw_payload={}))
            db.flush()
            self.assertEqual(_trial_allocated_notional(db, row), Decimal("300"))

    def test_partial_lot_entry_counts_filled_and_working_remainder_once(self):
        with Session(self.engine) as db:
            row = self.trial(db)
            self.account(db)
            strategy = Strategy(name="partial-allocation-helper", strategy_type="test",
                parameters={}, is_active=True, current_status="paper_trading_active")
            db.add(strategy); db.flush(); row.strategy_id = strategy.id
            decision = StockPaperTrialDecision(trial_id=row.id, symbol="SPY",
                bar_timestamp=datetime(2025, 1, 2, 15), decision_timestamp=datetime.now(timezone.utc),
                action="buy", qualifying=True, lineage=row.lineage)
            db.add(decision); db.flush()
            order = StockPaperOrder(account_id=1, client_order_id="partial-lot-working",
                symbol="SPY", side="buy", quantity=Decimal("3"), limit_price=Decimal("100"),
                reserved_cash=Decimal("300"), status="partially_filled",
                strategy_id=strategy.id, source="manual_control_room")
            db.add(order); db.flush()
            db.add(StockPaperTrialLot(trial_id=row.id, symbol="SPY",
                entry_decision_id=decision.id, entry_order_id=order.id, quantity=Decimal("2"),
                entry_session="2025-01-02", planned_horizon_sessions=5,
                stop_fraction=Decimal("0.02")))
            db.add(StockPaperFill(account_id=1, order_id=order.id,
                broker_activity_id="partial-lot-fill", broker_order_id="partial-lot-broker",
                symbol="SPY", side="buy", quantity=Decimal("2"), price=Decimal("100"),
                fee=None, cost_known=False, filled_at=datetime.now(timezone.utc), raw_payload={}))
            db.flush()
            # Two filled shares are represented by the lot; one working share is
            # represented by the still-partially-filled reservation.
            self.assertEqual(_trial_allocated_notional(db, row), Decimal("300"))

    def test_pending_qualifying_entry_remains_under_ten_thousand_allocation(self):
        with Session(self.engine) as db:
            row = self.trial(db)
            patches, _, _, _ = self._post_close_patches(db, row, 0.8)
            with self.enter_contexts(patches):
                observe_trial(db, row.id)
            strategy = Strategy(name="allocation-pending", strategy_type="test",
                parameters={}, is_active=True, current_status="paper_trading_active")
            db.add(strategy); db.flush(); row.strategy_id = strategy.id
            reference = datetime(2025, 1, 3, 14, 59, tzinfo=timezone.utc)
            db.add(IntradayBar(symbol="SPY", timeframe="1m", opened_at=reference,
                open=Decimal("100"), high=Decimal("100"), low=Decimal("100"),
                close=Decimal("100"), volume=1, provider="alpaca",
                feed_class="sip", exchange_timestamp=reference))
            db.flush()
            with self.enter_contexts(patches), \
                 patch("app.services.stock_forward_trial._now",
                       return_value=datetime(2025, 1, 3, 15, tzinfo=timezone.utc)), \
                 patch("app.services.stock_forward_trial.session_bounds",
                       side_effect=lambda d: (datetime(d.year, d.month, d.day, 14, 30, tzinfo=timezone.utc),
                                              datetime(d.year, d.month, d.day, 21, tzinfo=timezone.utc))):
                result = execute_pending_decisions(db, row.id)
            self.assertEqual(result["executed"], 1)
            order = db.query(StockPaperOrder).filter(StockPaperOrder.side == "buy").one()
            self.assertLessEqual(order.reserved_cash, Decimal("10000"))

    def test_paused_trial_creates_exit_intent_but_no_new_decision(self):
        with Session(self.engine) as db:
            row = self.trial(db, status="paused")
            patches, _, opened, _ = self._post_close_patches(db, row, 0.8)
            strategy = Strategy(name="paused-exit", strategy_type="test", parameters={},
                is_active=True, current_status="paper_trading_active")
            db.add(strategy); db.flush(); row.strategy_id = strategy.id
            entry = StockPaperOrder(account_id=1, client_order_id="paused-entry",
                symbol="SPY", side="buy", quantity=Decimal("1"), strategy_id=strategy.id,
                reserved_cash=Decimal("0"), status="filled", source="manual_control_room")
            db.add(entry); db.flush()
            decision = StockPaperTrialDecision(trial_id=row.id, symbol="SPY",
                bar_timestamp=opened, decision_timestamp=opened, action="buy",
                qualifying=True, lineage=row.lineage, order_id=entry.id)
            db.add(decision); db.flush()
            db.add(StockPaperTrialLot(trial_id=row.id, symbol="SPY",
                entry_decision_id=decision.id, entry_order_id=entry.id, quantity=Decimal("1"),
                entry_session="2025-01-02", planned_horizon_sessions=5,
                stop_fraction=Decimal("0.02")))
            db.add(StockPaperFill(account_id=1, order_id=entry.id,
                broker_activity_id="paused-entry-fill", broker_order_id="paused-entry-broker",
                symbol="SPY", side="buy", quantity=Decimal("1"), price=Decimal("100"),
                fee=Decimal("0"), cost_known=True, filled_at=opened, raw_payload={}))
            from app.models import StockPaperPosition
            db.add(StockPaperPosition(account_id=1, symbol="SPY", quantity=Decimal("1"),
                average_entry_price=Decimal("100"), current_price=Decimal("97"),
                market_value=Decimal("97"), observed_at=opened, raw_payload={}))
            db.query(IntradayBar).filter(IntradayBar.symbol == "SPY").update({"close": Decimal("97")})
            db.flush()
            with self.enter_contexts(patches):
                result = observe_trial(db, row.id)
            lot = db.query(StockPaperTrialLot).one()
            self.assertEqual(result["status"], "paused")
            self.assertEqual(lot.exit_reason, "stop")
            self.assertEqual(db.query(StockPaperTrialDecision).count(), 1)
            self.assertEqual(db.query(StockPaperOrder).filter(StockPaperOrder.side == "sell").count(), 0)

    def test_baseline_and_peak_are_initialized_once_and_drawdown_uses_peak(self):
        with Session(self.engine) as db:
            row = self.trial(db, status="approved")
            self.account(db)
            row.peak_equity = Decimal("1000")
            with patch("app.services.stock_forward_trial.validate_trial_artifact", return_value={}), \
                 patch("app.services.stock_forward_trial.session_bounds", return_value=None):
                start_trial(db, row.id)
            account = db.query(StockPaperAccount).one()
            self.assertEqual(row.baseline_equity, Decimal("1000"))
            self.assertEqual(row.peak_equity, Decimal("1000"))
            account.raw_payload = {"provider_replaced": True}
            account.equity = Decimal("1200")
            with patch("app.services.stock_forward_trial._now",
                       return_value=datetime(2025, 1, 2, 20, 0, tzinfo=timezone.utc)), \
                 patch("app.services.stock_forward_trial.session_bounds", return_value=(
                     datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc),
                     datetime(2025, 1, 2, 21, tzinfo=timezone.utc))):
                execute_pending_decisions(db, row.id)
            self.assertEqual(row.baseline_equity, Decimal("1000"))
            self.assertEqual(row.peak_equity, Decimal("1200"))
            account.raw_payload = {"provider_replaced_again": True}
            account.equity = Decimal("1175")
            with patch("app.services.stock_forward_trial._now",
                       return_value=datetime(2025, 1, 2, 20, 0, tzinfo=timezone.utc)), \
                 patch("app.services.stock_forward_trial.session_bounds", return_value=(
                     datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc),
                     datetime(2025, 1, 2, 21, tzinfo=timezone.utc))):
                execute_pending_decisions(db, row.id)
            self.assertEqual(row.status, "paused")
            self.assertEqual(row.pause_reason, "drawdown_limit_2_percent")

    def test_direct_trial_curve_uses_durable_baseline_and_verified_session_marks(self):
        with Session(self.engine) as db:
            row = self.trial(db)
            row.baseline_equity = Decimal("100000")
            row.baseline_at = datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc)
            row.policy = {**row.policy, "regular_sessions": 2}
            self.account(db)
            entry = StockPaperOrder(account_id=1, client_order_id="curve-entry",
                symbol="SPY", side="buy", quantity=Decimal("10"),
                reserved_cash=Decimal("1000"), status="filled", source="manual_control_room")
            db.add(entry); db.flush()
            decision = StockPaperTrialDecision(trial_id=row.id, symbol="SPY",
                bar_timestamp=datetime(2025, 1, 2, 14, 30), decision_timestamp=datetime.now(timezone.utc),
                action="buy", qualifying=True, lineage=row.lineage, order_id=entry.id)
            db.add(decision); db.flush()
            db.add(StockPaperTrialLot(trial_id=row.id, symbol="SPY",
                entry_decision_id=decision.id, entry_order_id=entry.id, quantity=Decimal("10"),
                entry_session="2025-01-02", planned_horizon_sessions=5,
                stop_fraction=Decimal("0.02")))
            db.add(StockPaperFill(account_id=1, order_id=entry.id,
                broker_activity_id="curve-fill", broker_order_id="curve-broker",
                symbol="SPY", side="buy", quantity=Decimal("10"), price=Decimal("100"),
                fee=Decimal("0"), cost_known=True,
                filled_at=datetime(2025, 1, 2, 15), raw_payload={}))
            for day, close in ((2, Decimal("100")), (3, Decimal("98"))):
                opened = datetime(2025, 1, day, 20, 59)
                db.add(IntradayBar(symbol="SPY", timeframe="1m", opened_at=opened,
                    open=close, high=close, low=close, close=close, volume=1,
                    provider="alpaca", feed_class="sip", exchange_timestamp=opened))
            db.flush()
            with patch("app.services.stock_forward_trial.session_bounds",
                       side_effect=lambda d: (datetime(d.year, d.month, d.day, 14, 30, tzinfo=timezone.utc),
                                              datetime(d.year, d.month, d.day, 21, tzinfo=timezone.utc))):
                drawdown = _trial_equity_curve_max_drawdown(
                    db, row, datetime(2025, 1, 3, 21, tzinfo=timezone.utc))
            self.assertEqual(drawdown, Decimal("0.0002"))

    def test_trial_curve_missing_open_position_mark_is_null_and_external_data_excluded(self):
        with Session(self.engine) as db:
            row = self.trial(db)
            row.baseline_equity = Decimal("100000")
            row.baseline_at = datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc)
            row.policy = {**row.policy, "regular_sessions": 2}
            self.account(db)
            with patch("app.services.stock_forward_trial.session_bounds",
                       side_effect=lambda d: (datetime(d.year, d.month, d.day, 14, 30, tzinfo=timezone.utc),
                                              datetime(d.year, d.month, d.day, 21, tzinfo=timezone.utc))):
                self.assertIsNone(_trial_equity_curve_max_drawdown(
                    db, row, datetime(2025, 1, 3, 21, tzinfo=timezone.utc)))

    def test_observe_job_processes_paused_stopped_blocked(self):
        from app.tasks import jobs
        with Session(self.engine) as db:
            ids = []
            for index, status in enumerate(("paused", "stopped", "blocked"), 1):
                row = self.trial(db, status=status,
                    trial_id=f"00000000-0000-0000-0000-00000000000{index}")
                ids.append(row.id)
            db.commit()
        seen = []
        metric = MagicMock(classification="insufficient")
        def run_job(_name, work):
            with Session(self.engine) as db:
                return work(db)
        with patch.object(jobs, "_run_job", side_effect=run_job), \
             patch.object(jobs, "observe_trial", side_effect=lambda db, trial_id: seen.append(("observe", trial_id))), \
             patch.object(jobs, "execute_pending_decisions", side_effect=lambda db, trial_id: seen.append(("execute", trial_id))), \
             patch.object(jobs, "evaluate_trial", return_value=metric):
            result = stock_forward_trial_observe_job.run()
        self.assertEqual({item[1] for item in seen}, set(ids))
        self.assertEqual(len(seen), 6)
        self.assertEqual(len(result["trials"]), 3)

    def test_session_twenty_stops_and_rejects_pending_entries(self):
        with Session(self.engine) as db:
            row = self.trial(db, status="running")
            row.started_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
            row.policy = {**row.policy, "regular_sessions": 1}
            decision = StockPaperTrialDecision(trial_id=row.id, symbol="SPY",
                bar_timestamp=datetime(2025, 1, 2), decision_timestamp=datetime(2025, 1, 2),
                action="buy", qualifying=True, lineage=row.lineage)
            db.add(decision)
            db.flush()
            with patch("app.services.stock_forward_trial._now",
                       return_value=datetime(2025, 1, 3, 21, 1, tzinfo=timezone.utc)), \
                 patch("app.services.stock_forward_trial.session_bounds",
                       return_value=(datetime(2025, 1, 3, 14, 30, tzinfo=timezone.utc),
                                     datetime(2025, 1, 3, 21, tzinfo=timezone.utc))):
                evaluate_trial(db, row.id)
            self.assertIn(row.status, {"stopped", "completed"})
            self.assertEqual(decision.action, "reject")
            self.assertEqual(decision.rejection_reason, "trial_window_complete")

    def test_stopped_trial_dispatches_exit_then_completes(self):
        with Session(self.engine) as db:
            row = self.trial(db, status="stopped")
            row.policy = {**row.policy, "regular_sessions": 1}
            self.account(db)
            order = StockPaperOrder(account_id=1, client_order_id="entry-stop",
                symbol="SPY", side="buy", quantity=Decimal("1"), status="filled",
                source="manual_control_room")
            db.add(order); db.flush()
            decision = StockPaperTrialDecision(trial_id=row.id, symbol="SPY",
                bar_timestamp=datetime(2025, 1, 2), decision_timestamp=datetime(2025, 1, 2),
                action="buy", qualifying=True, lineage=row.lineage, order_id=order.id)
            db.add(decision); db.flush()
            lot = StockPaperTrialLot(trial_id=row.id, symbol="SPY",
                entry_decision_id=decision.id, entry_order_id=order.id, quantity=Decimal("1"),
                entry_session="2025-01-02", planned_horizon_sessions=5)
            db.add(lot)
            db.add(StockPaperFill(account_id=1, order_id=order.id,
                broker_activity_id="stop-entry-fill", broker_order_id="stop-entry-broker",
                symbol="SPY", side="buy", quantity=Decimal("1"), price=Decimal("100"),
                fee=Decimal("0"), cost_known=True,
                filled_at=datetime(2025, 1, 2, 15), raw_payload={}))
            db.flush()
            with patch("app.services.stock_forward_trial._now",
                       return_value=datetime(2025, 1, 3, 15, tzinfo=timezone.utc)), \
                 patch("app.services.stock_forward_trial.session_bounds",
                       return_value=(datetime(2025, 1, 3, 14, 30, tzinfo=timezone.utc),
                                     datetime(2025, 1, 3, 21, tzinfo=timezone.utc))):
                execute_pending_decisions(db, row.id)
            self.assertIn(row.status, {"stopped", "completed"})
            self.assertEqual(row.status, "stopped")

    def test_partial_exit_does_not_complete(self):
        with Session(self.engine) as db:
            row = self.trial(db, status="stopped")
            order = StockPaperOrder(account_id=1, client_order_id="partial-entry",
                symbol="SPY", side="buy", quantity=Decimal("2"), status="filled",
                source="manual_control_room")
            db.add(order); db.flush()
            decision = StockPaperTrialDecision(trial_id=row.id, symbol="SPY",
                bar_timestamp=datetime(2025, 1, 2), decision_timestamp=datetime(2025, 1, 2),
                action="buy", qualifying=True, lineage=row.lineage, order_id=order.id)
            db.add(decision); db.flush()
            lot = StockPaperTrialLot(trial_id=row.id, symbol="SPY",
                entry_decision_id=decision.id, entry_order_id=order.id, quantity=Decimal("2"),
                entry_session="2025-01-02", planned_horizon_sessions=5)
            db.add(lot)
            db.add(StockPaperFill(account_id=1, order_id=order.id,
                broker_activity_id="partial-entry-fill", broker_order_id="partial-entry-broker",
                symbol="SPY", side="buy", quantity=Decimal("2"), price=Decimal("100"),
                fee=Decimal("0"), cost_known=True, filled_at=datetime(2025, 1, 2, 15), raw_payload={}))
            db.flush()
            lot.exit_status = "filled"
            lot.exited_quantity = Decimal("1")
            db.flush()
            stop_trial(db, row.id)
            self.assertEqual(row.status, "stopped")
            self.assertNotEqual(row.status, "completed")


if __name__ == "__main__":
    unittest.main()