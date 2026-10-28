"""Paper exploration: small tagged learning orders from candidate strategies."""
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # register all tables
from app.core.config import settings
from app.db.base import Base
from app.models import RiskRule, Strategy, StrategySignal
from app.models.stock_paper import StockPaperAccount, StockPaperFill, StockPaperOrder
from app.services.stock_paper_autotrader import run_stock_paper_signal_cycle
from app.services.stock_paper_ledger import (
    EXPLORATION_SOURCE,
    StockPaperError,
    initialize_stock_paper_account,
    reserve_stock_paper_order,
)
from tests.test_stock_paper_ledger import FakeAlpaca


class ExplorationLedgerTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(settings, "active_paper_broker", "alpaca_paper"),
            patch.object(settings, "paper_broker_account_id", ""),
            patch.object(settings, "paper_exploration_enabled", True),
            patch.object(settings, "paper_exploration_max_positions", 2),
        ]
        for item in self.patches:
            item.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + str(Path(self.tmp.name) / "explore.sqlite"))
        Base.metadata.create_all(self.engine)
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.engine.dispose()
        self.tmp.cleanup()

    def gates(self):
        now = self.now
        return [
            patch("app.services.stock_paper_ledger.session_bounds",
                  return_value=(now.replace(hour=0), now.replace(hour=23))),
            patch("app.services.stock_paper_ledger.feed_status", return_value={"status": "ready"}),
            patch("app.services.stock_paper_ledger._validate_reference_price"),
            patch("app.services.stock_learning_cycle.evaluate_launch_admission_prerequisites",
                  return_value={"all": {"status": "pass"}}),
        ]

    def seed(self, db, status="paper_trading_candidate", symbol="SPY", confidence="0.8"):
        if not db.query(StockPaperAccount).first():
            initialize_stock_paper_account(db, FakeAlpaca())
        strategy = Strategy(name=f"Explorer {status} {symbol}", strategy_type=f"macd_momentum_{symbol}",
                            parameters={}, is_active=True, current_status=status)
        db.add(strategy)
        db.flush()
        signal = StrategySignal(strategy_id=strategy.id, symbol=symbol, signal_time=self.now, action="BUY",
                                probability_up=Decimal("0.8"), probability_down=Decimal("0.2"),
                                confidence=Decimal(confidence), features={"test": True})
        db.add(signal)
        if not db.query(RiskRule).first():
            db.add(RiskRule(name="explore", value={"max_open_positions": 10}))
        db.commit()
        account = db.query(StockPaperAccount).one()
        return account, strategy, signal

    def budget_quantity(self, account, price=Decimal("100")):
        budget = account.equity * Decimal(str(settings.paper_exploration_risk_fraction))
        return (budget / price).quantize(Decimal("0.000001"))

    def reserve(self, db, signal, quantity, key="explore-1", symbol="SPY"):
        return reserve_stock_paper_order(
            db, symbol=symbol, side="buy", quantity=quantity, reference_price=Decimal("100"),
            idempotency_key=key, source=EXPLORATION_SOURCE, signal_id=signal.id,
        )

    def test_candidate_buy_without_evidence_is_admitted_and_tagged(self):
        with Session(self.engine) as db, ExitStack() as stack:
            for item in self.gates():
                stack.enter_context(item)
            account, strategy, signal = self.seed(db)
            order = self.reserve(db, signal, self.budget_quantity(account))
            self.assertEqual(order.source, EXPLORATION_SOURCE)
            self.assertEqual(order.strategy_id, strategy.id)
            self.assertIsNone(order.evidence_id)  # never promotion evidence

    def test_exploration_is_refused_when_disabled(self):
        with Session(self.engine) as db, ExitStack() as stack, \
                patch.object(settings, "paper_exploration_enabled", False):
            for item in self.gates():
                stack.enter_context(item)
            account, _strategy, signal = self.seed(db)
            with self.assertRaisesRegex(StockPaperError, "disabled"):
                self.reserve(db, signal, self.budget_quantity(account))

    def test_active_strategy_cannot_use_exploration_path(self):
        with Session(self.engine) as db, ExitStack() as stack:
            for item in self.gates():
                stack.enter_context(item)
            account, _strategy, signal = self.seed(db, status="paper_trading_active")
            with self.assertRaisesRegex(StockPaperError, "candidate or research"):
                self.reserve(db, signal, self.budget_quantity(account))

    def test_oversized_or_low_confidence_exploration_is_refused(self):
        with Session(self.engine) as db, ExitStack() as stack:
            for item in self.gates():
                stack.enter_context(item)
            account, _strategy, signal = self.seed(db)
            with self.assertRaisesRegex(StockPaperError, "exploration budget"):
                self.reserve(db, signal, self.budget_quantity(account) * 2)
            _a, _s, weak = self.seed(db, symbol="QQQ", confidence="0.1")
            with self.assertRaisesRegex(StockPaperError, "confidence"):
                self.reserve(db, weak, self.budget_quantity(account), key="explore-weak", symbol="QQQ")

    def test_lot_limit_counts_held_and_pending_exploration_lots(self):
        with Session(self.engine) as db, ExitStack() as stack:
            for item in self.gates():
                stack.enter_context(item)
            account, _s1, first = self.seed(db, symbol="SPY")
            _a, _s2, second = self.seed(db, symbol="QQQ")
            _a, _s3, third = self.seed(db, symbol="AAPL")
            quantity = self.budget_quantity(account)
            self.reserve(db, first, quantity, key="e1", symbol="SPY")
            self.reserve(db, second, quantity, key="e2", symbol="QQQ")
            with self.assertRaisesRegex(StockPaperError, "lot limit"):
                self.reserve(db, third, quantity, key="e3", symbol="AAPL")


