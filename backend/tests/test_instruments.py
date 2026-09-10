from datetime import datetime, timedelta, timezone
import unittest

from app.services.instruments import AssetClass, Candle, Instrument, Timeframe


class InstrumentTests(unittest.TestCase):
    def test_identity_distinguishes_venue_quote_and_asset_class(self):
        instruments = [Instrument(AssetClass.CRYPTO_SPOT, "KRAKEN", "BTC", "USD"),
                       Instrument(AssetClass.CRYPTO_SPOT, "KRAKEN", "BTC", "USDT"),
                       Instrument(AssetClass.CRYPTO_SPOT, "BINANCE", "BTC", "USDT"),
                       Instrument(AssetClass.US_EQUITY, "XNAS", "SPY", "USD"),
                       Instrument(AssetClass.US_ETF, "XNAS", "SPY", "USD")]
        self.assertEqual(len({i.instrument_id for i in instruments}), 5)
        self.assertEqual(instruments[0].provider_symbol, "BTC/USD")

    def test_invalid_identifiers(self):
        for base, quote in [("BTC/USD", "USD"), ("BTC", ""), ("btc", "USD"), ("BTC ", "USD"), ("../BTC", "USD"), ("USD", "USD")]:
            with self.assertRaises(ValueError): Instrument(AssetClass.CRYPTO_SPOT, "KRAKEN", base, quote)
        with self.assertRaises(ValueError): Instrument(AssetClass.US_ETF, "NASDAQ", "SPY", "USD")
        with self.assertRaises(ValueError): Instrument(AssetClass.US_EQUITY, "XNAS", "AAPL", "EUR")

    def candle(self, **kwargs):
        values = dict(instrument=Instrument(AssetClass.CRYPTO_SPOT, "KRAKEN", "BTC", "USD"), timeframe=Timeframe.HOUR,
                      opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc), open=10, high=12, low=9, close=11, volume=0)
        return Candle(**(values | kwargs))

    def test_completion_boundary(self):
        candle = self.candle()
        self.assertIs(candle.require_complete(as_of=candle.closed_at), candle)
        for before in (candle.opened_at - timedelta(days=1), candle.closed_at - timedelta(microseconds=1)):
            with self.assertRaises(ValueError): candle.require_complete(as_of=before)
        with self.assertRaises(ValueError): candle.require_complete(as_of=datetime(2026, 1, 1))

    def test_timezone_and_malformed_candles(self):
        instant = datetime(2026, 1, 1, 5, tzinfo=timezone(timedelta(hours=5)))
        self.assertEqual(self.candle(opened_at=instant).opened_at, datetime(2026, 1, 1, tzinfo=timezone.utc))
        for updates in ({"opened_at": datetime(2026, 1, 1)}, {"close": float("nan")}, {"volume": -1}, {"high": 1}, {"open": True}, {"timeframe": "1h"}):
            with self.assertRaises(ValueError): self.candle(**updates)
