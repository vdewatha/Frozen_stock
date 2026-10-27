"""Learning-loop feedback: mistake attribution, edge-decay demotion, and gates."""
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401 - register mappers
from app.core.config import settings
from app.db.base import Base
from app.models import (
    AuditLog, CandidateDecisionJournal, IntradayBar, StockPaperTrial, StockPaperTrialDecision,
    Strategy, StrategyMemory,
)
from app.models.stock_paper import (
    StockPaperAccount, StockPaperFill, StockPaperOrder, StockPaperStrategyEvidence, StockPaperTrialLot,
)
from app.services import decision_journal
from app.services.governance import aggregate_strategy_memory, evaluate_single_strategy
from app.services.reactivation import reactivation_queue, review_strategy_reactivation
from app.services.stock_forward_trial import POLICY, _evidence_allows_trade, _lot_outcomes
from app.services.strategy_edge_decay import (
    assess_edge_decay, edge_decay_projection, lot_returns, run_edge_decay_review, strategy_is_suspended,
)
from app.services.trade_mistakes import aggregate_mistakes, classify_trade, trial_mistake_report


NOW = datetime(2025, 3, 3, 15, tzinfo=timezone.utc)


class MistakeClassifierTests(unittest.TestCase):
    def base(self, **kw):
        return {"entry_price": 100.0, "exit_price": 99.0, "quantity": 10, "fees": 0.0, "stop_fraction": 0.02} | kw

    def test_win_has_no_mistakes(self):
        result = classify_trade(self.base(exit_price=103.0))
        self.assertEqual(result["status"], "win")
        self.assertEqual(result["mistakes"], [])
        self.assertIsNone(result["primary_mistake"])

    def test_cost_dominated_when_gross_win_is_net_loss(self):
        result = classify_trade(self.base(exit_price=100.5, fees=6.0))
        self.assertEqual(result["status"], "loss")
        self.assertIn("cost_dominated", result["mistakes"])
        self.assertEqual(result["primary_mistake"], "cost_dominated")

    def test_unknown_costs_are_not_treated_as_zero_cost_net(self):
        result = classify_trade(self.base(exit_price=101.0, fees=None))
        self.assertFalse(result["costs_known"])
        self.assertIsNone(result["net_pnl"])

    def test_oversized_loss_exceeds_risk_budget(self):
        result = classify_trade(self.base(exit_price=95.0, quantity=100, equity_at_entry=10000, risk_per_trade=0.0025))
        self.assertEqual(result["primary_mistake"], "oversized")

    def test_bad_entry_never_moved_in_favor(self):
        result = classify_trade(self.base(exit_price=98.0, max_favorable_excursion=0.001, exit_reason="stop"))
        self.assertIn("bad_entry", result["mistakes"])

    def test_stop_slippage_and_premature_stop(self):
        result = classify_trade(self.base(exit_price=96.0, exit_reason="stop", max_favorable_excursion=0.01,
                                          post_exit_return=0.03))
        self.assertIn("stop_slippage", result["mistakes"])
        self.assertIn("premature_stop", result["mistakes"])

    def test_gave_back_gains_and_signal_decay(self):
        gave_back = classify_trade(self.base(exit_price=99.5, max_favorable_excursion=0.05, exit_reason="horizon"))
        self.assertIn("gave_back_gains", gave_back["mistakes"])
        decay = classify_trade(self.base(exit_price=99.5, max_favorable_excursion=0.01, exit_reason="horizon"))
        self.assertEqual(decay["primary_mistake"], "signal_decay")

    def test_regime_mismatch_and_overconfidence(self):
        result = classify_trade(self.base(entry_regime="bear", validated_regimes=["bull"], probability=0.7))
        self.assertEqual(result["primary_mistake"], "regime_mismatch")
        self.assertIn("overconfident_signal", result["mistakes"])

    def test_unclassifiable_without_prices(self):
        self.assertEqual(classify_trade({"entry_price": None})["status"], "unclassifiable")

    def test_aggregate_lessons_are_proposals_requiring_fresh_validation(self):
        rows = [
            {"strategy_id": 7, "classification": classify_trade(self.base(exit_price=100.5, fees=6.0))},
            {"strategy_id": 7, "classification": classify_trade(self.base(exit_price=100.4, fees=6.0))},
            {"strategy_id": 7, "classification": classify_trade(self.base(exit_price=104.0))},
        ]
        summary = aggregate_mistakes(rows)
        strategy = summary["strategies"][0]
        self.assertEqual((strategy["closed_trades"], strategy["wins"], strategy["losses"]), (3, 1, 2))
        self.assertEqual(strategy["top_mistake"], "cost_dominated")
        self.assertEqual(strategy["primary_mistake_share_of_losses"], {"cost_dominated": 1.0})
        self.assertTrue(all(not lesson["auto_apply"] for lesson in strategy["lessons"]))
        self.assertTrue(all(lesson["requires_fresh_holdout_and_forward_trial"] for lesson in strategy["lessons"]))
        self.assertFalse(summary["mutates_strategy_state"])