class _Query:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *args, **kwargs):
        return self

    def filter_by(self, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None

    def one_or_none(self):
        return self.rows[0] if self.rows else None


class _Db:
    def __init__(self, strategies):
        self.strategies = strategies

    def query(self, model):
        return _Query(self.strategies if getattr(model, "__name__", "") == "Strategy" else [])

    def rollback(self):
        pass


def _run_cycle(enabled):
    strategy = SimpleNamespace(id=3, strategy_type="macd_momentum", current_status="paper_trading_candidate")
    account = SimpleNamespace(id=1, status="reconciled", reconciliation_required=False,
                              unexplained_residual=False, equity=Decimal("100000"))
    order = SimpleNamespace(id=9, status="accepted")
    with patch.object(settings, "paper_execution_symbols", ["AAPL"]), \
         patch.object(settings, "paper_exploration_enabled", enabled), \
         patch("app.services.stock_paper_autotrader.active_paper_account", return_value=account), \
         patch("app.services.stock_paper_autotrader._is_regular_session", return_value=True), \
         patch("app.services.stock_paper_autotrader.create_stock_paper_research_signal", return_value={
             "signal_id": 5, "signal_action": "BUY", "reference_price": "100",
         }), \
         patch("app.services.stock_paper_autotrader.reserve_stock_paper_order", return_value=order) as reserve, \
         patch("app.services.stock_paper_autotrader.dispatch_reserved_order", return_value=order):
        result = run_stock_paper_signal_cycle(_Db([strategy]))
    return result, reserve


def test_cycle_routes_candidate_buys_through_small_exploration_orders():
    result, reserve = _run_cycle(enabled=True)
    assert result["submitted_count"] == 1
    kwargs = reserve.call_args.kwargs
    assert kwargs["source"] == EXPLORATION_SOURCE
    assert kwargs["idempotency_key"].startswith("explore-paper:")
    expected = Decimal("100000") * Decimal(str(settings.paper_exploration_risk_fraction)) / Decimal("100")
    # Exploration entries carry broker-side bracket exits, which Alpaca only
    # accepts for whole shares: the risk-sized quantity is rounded down.
    assert kwargs["quantity"] == expected.quantize(Decimal("1"), rounding="ROUND_DOWN")
    assert kwargs["exit_plan"]["order_class"] == "bracket"
    assert result["results"][0]["mode"] == "explore"


def test_cycle_keeps_candidates_observation_only_when_exploration_is_off():
    result, reserve = _run_cycle(enabled=False)
    reserve.assert_not_called()
    assert result["results"][0]["status"] == "observed"
