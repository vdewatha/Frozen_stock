"""Credential-free Kraken spot observations, never a trading client.

Kraken documents up to 720 recent rows and a final uncommitted row. The live
endpoint has returned 721 total (720 committed plus current), which is our
strict response bound. `since` supports incremental polling, NOT backfill.
https://docs.kraken.com/api-reference/market-data/get-ohlc-data
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import math

import httpx

from app.services.instruments import AssetClass, Candle, Instrument, Timeframe, utc_timestamp


BTC_USD = Instrument(AssetClass.CRYPTO_SPOT, "KRAKEN", "BTC", "USD")
HISTORY_LIMIT = 720
MAX_RESPONSE_ROWS = 721
HISTORY_WARNING = "Kraken documents 720 recent rows; observed responses include up to 720 committed candles plus one current row. since cannot retrieve older history."


class KrakenError(RuntimeError):
    """Sanitized public-market-data failure."""


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("Invalid numeric field")
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("Nonfinite field")
    return number


class KrakenPublicClient:
    history_limit = HISTORY_LIMIT
    history_warning = HISTORY_WARNING

    def __init__(self, *, timeout: float = 10, transport: httpx.BaseTransport | None = None):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise KrakenError("Timeout must be positive and at most 30 seconds.")
        self._client = httpx.Client(base_url="https://api.kraken.com/0/public/", timeout=timeout,
                                    follow_redirects=False, trust_env=False, transport=transport)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self._client.close()

    def ohlc(self, instrument: Instrument = BTC_USD, timeframe: Timeframe = Timeframe.HOUR,
             *, since: int | None = None, as_of: datetime | None = None) -> list[Candle]:
        if instrument != BTC_USD or not isinstance(timeframe, Timeframe):
            raise KrakenError("Only canonical Kraken BTC/USD and supported fixed timeframes are available.")
        if since is not None and (type(since) is not int or since < 0):
            raise KrakenError("since must be a nonnegative integer timestamp.")
        try:
            clock = utc_timestamp(as_of if as_of is not None else datetime.now(timezone.utc))
        except ValueError:
            raise KrakenError("as_of must be timezone aware.") from None
        params = {"pair": "XBTUSD", "interval": timeframe.seconds // 60}
        if since is not None:
            params["since"] = since
        try:
            response = self._client.get("OHLC", params=params)
        except httpx.HTTPError:
            raise KrakenError("Kraken public request failed or timed out.") from None
        if response.status_code != 200:
            raise KrakenError(f"Kraken returned HTTP {response.status_code}.")
        try:
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("error") != []:
                raise ValueError("Provider error")
            result = payload["result"]
            if not isinstance(result, dict) or set(result) != {"XXBTZUSD", "last"}:
                raise ValueError("Pair identity mismatch")
            if type(result["last"]) is not int or result["last"] < 0:
                raise ValueError("Invalid cursor")
            rows = result["XXBTZUSD"]
            if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_RESPONSE_ROWS:
                raise ValueError("Invalid row count")
            candles = []
            previous = None
            for index, row in enumerate(rows):
                if not isinstance(row, list) or len(row) != 8 or type(row[0]) is not int or row[0] < 0:
                    raise ValueError("Invalid OHLC row")
                timestamp = row[0]
                if timestamp % timeframe.seconds or (previous is not None and timestamp - previous != timeframe.seconds):
                    raise ValueError("Unaligned, duplicate, unordered or missing interval")
                previous = timestamp
                opened = datetime.fromtimestamp(timestamp, timezone.utc)
                if opened > clock:
                    raise ValueError("Future observation")
                op, high, low, close, vwap, volume = map(_decimal, row[1:7])
                if min(op, high, low, close, vwap) <= 0 or volume < 0 or not low <= min(op, close, vwap) <= max(op, close, vwap) <= high:
                    raise ValueError("Invalid OHLC bounds")
                if type(row[7]) is not int or row[7] < 0:
                    raise ValueError("Invalid count")
                candle = Candle(instrument, timeframe, opened, float(op), float(high), float(low), float(close), float(volume))
                # Even a stale final row is uncommitted per the provider contract.
                if index < len(rows) - 1:
                    candles.append(candle.require_complete(as_of=clock))
            return candles
        except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError, OSError):
            raise KrakenError("Kraken returned invalid or incomplete OHLC data.") from None