class EdgeDecayAssessmentTests(unittest.TestCase):
    def test_insufficient_samples(self):
        self.assertEqual(assess_edge_decay([-0.01] * 5)["status"], "insufficient")

    def test_confident_losses_are_decayed(self):
        result = assess_edge_decay([-0.01, -0.012, -0.009, -0.011, -0.01, -0.013, -0.008, -0.01])
        self.assertEqual(result["status"], "decayed")
        self.assertLess(result["upper_confidence_bound"], 0)

    def test_noisy_negative_mean_is_only_decaying(self):
        result = assess_edge_decay([0.03, -0.04, 0.02, -0.03, 0.01, -0.02, 0.025, -0.03])
        self.assertEqual(result["status"], "decaying")

    def test_rolling_window_ignores_old_history(self):
        old_good = [0.05] * 30
        recent_bad = [-0.01, -0.012, -0.009, -0.011, -0.01, -0.013, -0.008, -0.01, -0.011, -0.012] * 2
        self.assertEqual(assess_edge_decay(old_good + recent_bad)["status"], "decayed")
        self.assertEqual(assess_edge_decay(recent_bad + old_good)["status"], "healthy")

    def test_lot_returns_prefer_net(self):
        outcomes = [{"close_at": NOW, "entry_cost": Decimal("100"), "outcome": Decimal("1"), "net_outcome": Decimal("-0.5")}]
        self.assertEqual(lot_returns(outcomes), [-0.005])


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.broker = patch.object(settings, "active_paper_broker", "alpaca_paper")
        self.broker.start()
        self.tmp = TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + str(Path(self.tmp.name) / "loop.sqlite"))
        Base.metadata.create_all(self.engine)

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()
        self.broker.stop()

    def account(self, db):
        db.add(StockPaperAccount(
            broker="alpaca_paper", broker_account_id="acct", currency="USD",
            cash=Decimal("1000"), buying_power=Decimal("1000"), equity=Decimal("1000"),
            last_equity=Decimal("1000"), status="reconciled", costs_known=True, accounting_verified=True,
            reconciliation_required=False, unexplained_residual=False, raw_payload={},
        ))
        db.flush()

    def trial(self, db, *, with_strategy=True, evidence_status="approved"):
        trial = StockPaperTrial(id="00000000-0000-0000-0000-0000000000aa", binding_id=1, actor="test",
                                status="running", policy=dict(POLICY), lineage={"universe": ["SPY"]},
                                baseline_equity=Decimal("1000"), peak_equity=Decimal("1000"))
        db.add(trial)
        db.flush()
        if with_strategy:
            strategy = Strategy(name="forward_trial:x", strategy_type="stock_forward_trial", parameters={},
                                is_active=True, current_status="paper_trading_active")
            db.add(strategy)
            db.flush()
            trial.strategy_id = strategy.id
            if evidence_status:
                db.add(StockPaperStrategyEvidence(
                    strategy_id=strategy.id, evidence_id="trial-x", status=evidence_status,
                    verified_drawdown=Decimal("0"), consecutive_losses=0, observed_at=NOW,
                    expires_at=NOW + timedelta(days=1), provenance={"trial_id": trial.id}))
            db.flush()
        return trial

    def closed_lot(self, db, trial, n, *, entry="100", exit_="99", fee="0", exit_reason="stop"):
        when = NOW - timedelta(days=30 - n)
        decision = StockPaperTrialDecision(trial_id=trial.id, symbol="SPY", bar_timestamp=when,
                                           decision_timestamp=when, action="buy", qualifying=True,
                                           lineage={"probability": 0.55})
        db.add(decision)
        db.flush()
        buy = StockPaperOrder(account_id=1, client_order_id=f"b{n}", symbol="SPY", side="buy",
                              quantity=Decimal("1"), status="filled", source="test", strategy_id=trial.strategy_id)
        db.add(buy)
        db.flush()
        lot = StockPaperTrialLot(trial_id=trial.id, symbol="SPY", entry_decision_id=decision.id,
                                 entry_order_id=buy.id, quantity=Decimal("1"), entry_session=when.date().isoformat(),
                                 exit_reason=exit_reason)
        db.add(lot)
        db.flush()
        sell = StockPaperOrder(account_id=1, client_order_id=f"s{n}", symbol="SPY", side="sell",
                               quantity=Decimal("1"), status="filled", source="test", trial_lot_id=lot.id)
        db.add(sell)
        db.flush()
        db.add_all([
            StockPaperFill(account_id=1, order_id=buy.id, broker_activity_id=f"fb{n}", symbol="SPY", side="buy",
                           quantity=Decimal("1"), price=Decimal(entry), fee=Decimal(fee), cost_known=True,
                           filled_at=when, raw_payload={}),
            StockPaperFill(account_id=1, order_id=sell.id, broker_activity_id=f"fs{n}", symbol="SPY", side="sell",
                           quantity=Decimal("1"), price=Decimal(exit_), fee=Decimal(fee), cost_known=True,
                           filled_at=when + timedelta(hours=2), raw_payload={}),
        ])
        db.flush()
        return lot


