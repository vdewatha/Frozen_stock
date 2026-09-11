import json
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch
from urllib.error import HTTPError

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.base import Base
from app.models import CorporateAction, IntradayBar
from app.services import intraday_data
from app.services.trusted_data import UntrustedMarketData, validate_intraday_readiness

UTC = timezone.utc


def bar(opened_at: datetime, close: float = 100.5) -> dict:
    return {
        "t": opened_at.isoformat().replace("+00:00", "Z"),
        "o": 100,
        "h": 101,
        "l": 99,
        "c": close,
        "v": 1000,
    }


class IntradayDataTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_nyse_holidays_and_early_close(self):
        self.assertFalse(intraday_data.is_nyse_session(date(2026, 7, 3)))
        self.assertFalse(intraday_data.is_nyse_session(date(2026, 11, 26)))
        self.assertTrue(intraday_data.is_nyse_session(date(2026, 11, 11)))
        bounds = intraday_data.session_bounds(date(2026, 11, 27))
        self.assertEqual(bounds[1].astimezone(intraday_data.NY).time(), datetime.strptime("13:00", "%H:%M").time())

    def test_completed_deduplicated_and_out_of_order_bars(self):
        observed = datetime(2026, 9, 11, 15, 0, tzinfo=UTC)
        first = datetime(2026, 9, 11, 14, 0, tzinfo=UTC)
        result = intraday_data.upsert_intraday_bars(
            self.db,
            "SPY",
            [bar(first + timedelta(minutes=1)), bar(first), bar(first, 100.75)],
            ingested_at=observed,
        )
        self.assertEqual(result["rows_imported"], 2)
        self.assertEqual(result["duplicate_bars"], 1)
        self.assertTrue(result["out_of_order"])
        self.assertEqual(self.db.query(IntradayBar).count(), 2)
        self.assertEqual(float(self.db.query(IntradayBar).filter_by(opened_at=first).one().close), 100.75)
        # A still-correctable bar is withheld.
        intraday_data.upsert_intraday_bars(
            self.db, "SPY", [bar(observed - timedelta(seconds=90))], ingested_at=observed
        )
        self.assertEqual(self.db.query(IntradayBar).count(), 2)

    def test_gap_and_stale_readiness_fail_closed(self):
        observed = datetime(2026, 9, 11, 13, 34, tzinfo=UTC)
        session_open = datetime(2026, 9, 11, 13, 30, tzinfo=UTC)
        intraday_data.upsert_intraday_bars(
            self.db, "SPY", [bar(session_open)], ingested_at=observed
        )
        with patch.object(settings, "alpaca_api_key", type(settings.alpaca_api_key)("key")), patch.object(
            settings, "alpaca_api_secret", type(settings.alpaca_api_secret)("secret")
        ):
            status = intraday_data.feed_status(self.db, "SPY", now=observed)
            self.assertEqual(status["status"], "incomplete")
            self.assertIn((session_open + timedelta(minutes=1)).isoformat(), status["missing_intervals"])
            with self.assertRaises(UntrustedMarketData):
                validate_intraday_readiness(self.db, "SPY", now=observed)

    def test_market_closed_requires_complete_previous_session(self):
        friday_open = datetime(2026, 9, 11, 13, 30, tzinfo=UTC)
        bars = [bar(friday_open + timedelta(minutes=index)) for index in range(390)]
        saturday = datetime(2026, 9, 12, 16, 0, tzinfo=UTC)
        intraday_data.upsert_intraday_bars(self.db, "SPY", bars, ingested_at=saturday)
        with patch.object(settings, "alpaca_api_key", type(settings.alpaca_api_key)("key")), patch.object(
            settings, "alpaca_api_secret", type(settings.alpaca_api_secret)("secret")
        ):
            status = intraday_data.feed_status(self.db, "SPY", now=saturday)
        self.assertEqual(status["status"], "market_closed")
        self.assertEqual(status["missing_intervals"], [])

    def test_unconfigured_and_entitlement_errors(self):
        with patch.object(settings, "alpaca_api_key", type(settings.alpaca_api_key)("")), patch.object(
            settings, "alpaca_api_secret", type(settings.alpaca_api_secret)("")
        ):
            with self.assertRaisesRegex(RuntimeError, "workspace secrets"):
                intraday_data._request("/v2/stocks/bars", {})
            status = intraday_data.feed_status(
                self.db, "SPY", now=datetime(2026, 9, 11, 15, 0, tzinfo=UTC)
            )
            self.assertEqual(status["status"], "unavailable")
            self.assertIn("workspace secrets", status["unavailable_reason"])
            session_open = datetime(2026, 9, 11, 13, 30, tzinfo=UTC)
            intraday_data.upsert_intraday_bars(
                self.db, "SPY", [bar(session_open)], ingested_at=datetime(2026, 9, 11, 15, 0, tzinfo=UTC)
            )
            cached_status = intraday_data.feed_status(
                self.db, "SPY", now=datetime(2026, 9, 11, 15, 0, tzinfo=UTC)
            )
            self.assertEqual(cached_status["status"], "unavailable")
            self.assertEqual(cached_status["entitlement_state"], "not_configured")
        denied = HTTPError("https://example", 403, "denied", {}, None)
        with patch.object(settings, "alpaca_api_key", type(settings.alpaca_api_key)("key")), patch.object(
            settings, "alpaca_api_secret", type(settings.alpaca_api_secret)("secret")
        ), patch("app.services.intraday_data.urlopen", side_effect=denied):
            with self.assertRaisesRegex(RuntimeError, "entitlement denied"):
                intraday_data._request("/v2/stocks/bars", {})

    def test_unsupported_symbol_is_structured_untrusted_data(self):
        with self.assertRaisesRegex(UntrustedMarketData, "must be one of"):
            validate_intraday_readiness(self.db, "TSLA")

    def test_rate_limit_retries_are_bounded(self):
        limited = HTTPError("https://example", 429, "limited", {}, None)
        with patch.object(settings, "alpaca_api_key", type(settings.alpaca_api_key)("key")), patch.object(
            settings, "alpaca_api_secret", type(settings.alpaca_api_secret)("secret")
        ), patch("app.services.intraday_data.urlopen", side_effect=limited) as request, patch(
            "app.services.intraday_data.time.sleep"
        ):
            with self.assertRaisesRegex(RuntimeError, "after 3 attempts"):
                intraday_data._request("/v2/stocks/bars", {})
        self.assertEqual(request.call_count, 3)

    def test_corporate_actions_are_idempotent_and_intraday_stays_raw(self):
        action = {
            "type": "forward_split",
            "ex_date": "2026-08-01",
            "new_rate": "4",
            "old_rate": "1",
        }
        self.assertEqual(intraday_data.upsert_corporate_actions(self.db, "AAPL", [action]), 1)
        self.assertEqual(intraday_data.upsert_corporate_actions(self.db, "AAPL", [action]), 0)
        self.assertEqual(self.db.query(CorporateAction).count(), 1)


if __name__ == "__main__":
    unittest.main()