"""feed_status tolerates the provider's normal 1-minute bar publication lag.

Production observation (2026-10-27/28, Alpaca IEX): the bar for minute 19:57Z
was ingested at 19:59:01Z, and readiness alternated between ready and stale
within each minute. These tests replay that timeline.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.base import Base
import app.models
import app.models.stock_paper  # noqa: F401  register paper tables
from app.services import intraday_data
from tests.test_intraday_data import bar

UTC = timezone.utc
SESSION_OPEN = datetime(2026, 10, 27, 13, 30, tzinfo=UTC)


class PublicationLagTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.patches = [
            patch.object(settings, "active_market_data_provider", "alpaca_iex"),
            patch.object(intraday_data, "MARKET_DATA_PROVIDER", "alpaca_iex"),
            patch.object(intraday_data, "MARKET_DATA_FEED_CLASS", "iex"),
            patch.object(settings, "paper_alpaca_api_key", type(settings.paper_alpaca_api_key)("key")),
            patch.object(settings, "paper_alpaca_api_secret", type(settings.paper_alpaca_api_secret)("secret")),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.db.close()
        self.engine.dispose()

    def ingest(self, minute: datetime, at: datetime):
        result = intraday_data.upsert_intraday_bars(
            self.db, "SPY", [bar(minute)], ingested_at=at,
            provider="alpaca_iex", feed_class="iex", verify_entitlement=True,
        )
        self.assertEqual(result["rows_imported"], 1)

    def replay(self, *, through: datetime, last_ingested_minute: datetime | None = None):
        """Ingest every minute from the open, each one at the observed lag.

        Minute ``m`` (closes at m+1) is published and ingested at m+2min+1s,
        which matches "last_bar 19:57Z ingested 19:59:01Z".
        """
        minute = SESSION_OPEN
        stop = last_ingested_minute or through
        while minute <= stop:
            arrival = minute + timedelta(minutes=2, seconds=1)
            if arrival > through:
                break
            self.ingest(minute, arrival)
            minute += timedelta(minutes=1)

    def statuses_between(self, start: datetime, end: datetime, *, step_seconds: int = 5,
                         ingest_live: bool = True):
        """Feed status sampled every few seconds while bars keep arriving."""
        out = []
        moment = start
        next_minute = None
        latest = self.db.query(app.models.IntradayBar).order_by(app.models.IntradayBar.opened_at.desc()).first()
        if latest is not None:
            next_minute = intraday_data._aware_utc(latest.opened_at) + timedelta(minutes=1)
        while moment <= end:
            if ingest_live and next_minute is not None and moment >= next_minute + timedelta(minutes=2, seconds=1):
                self.ingest(next_minute, next_minute + timedelta(minutes=2, seconds=1))
                next_minute += timedelta(minutes=1)
            status = intraday_data.feed_status(self.db, "SPY", now=moment)
            out.append((moment, status["status"], status["execution_status"]))
            moment += timedelta(seconds=step_seconds)
        return out

    def test_strict_boundary_reproduces_the_minute_flip(self):
        start = datetime(2026, 10, 27, 19, 55, tzinfo=UTC)
        self.replay(through=start)
        with patch.object(settings, "intraday_publication_lag_seconds", 0):
            samples = self.statuses_between(start, start + timedelta(minutes=4))
        kinds = {status for _moment, status, _execution in samples}
        # The old rule demanded bar m at m+2min exactly; it lands at m+2min+1s.
        self.assertIn("ready", kinds)
        self.assertTrue(kinds & {"stale", "incomplete"}, kinds)

    def test_publication_lag_keeps_a_normally_lagging_feed_ready(self):
        start = datetime(2026, 10, 27, 19, 50, tzinfo=UTC)
        self.replay(through=start)
        with patch.object(settings, "intraday_publication_lag_seconds", 120):
            samples = self.statuses_between(start, start + timedelta(minutes=8))
        not_ready = [(moment.isoformat(), status, execution)
                     for moment, status, execution in samples if (status, execution) != ("ready", "ready")]
        self.assertEqual(not_ready, [])

    def test_observed_timestamp_is_ready_and_reports_the_expected_bar(self):
        arrival = datetime(2026, 10, 27, 19, 59, 1, tzinfo=UTC)
        # Just before the 19:57 bar arrives, 19:56 is the newest bar.
        self.replay(through=arrival - timedelta(seconds=2))
        before = intraday_data.feed_status(self.db, "SPY", now=arrival - timedelta(seconds=1))
        self.assertEqual((before["status"], before["execution_status"]), ("ready", "ready"))
        self.assertEqual(before["publication_lag_seconds"], 120)
        self.ingest(datetime(2026, 10, 27, 19, 57, tzinfo=UTC), arrival)
        after = intraday_data.feed_status(self.db, "SPY", now=arrival)
        self.assertEqual((after["status"], after["execution_status"]), ("ready", "ready"))

    def test_a_feed_that_stops_still_goes_stale(self):
        last_minute = datetime(2026, 10, 27, 19, 50, tzinfo=UTC)
        self.replay(through=last_minute + timedelta(minutes=3), last_ingested_minute=last_minute)
        # Within the allowance (bar closed 19:51, now 19:54:30) still ready.
        ok = intraday_data.feed_status(self.db, "SPY", now=datetime(2026, 10, 27, 19, 53, 30, tzinfo=UTC))
        self.assertEqual(ok["execution_status"], "ready")
        # Once 19:51 should have been published (closed 19:52 + 60s + 120s)
        # and is still missing, the feed is not ready.
        late = intraday_data.feed_status(self.db, "SPY", now=datetime(2026, 10, 27, 19, 55, 0, tzinfo=UTC))
        self.assertEqual(late["execution_status"], "stale")
        self.assertNotEqual(late["status"], "ready")
        self.assertIn(datetime(2026, 10, 27, 19, 51, tzinfo=UTC).isoformat(), late["missing_intervals"])
        much_later = intraday_data.feed_status(self.db, "SPY", now=datetime(2026, 10, 27, 20, 10, tzinfo=UTC))
        self.assertIn(much_later["execution_status"], {"stale", "unavailable"})

    def test_interior_gap_inside_the_published_window_is_still_incomplete(self):
        observed = datetime(2026, 10, 27, 14, 0, tzinfo=UTC)
        self.replay(through=observed)
        missing = datetime(2026, 10, 27, 13, 40, tzinfo=UTC)
        self.db.query(app.models.IntradayBar).filter_by(opened_at=missing).delete()
        self.db.commit()
        status = intraday_data.feed_status(self.db, "SPY", now=observed)
        self.assertEqual(status["status"], "incomplete")
        self.assertIn(missing.isoformat(), status["missing_intervals"])

    def test_bar_for_an_unfinished_minute_is_still_rejected(self):
        observed = datetime(2026, 10, 27, 15, 0, 30, tzinfo=UTC)
        self.replay(through=observed)
        # Plant a bar whose minute has not passed the late-trade allowance.
        row = self.db.query(app.models.IntradayBar).order_by(app.models.IntradayBar.opened_at.desc()).first()
        row.opened_at = datetime(2026, 10, 27, 14, 59, tzinfo=UTC)
        self.db.commit()
        status = intraday_data.feed_status(self.db, "SPY", now=observed)
        self.assertEqual(status["status"], "stale")
        self.assertIn("Future", status["unavailable_reason"])


if __name__ == "__main__":
    unittest.main()