class ForwardTrialDecayTests(_DbCase):
    def test_lot_outcomes_expose_cost_basis_and_lot_link(self):
        with Session(self.engine) as db:
            self.account(db)
            trial = self.trial(db)
            lot = self.closed_lot(db, trial, 1)
            outcome = _lot_outcomes(db, [lot])[0]
            self.assertEqual(outcome["lot_id"], lot.id)
            self.assertEqual(outcome["entry_cost"], Decimal("100"))

    def test_missing_evidence_for_existing_strategy_blocks_instead_of_crashing(self):
        with Session(self.engine) as db:
            self.account(db)
            trial = self.trial(db, evidence_status=None)
            self.assertEqual(_evidence_allows_trade(db, trial, {}, NOW), (False, "missing_strategy_evidence"))

    def test_confident_forward_decay_pauses_strategy_and_suspends_evidence(self):
        with Session(self.engine) as db:
            self.account(db)
            trial = self.trial(db)
            for n, exit_ in enumerate(["98.9", "99.0", "98.8", "99.1", "98.9", "99.0", "98.7", "99.0"]):
                self.closed_lot(db, trial, n, exit_=exit_)
            allowed, reason = _evidence_allows_trade(db, trial, {}, NOW)
            self.assertEqual((allowed, reason), (False, "edge_decay_detected"))
            strategy = db.get(Strategy, trial.strategy_id)
            evidence = db.query(StockPaperStrategyEvidence).one()
            self.assertEqual(strategy.current_status, "paused")
            self.assertEqual(evidence.status, "suspended")
            self.assertEqual(db.query(AuditLog).filter_by(event_type="strategy_edge_decay").count(), 1)
            # One-way: later pre-trade checks never re-approve the evidence.
            self.assertEqual(_evidence_allows_trade(db, trial, {}, NOW + timedelta(days=1)), (False, "strategy_paused"))
            self.assertEqual(evidence.status, "suspended")

    def test_healthy_forward_edge_still_allowed(self):
        with Session(self.engine) as db:
            self.account(db)
            trial = self.trial(db)
            for n in range(8):
                self.closed_lot(db, trial, n, exit_="101")
            self.assertEqual(_evidence_allows_trade(db, trial, {}, NOW), (True, None))
            self.assertIsNone(strategy_is_suspended(db, trial.strategy_id))

    def test_consecutive_losses_count_net_losses(self):
        with Session(self.engine) as db:
            self.account(db)
            trial = self.trial(db)
            self.closed_lot(db, trial, 1, exit_="100.10", fee="0.5")
            self.assertEqual(_evidence_allows_trade(db, trial, {}, NOW), (True, None))
            self.assertEqual(db.query(StockPaperStrategyEvidence).one().consecutive_losses, 1)

    def test_scheduled_review_pauses_only_decayed_and_projection_is_read_only(self):
        with Session(self.engine) as db:
            self.account(db)
            trial = self.trial(db)
            for n in range(8):
                self.closed_lot(db, trial, n, exit_="99")
            projection = edge_decay_projection(db)
            self.assertEqual(projection["strategies"][0]["assessment"]["status"], "decayed")
            self.assertEqual(db.get(Strategy, trial.strategy_id).current_status, "paper_trading_active")
            result = run_edge_decay_review(db, now=NOW)
            self.assertEqual(result["paused"], [trial.strategy_id])
            self.assertFalse(result["live_authorized"])
            again = run_edge_decay_review(db, now=NOW)
            self.assertEqual(again["paused"], [])

    def test_trial_mistake_report_links_each_lot_to_its_decision(self):
        with Session(self.engine) as db:
            self.account(db)
            trial = self.trial(db)
            lot = self.closed_lot(db, trial, 1, entry="100", exit_="100.2", fee="0.3")
            entered = NOW - timedelta(days=29)
            db.add(IntradayBar(symbol="SPY", timeframe="1m", opened_at=entered + timedelta(minutes=1),
                               open=Decimal("100"), high=Decimal("100.3"), low=Decimal("99.9"), close=Decimal("100.1"),
                               volume=1, provider="alpaca", feed_class="iex", exchange_timestamp=entered))
            db.flush()
            report = trial_mistake_report(db, trial.id)
            trade = report["trades"][0]
            self.assertEqual(trade["lot_id"], lot.id)
            self.assertEqual(trade["entry_decision_id"], lot.entry_decision_id)
            self.assertAlmostEqual(trade["max_favorable_excursion"], 0.003, places=6)
            self.assertEqual(trade["classification"]["primary_mistake"], "cost_dominated")
            self.assertEqual(report["summary"]["strategies"][0]["top_mistake"], "cost_dominated")


