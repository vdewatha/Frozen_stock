"""The many-symbol data checks decide exactly what the per-symbol calls decide.

Readiness and monitoring used to ask the database three times a symbol; the
batched readers ask a fixed number of times for the whole universe. These
tests hold the two paths to the same answers and count the queries.
"""
import unittest
from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.base import Base
from app.models import Asset, IntradayNoTradeInterval, MarketPrice
import app.models.stock_paper  # noqa: F401
from app.services import intraday_data, market_data
from app.services.trusted_data import UntrustedMarketData, daily_history_checks, trusted_history

UTC = timezone.utc
MINUTE = timedelta(minutes=1)
# Friday 2026-09-11 regular session: 13:30-20:00 UTC.
OPEN = datetime(2026, 9, 11, 13, 30, tzinfo=UTC)
SYMBOLS = ["AAPL", "MSFT", "QQQ", "SPY"]


def bar(opened_at: datetime) -> dict:
    return {"t": opened_at.isoformat().replace("+00:00", "Z"), "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 300}


class QueryCounter:
    def __init__(self, engine):
        self.engine, self.count = engine, 0

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._seen)
        return self

    def __exit__(self, *_exc):
        event.remove(self.engine, "before_cursor_execute", self._seen)

    def _seen(self, _conn, _cursor, statement, *_rest):
        self.count += statement.lstrip().upper().startswith("SELECT")


class FeedStatusesTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.stack = ExitStack()
        for target, name, value in (
            (settings, "active_market_data_provider", "alpaca_iex"),
            (intraday_data, "MARKET_DATA_PROVIDER", "alpaca_iex"),
            (intraday_data, "MARKET_DATA_FEED_CLASS", "iex"),
            (settings, "paper_alpaca_api_key", type(settings.paper_alpaca_api_key)("key")),
            (settings, "paper_alpaca_api_secret", type(settings.paper_alpaca_api_secret)("secret")),
        ):
            self.stack.enter_context(patch.object(target, name, value))

    def tearDown(self):
        self.stack.close()
        self.db.close()
        self.engine.dispose()

    def store(self, symbol: str, minutes: list[datetime], *, at: datetime) -> None:
        intraday_data.upsert_intraday_bars(
            self.db, symbol, [bar(minute) for minute in minutes], ingested_at=at, verify_entitlement=True,
        )

    def seed(self, observed: datetime) -> None:
        """Four symbols in four states at ``observed``."""
        through = int((observed - OPEN).total_seconds() // 60)
        every = [OPEN + MINUTE * index for index in range(min(through, 390))]
        # SPY: every minute, verified just now.
        self.store("SPY", every, at=observed)
        # QQQ: a three-minute hole, one of those minutes a verified no-trade minute.
        hole = set(every[40:43])
        self.store("QQQ", [minute for minute in every if minute not in hole], at=observed)
        self.db.add(IntradayNoTradeInterval(
            symbol="QQQ", timeframe="1m", opened_at=every[41], provider="alpaca_iex", feed_class="iex",
            verified_at=observed, query_start=every[40], query_end=every[43],
            previous_bar_at=every[39], next_bar_at=every[43], run_minutes=3,
        ))
        # MSFT: every minute, but last verified half an hour ago.
        self.store("MSFT", every, at=observed - timedelta(minutes=30))
        # AAPL: nothing stored.
        self.db.commit()

    def assert_same(self, observed: datetime) -> dict:
        self.db.expire_all()
        with QueryCounter(self.engine) as batched:
            together = intraday_data.feed_statuses(self.db, SYMBOLS, now=observed)
        with QueryCounter(self.engine) as looped:
            one_by_one = {symbol: intraday_data.feed_status(self.db, symbol, now=observed) for symbol in SYMBOLS}
        self.assertEqual(together, one_by_one)
        self.assertLessEqual(batched.count, 3)
        self.looped_queries = looped.count
        return together

    def test_open_session_matches_the_per_symbol_answer(self):
        observed = OPEN + timedelta(hours=4, seconds=20)
        self.seed(observed - timedelta(seconds=20))
        statuses = self.assert_same(observed)
        self.assertEqual({symbol: row["status"] for symbol, row in statuses.items()}, {
            "AAPL": "unavailable", "MSFT": "unavailable", "QQQ": "incomplete", "SPY": "ready",
        })
        self.assertEqual(statuses["MSFT"]["failure_class"], "entitlement")
        self.assertEqual(len(statuses["QQQ"]["missing_intervals"]), 2)
        self.assertEqual(len(statuses["QQQ"]["verified_no_trade_intervals"]), 1)
        self.assertEqual(self.looped_queries, 3 * len(SYMBOLS))

    def test_closed_market_matches_the_per_symbol_answer(self):
        observed = OPEN + timedelta(hours=9)  # 22:30 UTC, after the close
        self.seed(observed - timedelta(seconds=30))
        statuses = self.assert_same(observed)
        self.assertEqual(statuses["SPY"]["status"], "market_closed")
        self.assertEqual(statuses["QQQ"]["status"], "incomplete")

    def test_before_the_first_expected_minute_matches(self):
        observed = OPEN + timedelta(seconds=30)
        self.assert_same(observed)

    def test_symbols_outside_the_universe_are_left_to_the_single_call(self):
        statuses = intraday_data.feed_statuses(self.db, ["SPY", "ZZZZ", " spy "], now=OPEN + timedelta(hours=1))
        self.assertEqual(list(statuses), ["SPY"])
        with self.assertRaises(ValueError):
            intraday_data.feed_status(self.db, "ZZZZ", now=OPEN + timedelta(hours=1))


class PriceHistoriesTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.today = date.today()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def add(self, symbol: str, source: str | None, days: int, *, newest_days_ago: int = 0, close: float = 101.0) -> None:
        for index in range(days):
            self.db.add(MarketPrice(
                symbol=symbol, price_date=self.today - timedelta(days=newest_days_ago + index), source=source,
                open=100, high=102, low=98, close=close, adjusted_close=close, volume=10,
            ))

    def seed(self) -> list[str]:
        self.add("AAA", "yfinance", 300)                          # more rows than the limit
        self.add("BBB", "yfinance", 120, newest_days_ago=3)       # older provider ...
        self.add("BBB", "alpaca_iex_daily", 90)                   # ... loses to the fresher one
        self.add("CCC", "yfinance", 20)                           # too short
        self.add("DDD", "yfinance", 100, newest_days_ago=30)      # stale
        self.add("EEE", "synthetic_fallback", 100)                # untrusted provider
        self.add("FFF", None, 100)                                # provider not recorded
        self.add("GGG", "yfinance", 100, close=0)                 # invalid candles
        self.db.commit()
        return ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "NONE"]

    def test_histories_match_the_single_symbol_reader(self):
        symbols = self.seed()
        with QueryCounter(self.engine) as batched:
            together = market_data.get_price_histories(self.db, symbols, 260)
        for symbol in symbols:
            frame, source = market_data.get_price_history(self.db, symbol, 260, auto_seed=False)
            self.assertEqual(together[symbol][1], source, symbol)
            self.assertEqual(together[symbol][0].to_dict("records"), frame.to_dict("records"), symbol)
        self.assertEqual(len(together["AAA"][0]), 260)
        self.assertEqual(together["BBB"][1], "database:alpaca_iex_daily")
        # Two queries for the batch, plus the single-symbol read of FFF.
        self.assertLessEqual(batched.count, 4)

    def test_daily_checks_match_trusted_history(self):
        symbols = self.seed()
        checks = daily_history_checks(self.db, symbols)
        for symbol in symbols:
            try:
                trusted_history(self.db, symbol, require_active=False)
                expected = None
            except UntrustedMarketData as exc:
                expected = str(exc)
            self.assertEqual(checks[symbol]["error"], expected, symbol)
            newest = (
                self.db.query(MarketPrice).filter(MarketPrice.symbol == symbol)
                .order_by(MarketPrice.price_date.desc()).first()
            )
            self.assertEqual(checks[symbol]["latest_date"], newest.price_date if newest else None, symbol)
        self.assertIsNone(checks["AAA"]["error"])
        self.assertEqual(checks["BBB"]["latest_source"], "alpaca_iex_daily")
        self.assertEqual(checks["FFF"]["latest_source"], "unknown")
        self.assertIsNotNone(checks["CCC"]["error"])

    def test_chunks_cover_every_symbol(self):
        symbols = self.seed()
        self.assertEqual(
            {symbol: len(frame) for symbol, (frame, _source) in market_data.get_price_histories(self.db, symbols, 50, chunk=3).items()},
            {symbol: len(market_data.get_price_history(self.db, symbol, 50, auto_seed=False)[0]) for symbol in symbols},
        )


class ReadinessUsesTheBatchTests(unittest.TestCase):
    def test_market_data_and_intraday_checks_do_not_loop_over_symbols(self):
        from app.services import readiness

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            symbols = ["AAPL", "MSFT", "QQQ", "SPY"]
            db.add_all([Asset(symbol=symbol, asset_type="stock", is_active=True) for symbol in symbols])
            db.commit()
            with patch.object(readiness, "trusted_history", create=True, side_effect=AssertionError("per-symbol daily read")), \
                 patch.object(readiness, "feed_status", side_effect=AssertionError("per-symbol feed read")), \
                 patch.object(settings, "paper_execution_symbols", symbols):
                snapshot = readiness.readiness_snapshot(db)
            names = {check["name"] for check in snapshot["checks"]}
            self.assertIn("Market data", names)
            self.assertIn("Intraday feed", names)
        finally:
            db.close()
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
