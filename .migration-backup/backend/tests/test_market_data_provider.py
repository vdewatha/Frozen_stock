import io
import json
import os
import time
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

import pandas as pd

from app.services import market_data


def payload():
    return {"chart": {"error": None, "result": [{"meta": {"symbol": "BTC-USD", "dataGranularity": "1d"},
        "timestamp": [1704153600], "indicators": {"quote": [{"open": [100], "high": [110], "low": [90], "close": [105], "volume": [20]}]}}]}}


class ProviderTests(unittest.TestCase):
    def fetch(self, value):
        with patch.object(market_data, "urlopen", return_value=io.StringIO(json.dumps(value))):
            return market_data.fetch_yahoo_chart_prices("BTC-USD")

    def test_daily_date_is_utc_independent_of_process_timezone(self):
        for zone in ("UTC", "America/New_York", "Pacific/Honolulu"):
            try:
                with patch.dict(os.environ, {"TZ": zone}):
                    time.tzset()
                    frame = self.fetch(payload())
                    self.assertEqual(frame.iloc[0].date, date(2024, 1, 2))
                    self.assertEqual(frame.iloc[0].adjusted_close, 105)
            finally:
                time.tzset()

    def test_missing_or_invalid_ohlcv_never_filled(self):
        for field in ("open", "high", "low", "close", "volume"):
            for invalid in (None, float("nan"), float("inf"), True):
                value = payload()
                value["chart"]["result"][0]["indicators"]["quote"][0][field] = [invalid]
                self.assertTrue(self.fetch(value).empty, (field, invalid))
        value = payload()
        del value["chart"]["result"][0]["indicators"]["quote"][0]["volume"]
        self.assertTrue(self.fetch(value).empty)

    def test_wrong_symbol_interval_or_malformed_response_rejected(self):
        for field, bad in [("symbol", "ETH-USD"), ("dataGranularity", "1h")]:
            value = payload(); value["chart"]["result"][0]["meta"][field] = bad
            self.assertTrue(self.fetch(value).empty)
        for value in ({}, [], {"chart": {"result": [None]}}, {"chart": {"result": "bad"}}):
            self.assertTrue(self.fetch(value).empty)

    def test_symbol_cannot_modify_url(self):
        for symbol in ("BTC?x=1", "../SPY", "BTC/USD", "SPY#fragment", "SPY%2f", "SPY&x=1"):
            with patch.object(market_data, "urlopen") as network, self.assertRaises(ValueError):
                market_data.fetch_yahoo_chart_prices(symbol)
            network.assert_not_called()
        value = payload(); value["chart"]["result"][0]["meta"]["symbol"] = "^GSPC"
        with patch.object(market_data, "urlopen", return_value=io.StringIO(json.dumps(value))) as network:
            market_data.fetch_yahoo_chart_prices("^GSPC")
        self.assertIn("/chart/%5EGSPC?", network.call_args.args[0].full_url)

    def test_yfinance_missing_required_data_and_wrong_ticker_rejected(self):
        frame = pd.DataFrame({"Open": [100], "High": [110], "Low": [90], "Close": [105], "Volume": [20]}, index=pd.DatetimeIndex(["2024-01-02"], name="Date"))
        with patch.object(market_data.yf, "download", return_value=frame):
            self.assertEqual(market_data.fetch_yfinance_prices("BTC-USD").iloc[0].adjusted_close, 105)
        for missing in frame.columns:
            with patch.object(market_data.yf, "download", return_value=frame.drop(columns=missing)):
                self.assertTrue(market_data.fetch_yfinance_prices("BTC-USD").empty)
        frame.columns = pd.MultiIndex.from_product([frame.columns, ["ETH-USD"]])
        with patch.object(market_data.yf, "download", return_value=frame):
            self.assertTrue(market_data.fetch_yfinance_prices("BTC-USD").empty)

    def test_current_and_future_daily_bars_are_not_available(self):
        frame = pd.DataFrame({"date": ["2026-09-03", "2026-09-04", "2026-09-05"],
                              "open": 100, "high": 110, "low": 90, "close": 105, "volume": 20})
        observed = market_data._validated_provider_frame(frame, as_of=datetime(2026, 9, 4, 23, 59, tzinfo=timezone.utc))
        self.assertEqual(list(observed.date), [date(2026, 9, 3)])
        with self.assertRaises(ValueError):
            market_data._validated_provider_frame(frame, as_of=datetime(2026, 9, 4))
