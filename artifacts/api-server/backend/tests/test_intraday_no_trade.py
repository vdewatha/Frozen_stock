"""Partial-venue (Alpaca IEX) no-trade minutes versus genuine feed outages."""
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.base import Base
from app.models import IntradayBar, IntradayNoTradeInterval
import app.models.stock_paper  # noqa: F401
from app.services import intraday_data
from app.services.trusted_data import UntrustedMarketData, validate_intraday_readiness

UTC = timezone.utc
MINUTE = timedelta(minutes=1)
# Friday 2026-09-11 regular session: 13:30-20:00 UTC. Thursday is 2026-09-10.
FRIDAY_OPEN = datetime(2026, 9, 11, 13, 30, tzinfo=UTC)
THURSDAY_OPEN = datetime(2026, 9, 10, 13, 30, tzinfo=UTC)
SESSION_MINUTES = 390


def bar(opened_at: datetime) -> dict:
    return {
        "t": opened_at.isoformat().replace("+00:00", "Z"),
        "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 300,
    }


def iex_fetch(skip: set[datetime], *, last_print: datetime | None = None):
    """Fake successful IEX responses: a bar for every minute except ``skip``."""
    calls = []

    def fetch(symbol, start, end):
        calls.append((start, end))
        rows = []
        cursor = start
        while cursor < end:
            if cursor not in skip and (last_print is None or cursor <= last_print):
                rows.append(bar(cursor))
            cursor += MINUTE
        return rows, 0, False

    fetch.calls = calls
    return fetch


class IntradayNoTradeTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.stack = ExitStack()
        self.use_provider("alpaca_iex", "iex")
        self.stack.enter_context(patch.object(
            settings, "paper_alpaca_api_key", type(settings.paper_alpaca_api_key)("key")))
        self.stack.enter_context(patch.object(
            settings, "paper_alpaca_api_secret", type(settings.paper_alpaca_api_secret)("secret")))
        self.stack.enter_context(patch.object(
            settings, "tradier_market_data_api_key",
            type(settings.tradier_market_data_api_key)("key")))
        self.stack.enter_context(patch.object(settings, "intraday_backfill_chunks_per_cycle", 8))

    def tearDown(self):
        self.stack.close()
        self.db.close()
        self.engine.dispose()

    def use_provider(self, provider: str, feed_class: str):
        self.stack.enter_context(patch.object(settings, "active_market_data_provider", provider))
        self.stack.enter_context(patch.object(intraday_data, "MARKET_DATA_PROVIDER", provider))
        self.stack.enter_context(patch.object(intraday_data, "MARKET_DATA_FEED_CLASS", feed_class))

    def seed_thursday(self, provider="alpaca_iex", feed_class="iex"):
        intraday_data.upsert_intraday_bars(
            self.db, "SPY",
            [bar(THURSDAY_OPEN + MINUTE * i) for i in range(SESSION_MINUTES)],
            ingested_at=datetime(2026, 9, 10, 21, 0, tzinfo=UTC),
            provider=provider, feed_class=feed_class, verify_entitlement=True,
        )

    def ingest(self, fetch, observed, **kwargs):
        with patch.object(intraday_data, "_fetch_bars", side_effect=fetch):
            return intraday_data.ingest_intraday(self.db, ["SPY"], now=observed, **kwargs)

    def markers(self):
        return sorted(
            intraday_data._aware_utc(row.opened_at)
            for row in self.db.query(IntradayNoTradeInterval).all()
        )

    def test_single_iex_no_trade_minute_no_longer_blocks_readiness(self):
        self.seed_thursday()
        observed = datetime(2026, 9, 11, 15, 0, 30, tzinfo=UTC)
        hole = datetime(2026, 9, 11, 14, 20, tzinfo=UTC)
        result = self.ingest(iex_fetch({hole}), observed)["results"][0]

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["missing_intervals"], [])
        self.assertIsNone(result["oldest_unresolved_interval"])
        self.assertIsNone(result["deferred_window"])
        self.assertEqual(result["verified_no_trade_intervals"], [hole.isoformat()])

        status = intraday_data.feed_status(self.db, "SPY", now=observed)
        self.assertEqual(status["status"], "ready")
        self.assertEqual(status["missing_intervals"], [])
        self.assertEqual(status["verified_no_trade_intervals"], [hole.isoformat()])
        validate_intraday_readiness(self.db, "SPY", now=observed)

        # No price row is fabricated for the no-trade minute.
        self.assertIsNone(
            self.db.query(IntradayBar).filter_by(symbol="SPY", opened_at=hole).one_or_none()
        )
        marker = self.db.query(IntradayNoTradeInterval).one()
        self.assertEqual(marker.provider, "alpaca_iex")
        self.assertEqual(marker.feed_class, "iex")
        self.assertEqual(marker.reason, "provider_returned_no_bar")
        self.assertEqual(marker.run_minutes, 1)
        self.assertEqual(intraday_data._aware_utc(marker.previous_bar_at), hole - MINUTE)
        self.assertEqual(intraday_data._aware_utc(marker.next_bar_at), hole + MINUTE)

    def test_historical_no_trade_minutes_resolve_deferred_windows(self):
        # Mirrors the hosted report: QQQ-style 16:50 and SPY-style 17:57 holes.
        holes = {
            datetime(2026, 9, 10, 16, 50, tzinfo=UTC),
            datetime(2026, 9, 10, 17, 57, tzinfo=UTC),
            datetime(2026, 9, 10, 17, 58, tzinfo=UTC),
        }
        observed = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)  # before Friday open
        result = None
        for _ in range(4):  # bounded chunks per cycle; repair converges
            result = self.ingest(iex_fetch(holes), observed)["results"][0]
        self.assertEqual(result["status"], "complete")
        self.assertIsNone(result["deferred_window"])
        self.assertIsNone(result["oldest_unresolved_interval"])
        self.assertEqual(self.markers(), sorted(holes))
        status = intraday_data.feed_status(self.db, "SPY", now=observed)
        self.assertEqual(status["status"], "market_closed")
        self.assertEqual(status["missing_intervals"], [])

    def test_run_longer_than_threshold_stays_missing_and_blocks(self):
        self.seed_thursday()
        observed = datetime(2026, 9, 11, 15, 0, 30, tzinfo=UTC)
        start = datetime(2026, 9, 11, 14, 20, tzinfo=UTC)
        outage = {start + MINUTE * i for i in range(6)}
        result = self.ingest(iex_fetch(outage), observed)["results"][0]

        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["oldest_unresolved_interval"], start.isoformat())
        self.assertEqual(result["verified_no_trade_intervals"], [])
        self.assertEqual(self.markers(), [])
        with self.assertRaises(UntrustedMarketData):
            validate_intraday_readiness(self.db, "SPY", now=observed)

    def test_run_at_threshold_is_resolved(self):
        self.seed_thursday()
        observed = datetime(2026, 9, 11, 15, 0, 30, tzinfo=UTC)
        start = datetime(2026, 9, 11, 14, 20, tzinfo=UTC)
        run = {start + MINUTE * i for i in range(5)}
        result = self.ingest(iex_fetch(run), observed)["results"][0]
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.markers(), sorted(run))

    def test_feed_that_stops_printing_stays_blocked(self):
        self.seed_thursday()
        observed = datetime(2026, 9, 11, 15, 0, 30, tzinfo=UTC)
        last_print = datetime(2026, 9, 11, 14, 55, tzinfo=UTC)
        result = self.ingest(iex_fetch(set(), last_print=last_print), observed)["results"][0]

        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["oldest_unresolved_interval"], (last_print + MINUTE).isoformat())
        self.assertEqual(self.markers(), [])
        with self.assertRaises(UntrustedMarketData):
            validate_intraday_readiness(self.db, "SPY", now=observed)

    def test_empty_provider_response_never_creates_markers(self):
        observed = datetime(2026, 9, 11, 15, 0, 30, tzinfo=UTC)
        result = self.ingest(lambda symbol, start, end: ([], 0, False), observed)["results"][0]
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(self.markers(), [])

    def test_provider_failure_never_creates_markers(self):
        self.seed_thursday()
        observed = datetime(2026, 9, 11, 15, 0, 30, tzinfo=UTC)

        def fail(symbol, start, end):
            raise RuntimeError("Alpaca IEX market-data service is unavailable")

        result = self.ingest(fail, observed)["results"][0]
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(self.markers(), [])

    def test_session_open_edge_stays_missing(self):
        self.seed_thursday()
        observed = datetime(2026, 9, 11, 15, 0, 30, tzinfo=UTC)
        result = self.ingest(iex_fetch({FRIDAY_OPEN}), observed)["results"][0]
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["oldest_unresolved_interval"], FRIDAY_OPEN.isoformat())
        self.assertEqual(self.markers(), [])

    def test_consolidated_feed_gap_is_never_marked(self):
        self.use_provider("tradier", "sip")
        self.seed_thursday(provider="tradier", feed_class="sip")
        observed = datetime(2026, 9, 11, 15, 0, 30, tzinfo=UTC)
        hole = datetime(2026, 9, 11, 14, 20, tzinfo=UTC)
        result = self.ingest(iex_fetch({hole}), observed)["results"][0]
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["oldest_unresolved_interval"], hole.isoformat())
        self.assertEqual(self.markers(), [])

    def test_threshold_zero_disables_no_trade_resolution(self):
        self.seed_thursday()
        observed = datetime(2026, 9, 11, 15, 0, 30, tzinfo=UTC)
        hole = datetime(2026, 9, 11, 14, 20, tzinfo=UTC)
        with patch.object(settings, "intraday_no_trade_max_run_minutes", 0):
            result = self.ingest(iex_fetch({hole}), observed)["results"][0]
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(self.markers(), [])

    def test_unverified_fixture_bars_cannot_bracket_a_no_trade_minute(self):
        observed = datetime(2026, 9, 11, 15, 0, 30, tzinfo=UTC)
        hole = datetime(2026, 9, 11, 14, 20, tzinfo=UTC)
        intraday_data.upsert_intraday_bars(
            self.db, "SPY", [bar(hole - MINUTE), bar(hole + MINUTE)],
            ingested_at=observed, provider="alpaca_iex", feed_class="iex",
        )
        created = intraday_data.record_verified_no_trade_minutes(
            self.db, "SPY", hole - MINUTE, hole + 2 * MINUTE,
            observed_at=observed, provider="alpaca_iex", feed_class="iex",
        )
        self.assertEqual(created, [])

    def test_late_real_bar_supersedes_marker(self):
        observed = datetime(2026, 9, 11, 15, 0, 30, tzinfo=UTC)
        hole = datetime(2026, 9, 11, 14, 20, tzinfo=UTC)
        intraday_data.upsert_intraday_bars(
            self.db, "SPY", [bar(hole - MINUTE), bar(hole + MINUTE)],
            ingested_at=observed, provider="alpaca_iex", feed_class="iex",
            verify_entitlement=True,
        )
        window = (hole - MINUTE, hole + 2 * MINUTE)
        created = intraday_data.record_verified_no_trade_minutes(
            self.db, "SPY", *window,
            observed_at=observed, provider="alpaca_iex", feed_class="iex",
        )
        self.assertEqual(created, [hole.isoformat()])
        # Idempotent on re-query.
        self.assertEqual(intraday_data.record_verified_no_trade_minutes(
            self.db, "SPY", *window,
            observed_at=observed, provider="alpaca_iex", feed_class="iex",
        ), [])
        self.assertEqual(self.markers(), [hole])

        intraday_data.upsert_intraday_bars(
            self.db, "SPY", [bar(hole)], ingested_at=observed + MINUTE,
            provider="alpaca_iex", feed_class="iex", verify_entitlement=True,
        )
        intraday_data.record_verified_no_trade_minutes(
            self.db, "SPY", *window,
            observed_at=observed + MINUTE, provider="alpaca_iex", feed_class="iex",
        )
        self.assertEqual(self.markers(), [])
        self.assertIsNotNone(
            self.db.query(IntradayBar).filter_by(symbol="SPY", opened_at=hole).one_or_none()
        )

    def test_recently_closed_minute_waits_for_confirmation_delay(self):
        hole = datetime(2026, 9, 11, 14, 20, tzinfo=UTC)
        observed = hole + MINUTE + intraday_data.NO_TRADE_CONFIRMATION_DELAY - timedelta(seconds=1)
        intraday_data.upsert_intraday_bars(
            self.db, "SPY", [bar(hole - MINUTE)], ingested_at=observed,
            provider="alpaca_iex", feed_class="iex", verify_entitlement=True,
        )
        # Inject the trailing bar directly to isolate the delay rule.
        self.db.add(IntradayBar(
            symbol="SPY", timeframe="1m", opened_at=hole + MINUTE, open=100, high=101,
            low=99, close=100, volume=1, provider="alpaca_iex", feed_class="iex",
            exchange_timestamp=hole + MINUTE, ingested_at=observed, last_verified_at=observed,
        ))
        self.db.commit()
        created = intraday_data.record_verified_no_trade_minutes(
            self.db, "SPY", hole - MINUTE, hole + 2 * MINUTE,
            observed_at=observed, provider="alpaca_iex", feed_class="iex",
        )
        self.assertEqual(created, [])


if __name__ == "__main__":
    unittest.main()