class AutomaticPromotionEdgeGateTests(_DbCase):
    def test_suspended_trial_strategy_fails_promotion_gate(self):
        from app.services.stock_learning_cycle import _automatic_strategy_edge_gate
        with Session(self.engine) as db:
            self.account(db)
            trial = self.trial(db)
            self.assertEqual(_automatic_strategy_edge_gate(db, trial)["status"], "pass")
            db.query(StockPaperStrategyEvidence).one().status = "suspended"
            db.flush()
            gate = _automatic_strategy_edge_gate(db, trial)
            self.assertEqual(gate["status"], "fail")
            self.assertEqual(gate["evidence"]["suspended"], "strategy_evidence_suspended")


class GovernanceTighteningTests(_DbCase):
    def strategy(self, db, status):
        row = Strategy(name=f"s-{status}", strategy_type=f"type-{status}", parameters={}, is_active=True,
                       current_status=status)
        db.add(row)
        db.flush()
        return row

    def memory(self, db, strategy, *, symbol, samples, win, pf, dd, conf, updated):
        db.add(StrategyMemory(strategy_id=strategy.id, symbol=symbol, market_regime="bull", sample_size=samples,
                              win_rate=Decimal(str(win)), profit_factor=Decimal(str(pf)),
                              avg_drawdown=Decimal(str(dd)), confidence_score=Decimal(str(conf)),
                              last_updated=updated))
        db.flush()

    def test_governance_never_activates_paused_strategy(self):
        with Session(self.engine) as db:
            strategy = self.strategy(db, "paused")
            self.memory(db, strategy, symbol="SPY", samples=30, win=0.6, pf=1.5, dd=-0.02, conf=0.7,
                        updated=datetime(2025, 1, 1))
            result = evaluate_single_strategy(db, strategy)
            self.assertEqual(result["decision"], "promotion_review_required")
            self.assertEqual(strategy.current_status, "paused")

    def test_governance_never_unretires(self):
        with Session(self.engine) as db:
            strategy = self.strategy(db, "retired")
            self.memory(db, strategy, symbol="SPY", samples=6, win=0.5, pf=1.2, dd=-0.15, conf=0.5,
                        updated=datetime(2025, 1, 1))
            result = evaluate_single_strategy(db, strategy)
            self.assertEqual(result["new_status"], "retired")

    def test_governance_judges_all_memory_slices_not_latest_only(self):
        with Session(self.engine) as db:
            strategy = self.strategy(db, "paper_trading_active")
            self.memory(db, strategy, symbol="QQQ", samples=30, win=0.2, pf=0.5, dd=-0.05, conf=0.3,
                        updated=datetime(2025, 1, 1))
            self.memory(db, strategy, symbol="SPY", samples=5, win=0.8, pf=2.0, dd=-0.01, conf=0.8,
                        updated=datetime(2025, 2, 1))
            snapshot = aggregate_strategy_memory(db, strategy.id)
            self.assertEqual(snapshot["sample_size"], 35)
            self.assertEqual(snapshot["slices"], 2)
            result = evaluate_single_strategy(db, strategy)
            self.assertEqual(result["decision"], "pause")
            self.assertEqual(strategy.current_status, "paused")

    def test_reactivation_requires_current_forward_evidence(self):
        with Session(self.engine) as db:
            strategy = self.strategy(db, "paused")
            self.memory(db, strategy, symbol="SPY", samples=30, win=0.6, pf=1.5, dd=-0.02, conf=0.7,
                        updated=datetime(2025, 1, 1))
            queue = reactivation_queue(db)
            self.assertFalse(queue["candidates"][0]["eligible"])
            self.assertTrue(any("forward" in item.lower() for item in queue["candidates"][0]["blockers"]))
            blocked = review_strategy_reactivation(db, strategy.id, "approve", "test")
            self.assertEqual(blocked["status"], "blocked")
            self.assertEqual(blocked["new_status"], "paused")

            evidence = StockPaperStrategyEvidence(
                strategy_id=strategy.id, evidence_id="fresh", status="suspended", verified_drawdown=Decimal("0"),
                consecutive_losses=0, observed_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc) + timedelta(days=1), provenance={})
            db.add(evidence)
            db.commit()
            self.assertEqual(review_strategy_reactivation(db, strategy.id, "approve", "test")["status"], "blocked")
            evidence.status = "approved"
            db.commit()
            approved = review_strategy_reactivation(db, strategy.id, "approve", "test")
            self.assertEqual(approved["new_status"], "paper_trading_active")


