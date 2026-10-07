import json
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch
from urllib.error import HTTPError

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.base import Base
from app.models import AuditLog, CorporateAction, IntradayBar
from app.services import intraday_data
from app.services.intraday_data import _aware_utc
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

    def test_scheduled_post_close_repair_imports_final_bars_without_trading_readiness(self):
        observed = datetime(2026, 9, 29, 20, 2, tzinfo=UTC)
        calls = []

        def fetch(symbol, start, end):
            calls.append((start, end))
            return [bar(start + timedelta(minutes=i)) for i in range(int((end-start).total_seconds()/60))], 0, False

        with patch.object(settings, "active_market_data_provider", "tradier"), patch.object(
            settings, "tradier_market_data_api_key", type(settings.tradier_market_data_api_key)("key")
        ), patch.object(intraday_data, "_fetch_bars", side_effect=fetch):
            result = intraday_data.collect_scheduled_intraday(self.db, ["SPY"], now=observed)
            preflight = intraday_data.preflight_intraday(self.db, ["SPY"], now=observed)
        self.assertEqual(result["collection_scope"], "post_close_repair")
        self.assertFalse(result["ready"])
        self.assertFalse(result["execution_eligible"])
        self.assertFalse(preflight["ready"])
        self.assertEqual(preflight["failure_class"], "timing")
        self.assertLessEqual(len(calls), 2)
        for minute in (58, 59):
            self.assertIsNotNone(self.db.query(IntradayBar).filter_by(
                symbol="SPY", provider="tradier", opened_at=datetime(2026, 9, 29, 19, minute, tzinfo=UTC)
            ).one_or_none())
        audit = self.db.query(AuditLog).filter_by(action="sip_post_close_collection").one()
        self.assertFalse(audit.payload["ready"])

    def test_post_close_window_respects_early_close_and_does_not_run_overnight(self):
        early_close = datetime(2026, 11, 27, 18, 2, tzinfo=UTC)
        with patch.object(settings, "active_market_data_provider", "tradier"), patch.object(
            settings, "tradier_market_data_api_key", type(settings.tradier_market_data_api_key)("key")
        ), patch.object(intraday_data, "ingest_intraday", return_value={"results": [{"symbol": "SPY", "status": "complete"}]}) as ingest:
            result = intraday_data.collect_scheduled_intraday(self.db, ["SPY"], now=early_close)
            self.assertEqual(result["status"], "complete")
            self.assertFalse(result["ready"])
            ingest.assert_called_once()
            for now in [datetime(2026, 11, 27, 19, 0, tzinfo=UTC), datetime(2026, 11, 28, 18, 2, tzinfo=UTC)]:
                result = intraday_data.collect_scheduled_intraday(self.db, ["SPY"], now=now)
                self.assertFalse(result["ready"])
            ingest.assert_called_once()

    def test_regular_session_scheduler_keeps_authenticated_preflight(self):
        observed = datetime(2026, 9, 29, 18, 0, tzinfo=UTC)
        with patch.object(intraday_data, "preflight_intraday", return_value={"ready": True}) as preflight:
            self.assertEqual(intraday_data.collect_scheduled_intraday(self.db, ["SPY"], now=observed), {"ready": True})
        preflight.assert_called_once_with(self.db, ["SPY"], now=observed)

    def test_post_close_provider_failures_do_not_become_success(self):
        observed = datetime(2026, 9, 29, 20, 2, tzinfo=UTC)
        with patch.object(settings, "active_market_data_provider", "tradier"), patch.object(
            settings, "tradier_market_data_api_key", type(settings.tradier_market_data_api_key)("key")
        ), patch.object(intraday_data, "_fetch_bars", side_effect=intraday_data.TradierProviderError("denied", "authentication")):
            result = intraday_data.collect_scheduled_intraday(self.db, ["SPY"], now=observed)
        self.assertEqual(result["status"], "incomplete")
        self.assertFalse(result["ready"])
        self.assertEqual(result["results"][0]["failure_class"], "authentication")

    def test_post_close_repair_requires_configured_production_provider(self):
        observed = datetime(2026, 9, 29, 20, 2, tzinfo=UTC)
        for provider, key in [("other", "key"), ("tradier", "")]:
            with self.subTest(provider=provider), patch.object(settings, "active_market_data_provider", provider), patch.object(
                settings, "tradier_market_data_api_key", type(settings.tradier_market_data_api_key)(key)
            ), patch.object(intraday_data, "ingest_intraday") as ingest:
                result = intraday_data.collect_scheduled_intraday(self.db, ["SPY"], now=observed)
            ingest.assert_not_called()
            self.assertEqual(result["failure_class"], "configuration")
            self.assertFalse(result["ready"])

    def test_alpaca_iex_provider_uses_bounded_authenticated_fetch(self):
        start = datetime(2026, 9, 29, 14, 30, tzinfo=UTC)
        end = start + timedelta(minutes=3)
        rows = [bar(start + timedelta(minutes=offset)) for offset in range(3)]
        with patch.object(settings, "active_market_data_provider", "alpaca_iex"), patch.object(
            intraday_data, "_fetch_alpaca_iex_bars", return_value=rows
        ) as fetch:
            result = intraday_data._fetch_bars("SPY", start, end)
        fetch.assert_called_once_with("SPY", start, end)
        self.assertEqual(result[0], rows)
        self.assertEqual(result[1:], (0, False))

    def test_close_boundary_still_waits_for_late_trade_allowance(self):
        observed = datetime(2026, 9, 29, 20, 0, tzinfo=UTC)
        intraday_data.upsert_intraday_bars(self.db, "SPY", [bar(observed - timedelta(minutes=1))], ingested_at=observed)
        self.assertEqual(self.db.query(IntradayBar).count(), 0)
        intraday_data.upsert_intraday_bars(self.db, "SPY", [bar(observed - timedelta(minutes=1))], ingested_at=observed + timedelta(minutes=1))
        self.assertEqual(self.db.query(IntradayBar).count(), 1)

    def test_future_ingestion_cannot_prove_current_entitlement(self):
        observed = datetime(2026, 9, 29, 18, 0, tzinfo=UTC)
        intraday_data.upsert_intraday_bars(self.db, "SPY", [bar(observed - timedelta(minutes=2))],
                                         ingested_at=observed + timedelta(minutes=1))
        with patch.object(settings, "tradier_market_data_api_key", type(settings.tradier_market_data_api_key)("key")):
            result = intraday_data.feed_status(self.db, "SPY", now=observed)
        self.assertEqual(result["entitlement_state"], "unverified")

    def test_authenticated_refresh_updates_verification_without_rewriting_observation(self):
        first_observed = datetime(2026, 9, 29, 18, 0, tzinfo=UTC)
        refreshed_at = first_observed + timedelta(minutes=4)
        with patch.object(settings, "active_market_data_provider", "alpaca_iex"), patch.object(
            intraday_data, "MARKET_DATA_PROVIDER", "alpaca_iex"
        ), patch.object(
            intraday_data, "MARKET_DATA_FEED_CLASS", "iex"
        ), patch.object(
            settings, "paper_alpaca_api_key", type(settings.paper_alpaca_api_key)("key")
        ), patch.object(
            settings, "paper_alpaca_api_secret", type(settings.paper_alpaca_api_secret)("secret")
        ):
            intraday_data.upsert_intraday_bars(
                self.db, "SPY", [bar(first_observed - timedelta(minutes=2))],
                ingested_at=first_observed, provider="alpaca_iex", feed_class="iex",
            )
            intraday_data.upsert_intraday_bars(
                self.db, "SPY", [bar(first_observed - timedelta(minutes=2))],
                ingested_at=refreshed_at, provider="alpaca_iex", feed_class="iex",
            )
            row = self.db.query(IntradayBar).one()
            result = intraday_data.feed_status(self.db, "SPY", now=refreshed_at)
        self.assertEqual(_aware_utc(row.ingested_at), first_observed)
        self.assertEqual(_aware_utc(row.last_verified_at), refreshed_at)
        self.assertEqual(result["entitlement_state"], "verified")

    def test_next_regular_session_open_skips_weekends_and_holidays(self):
        before_open = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
        after_close = datetime(2026, 9, 11, 21, 0, tzinfo=UTC)
        thanksgiving = datetime(2026, 11, 26, 15, 0, tzinfo=UTC)

        self.assertEqual(
            intraday_data.next_regular_session_open(before_open),
            datetime(2026, 9, 11, 13, 30, tzinfo=UTC),
        )
        self.assertEqual(
            intraday_data.next_regular_session_open(after_close),
            datetime(2026, 9, 14, 13, 30, tzinfo=UTC),
        )
        self.assertEqual(
            intraday_data.next_regular_session_open(thanksgiving),
            datetime(2026, 11, 27, 14, 30, tzinfo=UTC),
        )

    def test_next_regular_session_open_preserves_dst_and_observed_holiday_opens(self):
        cases = (
            (
                "spring daylight-saving transition",
                datetime(2026, 3, 8, 15, 0, tzinfo=UTC),
                datetime(2026, 3, 9, 13, 30, tzinfo=UTC),
            ),
            (
                "fall daylight-saving transition",
                datetime(2026, 11, 1, 15, 0, tzinfo=UTC),
                datetime(2026, 11, 2, 14, 30, tzinfo=UTC),
            ),
            (
                "observed New Year's Day",
                datetime(2023, 1, 2, 15, 0, tzinfo=UTC),
                datetime(2023, 1, 3, 14, 30, tzinfo=UTC),
            ),
            (
                "observed Juneteenth",
                datetime(2022, 6, 20, 15, 0, tzinfo=UTC),
                datetime(2022, 6, 21, 13, 30, tzinfo=UTC),
            ),
            (
                "observed Christmas",
                datetime(2022, 12, 26, 15, 0, tzinfo=UTC),
                datetime(2022, 12, 27, 14, 30, tzinfo=UTC),
            ),
        )

        for label, observed_at, expected_open in cases:
            with self.subTest(label=label):
                actual_open = intraday_data.next_regular_session_open(observed_at)
                self.assertEqual(actual_open.isoformat(), expected_open.isoformat())
                self.assertEqual(
                    actual_open.astimezone(intraday_data.NY).time(),
                    datetime.strptime("09:30", "%H:%M").time(),
                )

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
        with patch.object(
            settings,
            "tradier_market_data_api_key",
            type(settings.tradier_market_data_api_key)("key"),
        ):
            status = intraday_data.feed_status(self.db, "SPY", now=observed)
            self.assertEqual(status["status"], "incomplete")
            self.assertIn((session_open + timedelta(minutes=1)).isoformat(), status["missing_intervals"])
            self.assertEqual(
                status["deferred_window"]["end"],
                (observed - timedelta(minutes=1)).isoformat(),
            )
            with self.assertRaises(UntrustedMarketData):
                validate_intraday_readiness(self.db, "SPY", now=observed)

    def test_market_closed_requires_complete_previous_session(self):
        friday_open = datetime(2026, 9, 11, 13, 30, tzinfo=UTC)
        bars = [bar(friday_open + timedelta(minutes=index)) for index in range(390)]
        saturday = datetime(2026, 9, 12, 16, 0, tzinfo=UTC)
        intraday_data.upsert_intraday_bars(self.db, "SPY", bars, ingested_at=saturday)
        with patch.object(
            settings,
            "tradier_market_data_api_key",
            type(settings.tradier_market_data_api_key)("key"),
        ):
            status = intraday_data.feed_status(self.db, "SPY", now=saturday)
        self.assertEqual(status["status"], "market_closed")
        self.assertEqual(status["missing_intervals"], [])

    def test_completed_bar_boundary_is_strict(self):
        opened = datetime(2026, 9, 11, 14, 0, tzinfo=UTC)
        # The bar is not accepted until the full minute and late-trade
        # allowance have elapsed.
        early = intraday_data.upsert_intraday_bars(
            self.db, "SPY", [bar(opened)], ingested_at=opened + timedelta(seconds=119)
        )
        self.assertEqual(early["rows_imported"], 0)
        result = intraday_data.upsert_intraday_bars(
            self.db, "SPY", [bar(opened)], ingested_at=opened + timedelta(seconds=120)
        )
        self.assertEqual(result["rows_imported"], 1)

    def test_unconfigured_and_entitlement_errors(self):
        with patch.object(
            settings,
            "tradier_market_data_api_key",
            type(settings.tradier_market_data_api_key)(""),
        ):
            with self.assertRaisesRegex(RuntimeError, "workspace secrets"):
                intraday_data._request("/markets/timesales", {})
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
        with patch.object(
            settings,
            "tradier_market_data_api_key",
            type(settings.tradier_market_data_api_key)("key"),
        ), patch("app.services.intraday_data.urlopen", side_effect=denied):
            with self.assertRaisesRegex(RuntimeError, "entitlement denied"):
                intraday_data._request("/markets/timesales", {})

    def test_authentication_and_entitlement_failures_are_distinct(self):
        for code, message, expected_class in (
            (401, "authentication denied", "authentication"),
            (403, "entitlement denied", "entitlement"),
        ):
            denied = HTTPError("https://example", code, "denied", {}, None)
            with self.subTest(code=code), patch.object(
                settings, "tradier_market_data_api_key", type(settings.tradier_market_data_api_key)("key")
            ), patch("app.services.intraday_data.urlopen", side_effect=denied):
                with self.assertRaisesRegex(RuntimeError, message) as raised:
                    intraday_data._request("/markets/timesales", {})
                self.assertEqual(
                    intraday_data._provider_failure(raised.exception)[0],
                    expected_class,
                )

    def test_future_bar_is_not_current_session_ready(self):
        now = datetime(2026, 9, 11, 14, 0, tzinfo=UTC)
        future = IntradayBar(
            symbol="SPY",
            timeframe="1m",
            opened_at=now,
            open=100,
            high=101,
            low=99,
            close=100,
            volume=100,
            provider=intraday_data.MARKET_DATA_PROVIDER,
            feed_class="sip",
            exchange_timestamp=now,
            ingested_at=now,
        )
        self.db.add(future)
        self.db.commit()
        with patch.object(
            settings,
            "tradier_market_data_api_key",
            type(settings.tradier_market_data_api_key)("key"),
        ):
            status = intraday_data.feed_status(self.db, "SPY", now=now)
        self.assertEqual(status["status"], "stale")
        self.assertIn("Future", status["unavailable_reason"])

    def test_successful_four_symbol_sip_preflight_is_complete_redacted_and_byte_stable(self):
        now = datetime(2026, 9, 11, 15, 0, tzinfo=UTC)
        symbols = ("AAPL", "MSFT", "QQQ", "SPY")
        session_open = datetime(2026, 9, 11, 13, 30, tzinfo=UTC)
        completed_bars = [
            bar(session_open + timedelta(minutes=offset))
            for offset in range(89)
        ]
        for symbol in symbols:
            result = intraday_data.upsert_intraday_bars(
                self.db,
                symbol,
                completed_bars,
                ingested_at=now,
            )
            self.assertEqual(result["rows_imported"], len(completed_bars))
        self.assertEqual(self.db.query(IntradayBar).count(), len(symbols) * len(completed_bars))

        imported = {
            "status": "complete",
            "results": [
                {"symbol": symbol, "status": "complete", "rows_imported": len(completed_bars)}
                for symbol in symbols
            ],
        }
        with patch.object(settings, "active_market_data_provider", "tradier"), patch.object(
            settings,
            "tradier_market_data_api_key",
            type(settings.tradier_market_data_api_key)("sip-test-key"),
        ), patch("app.services.intraday_data.ingest_intraday", return_value=imported):
            first = intraday_data.preflight_intraday(self.db, list(symbols), now=now)
            second = intraday_data.preflight_intraday(self.db, list(symbols), now=now)

        self.assertTrue(first["ready"])
        self.assertEqual(first["status"], "ready")
        self.assertEqual(first["symbols"], list(symbols))
        for item in first["results"]:
            with self.subTest(symbol=item["symbol"]):
                self.assertEqual(item["status"], "ready")
                self.assertEqual(item["entitlement_state"], "verified")
                self.assertEqual(item["latency_seconds"], 60.0)
                self.assertEqual(item["missing_intervals"], [])

        audits = (
            self.db.query(AuditLog)
            .filter_by(action="sip_preflight")
            .order_by(AuditLog.id)
            .all()
        )
        self.assertEqual(len(audits), 2)
        first_audit_bytes = json.dumps(
            audits[0].payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        second_audit_bytes = json.dumps(
            audits[1].payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.assertEqual(first_audit_bytes, second_audit_bytes)
        audit_text = first_audit_bytes.decode("utf-8").lower()
        self.assertNotIn("sip-test-key", audit_text)
        self.assertNotIn("api_key", audit_text)
        self.assertNotIn("secret", audit_text)
        self.assertNotIn("raw_payload", audit_text)
        self.assertNotIn("account", audit_text)

    def test_four_symbol_preflight_is_bounded_to_active_regular_session(self):
        now = datetime(2026, 9, 11, 15, 0, tzinfo=UTC)
        statuses = {
            symbol: {
                "symbol": symbol,
                "status": "ready",
                "exchange_timestamp": now - timedelta(minutes=2),
                "ingestion_timestamp": now,
                "latency_seconds": 60.0,
                "missing_intervals": [],
                "unavailable_reason": None,
            }
            for symbol in ("AAPL", "MSFT", "QQQ", "SPY")
        }
        imported = {
            "status": "complete",
            "results": [
                {"symbol": symbol, "status": "complete", "rows_imported": 1}
                for symbol in statuses
            ],
        }
        with patch.object(
            settings,
            "tradier_market_data_api_key",
            type(settings.tradier_market_data_api_key)("key"),
        ), patch("app.services.intraday_data.ingest_intraday", return_value=imported), patch(
            "app.services.intraday_data.feed_status", side_effect=lambda db, symbol, now: statuses[symbol]
        ):
            result = intraday_data.preflight_intraday(self.db, now=now)
        self.assertTrue(result["ready"])
        self.assertEqual(result["status"], "ready")
        self.assertIsNone(result["next_regular_session_open"])
        self.assertIsNone(result["next_regular_session_gap"])
        self.assertEqual(result["symbols"], ["AAPL", "MSFT", "QQQ", "SPY"])
        self.assertEqual({row["symbol"] for row in result["results"]}, set(statuses))
        audit = self.db.query(AuditLog).filter_by(action="sip_preflight").one()
        self.assertEqual(audit.payload["status"], "ready")
        self.assertEqual(audit.payload["symbols"], ["AAPL", "MSFT", "QQQ", "SPY"])
        self.assertNotIn("api_key", json.dumps(audit.payload).lower())
        self.assertNotIn("account", json.dumps(audit.payload).lower())

    def test_preflight_exposes_next_open_and_gap_when_session_is_closed(self):
        observed_at = datetime(2026, 9, 11, 21, 0, tzinfo=UTC)
        with patch.object(settings, "active_market_data_provider", "tradier"), patch.object(
            settings,
            "tradier_market_data_api_key",
            type(settings.tradier_market_data_api_key)("key"),
        ):
            result = intraday_data.preflight_intraday(self.db, now=observed_at)

        self.assertFalse(result["ready"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(
            result["next_regular_session_open"],
            "2026-09-14T13:30:00+00:00",
        )
        self.assertEqual(result["next_regular_session_gap"], "weekend")
        self.assertTrue(all(row["status"] == "out_of_session" for row in result["results"]))

    def test_session_repair_uses_bounded_windows_and_stays_fail_closed(self):
        calls = []

        def fetch(symbol, start, end):
            calls.append((symbol, start, end))
            return [], 0, False

        observed = datetime(2026, 9, 11, 20, 0, tzinfo=UTC)
        with patch.object(intraday_data, "_fetch_bars", side_effect=fetch):
            result = intraday_data.ingest_intraday(self.db, ["SPY"], now=observed)

        self.assertTrue(calls)
        self.assertTrue(
            all(end - start <= intraday_data.INTRADAY_BACKFILL_WINDOW for _, start, end in calls)
        )
        symbol_result = result["results"][0]
        self.assertEqual(symbol_result["status"], "incomplete")
        self.assertTrue(symbol_result["missing_intervals"])
        self.assertEqual(
            symbol_result["deferred_window"],
            {
                "start": "2026-09-10T13:30:00+00:00",
                "end": "2026-09-10T14:30:00+00:00",
            },
        )
        self.assertEqual(
            symbol_result["oldest_unresolved_interval"],
            "2026-09-10T13:30:00+00:00",
        )

    def test_interrupted_poll_resumes_bounded_repair_on_next_invocation(self):
        observed = datetime(2026, 9, 11, 15, 0, tzinfo=UTC)
        calls = []
        interrupt_next_slice = True

        class WorkerLost(BaseException):
            pass

        def fetch(symbol, start, end):
            nonlocal interrupt_next_slice
            calls.append((symbol, start, end))
            if interrupt_next_slice and len(calls) == 2:
                interrupt_next_slice = False
                raise WorkerLost("worker interrupted during the next repair slice")
            return [
                bar(start + timedelta(minutes=offset))
                for offset in range(int((end - start).total_seconds() // 60))
            ], 0, False

        with patch.object(intraday_data, "_fetch_bars", side_effect=fetch):
            with self.assertRaises(WorkerLost):
                intraday_data.ingest_intraday(self.db, ["SPY"], now=observed)

        # The first bounded slice committed before the worker disappeared, but
        # the unresolved previous session must not be treated as ready.
        self.assertEqual(self.db.query(IntradayBar).count(), 60)

        calls.clear()
        with patch.object(intraday_data, "_fetch_bars", side_effect=fetch):
            result = intraday_data.ingest_intraday(self.db, ["SPY"], now=observed)

        self.assertEqual(len(calls), 2)
        self.assertTrue(
            all(end - start <= intraday_data.INTRADAY_BACKFILL_WINDOW for _, start, end in calls)
        )
        symbol_result = result["results"][0]
        self.assertEqual(symbol_result["status"], "incomplete")
        self.assertEqual(
            symbol_result["oldest_unresolved_interval"],
            "2026-09-10T13:30:00+00:00",
        )
        self.assertEqual(
            symbol_result["deferred_window"],
            {
                "start": "2026-09-10T13:30:00+00:00",
                "end": "2026-09-10T14:30:00+00:00",
            },
        )
        self.assertTrue(symbol_result["missing_intervals"])

    def test_unsupported_symbol_is_structured_untrusted_data(self):
        with self.assertRaisesRegex(UntrustedMarketData, "must be one of"):
            validate_intraday_readiness(self.db, "TSLA")

    def test_rate_limit_retries_are_bounded(self):
        limited = HTTPError("https://example", 429, "limited", {}, None)
        with patch.object(
            settings,
            "tradier_market_data_api_key",
            type(settings.tradier_market_data_api_key)("key"),
        ), patch("app.services.intraday_data.urlopen", side_effect=limited) as request, patch(
            "app.services.intraday_data.time.sleep"
        ):
            with self.assertRaisesRegex(RuntimeError, "after 3 attempts"):
                intraday_data._request("/markets/timesales", {})
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
