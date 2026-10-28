"""Batched scheduled signal generation: same signals, O(symbols) data loads."""
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401  register all tables
from app.db.base import Base
from app.models import Strategy, StrategySignal
from app.models.stock_paper import StockPaperLedgerEvent
from app.services import stock_paper_signal_batch as batch
from app.services.stock_paper_ledger import (
    StockPaperError,
    create_stock_paper_research_signal,
    create_stock_paper_signal,
)
from app.services.strategies.registry import STRATEGY_REGISTRY
from app.services.trusted_data import UntrustedMarketData

STRATEGY_STATUSES = {
    "moving_average_crossover": "paper_trading_active",
    "rsi_mean_reversion": "research",
    "macd_momentum": "paper_trading_candidate",
    "ensemble": "research",
    "model_predictive_long": "paper_trading_candidate",
    "bollinger_mean_reversion": "research",
    "channel_breakout": "research",
    "trend_pullback": "research",
}


def _frame(seed: int, rows: int = 260) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.012, rows)))
    start = date.today() - timedelta(days=rows)
    return pd.DataFrame({
        "date": [start + timedelta(days=index) for index in range(rows)],
        "open": close * 0.999,
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "volume": rng.integers(1_000_000, 3_000_000, rows),
        "source": "yfinance",
    })


class _DataDouble:
    """Counts trusted daily/intraday loads made by signal generation."""

    def __init__(self, symbols, *, untrusted=()):
        self.frames = {symbol: _frame(index) for index, symbol in enumerate(symbols)}
        self.untrusted = set(untrusted)
        self.daily_calls: list[str] = []
        self.intraday_calls: list[str] = []
        self.observed_at = datetime.now(timezone.utc).replace(microsecond=0)

    def history(self, _db, symbol, **_kwargs):
        self.daily_calls.append(symbol)
        if symbol in self.untrusted:
            raise UntrustedMarketData("Market prices are stale or future dated.")
        return self.frames[symbol], "database:yfinance"

    def observation(self, _db, symbol, **_kwargs):
        self.intraday_calls.append(symbol)
        return {
            "provider": "alpaca_iex", "feed_class": "iex", "timeframe": "1m",
            "opened_at": self.observed_at, "exchange_timestamp": self.observed_at,
            "ingested_at": self.observed_at,
            "close": round(float(self.frames[symbol]["close"].iloc[-1]), 4),
            "status": "ready", "adjustment_policy": "raw current-session execution observation",
        }

    def patches(self):
        return [
            patch("app.services.stock_paper_ledger.trusted_history", side_effect=self.history),
            patch("app.services.stock_paper_ledger.trusted_intraday_observation", side_effect=self.observation),
        ]


