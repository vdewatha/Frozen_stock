import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.error import HTTPError

from app.core.config import settings
from app.db.base import Base
from app.models import IntradayBar
from app.services import intraday_data
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


UTC = timezone.utc


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def __iter__(self):
        return iter(())

    def read(self):
        return json.dumps(self.payload).encode()


class TradierMarketDataTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_timesales_request_uses_bearer_and_bounded_open_session_params(self):
        captured = {}

        def open_request(request, timeout):
            captured["url"] = request.full_url
            captured["authorization"] = request.get_header("Authorization")
            captured["accept"] = request.get_header("Accept")
            captured["timeout"] = timeout
            return _Response({"series": {"data": []}})

        start = datetime(2026, 9, 11, 14, 0, tzinfo=UTC)
        end = start + timedelta(minutes=5)
        with patch.object(settings, "tradier_market_data_api_key", type(settings.tradier_market_data_api_key)("market-key")), \
             patch("app.services.intraday_data.urlopen", side_effect=open_request):
            rows, duplicates, out_of_order = intraday_data._fetch_bars("SPY", start, end)

        self.assertEqual(rows, [])
        self.assertEqual((duplicates, out_of_order), (0, False))
        self.assertEqual(captured["authorization"], "Bearer market-key")
        self.assertEqual(captured["accept"], "application/json")
        self.assertEqual(captured["timeout"], 20)
        self.assertIn("/markets/timesales?", captured["url"])
        self.assertIn("symbol=SPY", captured["url"])
        self.assertIn("interval=1min", captured["url"])
        self.assertIn("session_filter=open", captured["url"])
        self.assertIn("start=2026-09-11+10%3A00", captured["url"])
        self.assertIn("end=2026-09-11+10%3A05", captured["url"])

    def test_tradier_timesales_normalizes_local_time_and_ohlcv(self):
        opened_at, values = intraday_data._parse_bar({
            "time": "2026-09-11 10:00:00",
            "timestamp": 1789120800000,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1000,
        })
        self.assertEqual(opened_at, datetime(2026, 9, 11, 14, 0, tzinfo=UTC))
        self.assertEqual(values["open"], 100)
        self.assertEqual(values["close"], 100.5)
        self.assertEqual(values["volume"], 1000)

    def test_production_market_token_is_required_without_alpaca_fallback(self):
        with patch.object(settings, "tradier_market_data_api_key", type(settings.tradier_market_data_api_key)("")), \
             patch.object(settings, "alpaca_api_key", type(settings.alpaca_api_key)("legacy-key")), \
             patch.object(settings, "alpaca_api_secret", type(settings.alpaca_api_secret)("legacy-secret")):
            with self.assertRaisesRegex(RuntimeError, "Tradier production market-data credentials"):
                intraday_data._request("/markets/timesales", {})

    def test_sandbox_url_cannot_be_labeled_as_trusted_sip(self):
        with patch.object(
            settings,
            "tradier_market_data_url",
            "https://sandbox.tradier.com/v1",
        ), patch.object(
            settings,
            "tradier_market_data_api_key",
            type(settings.tradier_market_data_api_key)("sandbox-key"),
        ):
            with self.assertRaisesRegex(RuntimeError, "immutable production endpoint"):
                intraday_data._request("/markets/timesales", {})

    def test_401_and_403_remain_distinct_provider_failures(self):
        for code, classification in ((401, "authentication"), (403, "entitlement")):
            denied = HTTPError("https://api.tradier.com", code, "denied", {}, None)
            with self.subTest(code=code), patch.object(
                settings, "tradier_market_data_api_key", type(settings.tradier_market_data_api_key)("market-key")
            ), patch("app.services.intraday_data.urlopen", side_effect=denied):
                with self.assertRaises(intraday_data.TradierProviderError) as raised:
                    intraday_data._request("/markets/timesales", {})
                self.assertEqual(raised.exception.failure_class, classification)

    def test_timesales_window_is_bounded(self):
        start = datetime(2026, 9, 11, 14, 0, tzinfo=UTC)
        with self.assertRaisesRegex(ValueError, "bounded one-hour"):
            intraday_data._fetch_bars("SPY", start, start + timedelta(hours=1, minutes=1))

    def test_tradier_rows_coexist_with_historical_alpaca_rows(self):
        opened = datetime(2026, 9, 11, 14, 0, tzinfo=UTC)
        self.db.add(IntradayBar(
            symbol="SPY", timeframe="1m", opened_at=opened,
            open=100, high=101, low=99, close=100, volume=1,
            provider="alpaca", feed_class="sip",
            exchange_timestamp=opened, ingested_at=opened,
        ))
        self.db.commit()
        intraday_data.upsert_intraday_bars(self.db, "SPY", [{
            "time": "2026-09-11 10:00:00",
            "open": 100.1, "high": 101.1, "low": 99.1, "close": 100.6, "volume": 2,
        }], ingested_at=opened + timedelta(minutes=3))
        rows = self.db.query(IntradayBar).order_by(IntradayBar.provider).all()
        self.assertEqual([row.provider for row in rows], ["alpaca", "tradier"])

    def test_preflight_preserves_ingestion_repair_metadata(self):
        now = datetime(2026, 9, 11, 15, 0, tzinfo=UTC)
        missing = "2026-09-10T13:30:00+00:00"
        ingestion = {
            "results": [{
                "symbol": "SPY",
                "status": "incomplete",
                "failure_class": "incomplete_data",
                "unavailable_reason": "Missing completed regular-session intervals",
                "missing_intervals": [missing],
                "deferred_window": {
                    "start": missing,
                    "end": "2026-09-10T14:30:00+00:00",
                },
                "oldest_unresolved_interval": missing,
                "rows_imported": 1,
            }],
        }
        feed = {
            "symbol": "SPY",
            "status": "ready",
            "exchange_timestamp": now - timedelta(minutes=2),
            "ingestion_timestamp": now,
            "checked_at": now,
            "missing_intervals": [],
        }
        with patch.object(
            settings,
            "tradier_market_data_api_key",
            type(settings.tradier_market_data_api_key)("market-key"),
        ), patch(
            "app.services.intraday_data.ingest_intraday",
            return_value=ingestion,
        ), patch(
            "app.services.intraday_data.feed_status",
            return_value=feed,
        ):
            result = intraday_data.preflight_intraday(
                self.db,
                ["SPY"],
                now=now,
            )
        self.assertTrue(result["ready"])
        self.assertEqual(result["results"][0]["status"], "ready")
        self.assertEqual(
            result["results"][0]["historical_missing_intervals"],
            [missing],
        )
        self.assertEqual(
            result["results"][0]["historical_repair_status"],
            "incomplete",
        )
        self.assertEqual(
            result["results"][0]["historical_oldest_unresolved_interval"],
            missing,
        )


if __name__ == "__main__":
    unittest.main()