class JournalOutcomeTimingTests(unittest.TestCase):
    def row(self, entry_date):
        return CandidateDecisionJournal(symbol="SPY", strategy_type="x", decision="approve", status="complete",
                                        reason="r", evidence_snapshot={
                                            "market_data": {"latest_date": entry_date, "latest_close": 100},
                                            "cached_candidate": {"horizon_days": 5}})

    def prices(self, n):
        dates = pd.bdate_range("2025-01-02", periods=n)
        return pd.DataFrame({"date": dates.strftime("%Y-%m-%d"), "close": [100 + i for i in range(n)]})

    def test_partial_horizon_is_not_scored_as_outcome(self):
        with patch.object(decision_journal, "get_price_history", return_value=(self.prices(3), "test")):
            result = decision_journal._market_follow_through(None, self.row("2025-01-02"))
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["return"])
        self.assertIsNotNone(result["partial_return"])

    def test_entry_older_than_price_window_is_missing(self):
        with patch.object(decision_journal, "get_price_history", return_value=(self.prices(20), "test")):
            result = decision_journal._market_follow_through(None, self.row("2024-06-01"))
        self.assertEqual(result["status"], "missing_entry_date")
        self.assertIsNone(result["return"])

    def test_complete_horizon_scores(self):
        with patch.object(decision_journal, "get_price_history", return_value=(self.prices(10), "test")):
            result = decision_journal._market_follow_through(None, self.row("2025-01-02"))
        self.assertEqual(result["status"], "complete")
        self.assertAlmostEqual(result["return"], 0.05)


class SchedulingTests(unittest.TestCase):
    def test_edge_decay_job_is_scheduled_and_lease_locked(self):
        from app.tasks import jobs
        from app.tasks.celery_app import celery_app
        self.assertIn("strategy_edge_decay_job", jobs.REDIS_LOCKED_JOBS)
        self.assertEqual(celery_app.conf.beat_schedule["strategy-edge-decay"]["task"],
                         "app.tasks.jobs.strategy_edge_decay_job")

    def test_learning_routes_are_not_shadowed_by_trial_id_route(self):
        from app.api.stock_forward_trial import router
        paths = [route.path for route in router.routes]
        self.assertLess(paths.index("/stock/forward-trials/learning/mistakes"), paths.index("/stock/forward-trials/{trial_id}"))
        self.assertIn("/stock/forward-trials/{trial_id}/mistakes", paths)


if __name__ == "__main__":
    unittest.main()