class SignalBatchTests(unittest.TestCase):
    def setUp(self):
        batch.clear_signal_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + str(Path(self.tmp.name) / "batch.sqlite"))
        Base.metadata.create_all(self.engine)
        with Session(self.engine) as db:
            for slug, status in STRATEGY_STATUSES.items():
                db.add(Strategy(name=slug, strategy_type=slug, parameters={}, is_active=True,
                                current_status=status))
            db.commit()

    def tearDown(self):
        batch.clear_signal_cache()
        self.engine.dispose()
        self.tmp.cleanup()

    def _strategies(self, db):
        return db.query(Strategy).order_by(Strategy.id).all()

    def _run(self, data, symbols):
        with Session(self.engine) as db:
            with patch.object(Session, "commit", autospec=True, side_effect=Session.commit) as commit:
                stack = data.patches()
                for item in stack:
                    item.start()
                try:
                    outcomes, stats = batch.generate_cycle_signals(db, symbols, self._strategies(db))
                finally:
                    for item in stack:
                        item.stop()
            return outcomes, stats, commit.call_count

    def test_fifty_symbols_eight_strategies_load_each_symbol_once_and_commit_once(self):
        self.assertEqual(set(STRATEGY_STATUSES), set(STRATEGY_REGISTRY))
        symbols = [f"S{index:02d}" for index in range(50)]
        data = _DataDouble(symbols)
        started = time.perf_counter()
        outcomes, stats, commits = self._run(data, symbols)
        elapsed = time.perf_counter() - started

        # O(symbols) data loads, never O(symbols x strategies).
        self.assertEqual(sorted(data.daily_calls), sorted(symbols))
        self.assertEqual(sorted(data.intraday_calls), sorted(symbols))
        self.assertEqual(stats["daily_loads"], 50)
        self.assertEqual(stats["intraday_loads"], 50)
        self.assertEqual(stats["strategy_resolutions"], 8)
        self.assertEqual(commits, 1)
        self.assertEqual(stats["signals_persisted"], 400)
        self.assertEqual(len(outcomes), 400)
        self.assertTrue(all(isinstance(value, dict) for value in outcomes.values()))
        # Cold pass (every model fitted) stays inside the 20 s cycle budget.
        self.assertLess(elapsed, 20.0)
        with Session(self.engine) as db:
            self.assertEqual(db.query(StrategySignal).count(), 400)
            self.assertEqual(
                db.query(StockPaperLedgerEvent).filter_by(event_type="signal", status="generated").count(), 400,
            )

        # Unchanged daily frames reuse the memoized strategy outputs.
        data.daily_calls.clear()
        data.intraday_calls.clear()
        started = time.perf_counter()
        _outcomes, warm, _commits = self._run(data, symbols)
        self.assertEqual(warm["strategy_computes"], 0)
        self.assertEqual(warm["strategy_cache_hits"], 400)
        self.assertEqual(len(data.daily_calls), 50)
        self.assertLess(time.perf_counter() - started, elapsed)

    def test_batched_signals_match_per_pair_signals_exactly(self):
        symbols = ["AAPL", "SPY"]
        data = _DataDouble(symbols)
        outcomes, _stats, _commits = self._run(data, symbols)
        with Session(self.engine) as db:
            batched = {
                (row.symbol, row.strategy_id): row
                for row in db.query(StrategySignal).all()
            }
            stack = data.patches()
            for item in stack:
                item.start()
            try:
                for strategy in self._strategies(db):
                    for symbol in symbols:
                        research_only = strategy.current_status != "paper_trading_active"
                        single = (
                            create_stock_paper_research_signal(db, symbol, strategy.strategy_type)
                            if research_only else create_stock_paper_signal(db, symbol, strategy.strategy_type)
                        )
                        produced = outcomes[(symbol, strategy.strategy_type)]
                        ignore = {"signal_id"}
                        self.assertEqual(
                            {k: v for k, v in produced.items() if k not in ignore},
                            {k: v for k, v in single.items() if k not in ignore},
                        )
                        row = db.get(StrategySignal, single["signal_id"])
                        twin = batched[(symbol, strategy.id)]
                        self.assertEqual(twin.features, row.features)
                        self.assertEqual(
                            (twin.action, twin.confidence, twin.probability_up, twin.reason),
                            (row.action, row.confidence, row.probability_up, row.reason),
                        )
                        self.assertEqual(twin.features["research_only"], research_only)
            finally:
                for item in stack:
                    item.stop()

    def test_untrusted_symbol_fails_only_its_own_pairs(self):
        symbols = ["AAPL", "MSFT", "SPY"]
        data = _DataDouble(symbols, untrusted={"MSFT"})
        outcomes, stats, _commits = self._run(data, symbols)
        for slug in STRATEGY_STATUSES:
            self.assertIsInstance(outcomes[("MSFT", slug)], StockPaperError)
            self.assertIn("stale", str(outcomes[("MSFT", slug)]))
            self.assertIsInstance(outcomes[("AAPL", slug)], dict)
            self.assertIsInstance(outcomes[("SPY", slug)], dict)
        self.assertEqual(stats["signals_persisted"], 16)

    def test_unknown_strategy_and_compute_failure_are_isolated(self):
        symbols = ["AAPL"]
        data = _DataDouble(symbols)
        with Session(self.engine) as db:
            strategies = self._strategies(db)
            ghost = type("Ghost", (), {"strategy_type": "ghost", "current_status": "research", "id": 999})()
            real_factory = batch.ledger.get_strategy

            def factory(slug, parameters=None):
                if slug == "channel_breakout":
                    raise RuntimeError("boom")
                return real_factory(slug, parameters)

            stack = data.patches() + [patch("app.services.stock_paper_ledger.get_strategy", side_effect=factory)]
            for item in stack:
                item.start()
            try:
                outcomes, stats = batch.generate_cycle_signals(db, symbols, [*strategies, ghost])
            finally:
                for item in stack:
                    item.stop()
        self.assertIsInstance(outcomes[("AAPL", "ghost")], StockPaperError)
        self.assertIn("Unknown strategy", str(outcomes[("AAPL", "ghost")]))
        self.assertIsInstance(outcomes[("AAPL", "channel_breakout")], RuntimeError)
        self.assertEqual(stats["signals_persisted"], 7)


if __name__ == "__main__":
    unittest.main()
