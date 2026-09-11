"""Canonical spot/equity identities and fixed UTC candle intervals.

Equity trading sessions/holidays are deliberately not inferred here.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import math
import re


class AssetClass(str, Enum):
    US_EQUITY = "us_equity"
    US_ETF = "us_etf"
    CRYPTO_SPOT = "crypto_spot"


class Timeframe(str, Enum):
    MINUTE = "1m"
    FIVE_MINUTES = "5m"
    FIFTEEN_MINUTES = "15m"
    HOUR = "1h"
    FOUR_HOURS = "4h"
    DAY = "1d"

    @property
    def seconds(self):
        return {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}[self.value]


@dataclass(frozen=True)
class Instrument:
    asset_class: AssetClass
    venue: str
    base: str
    quote: str

    def __post_init__(self):
        if not isinstance(self.asset_class, AssetClass):
            raise ValueError("asset_class must be an AssetClass")
        for name in ("venue", "base", "quote"):
            value = getattr(self, name)
            if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,23}", value):
                raise ValueError(f"{name} must be a canonical uppercase identifier")
        if self.base == self.quote:
            raise ValueError("Base and quote must differ")
        if self.asset_class in (AssetClass.US_EQUITY, AssetClass.US_ETF):
            if self.quote != "USD" or not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", self.base):
                raise ValueError("US securities require a ticker and USD quote")
            if not re.fullmatch(r"[A-Z]{4}", self.venue):
                raise ValueError("US securities require a four-letter venue MIC")
        elif not re.fullmatch(r"[A-Z0-9]{1,16}", self.base) or not re.fullmatch(r"[A-Z0-9]{1,16}", self.quote):
            raise ValueError("Crypto requires separate base and quote currency identifiers")

    @property
    def instrument_id(self):
        return f"{self.asset_class.value}:{self.venue}:{self.base}:{self.quote}"

    @property
    def provider_symbol(self):
        return f"{self.base}/{self.quote}" if self.asset_class == AssetClass.CRYPTO_SPOT else self.base


def utc_timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must have an explicit timezone")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class Candle:
    instrument: Instrument
    timeframe: Timeframe
    opened_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self):
        if not isinstance(self.instrument, Instrument) or not isinstance(self.timeframe, Timeframe):
            raise ValueError("Candle requires explicit instrument and timeframe")
        object.__setattr__(self, "opened_at", utc_timestamp(self.opened_at))
        for name in ("open", "high", "low", "close", "volume"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
                raise ValueError(f"{name} must be finite numeric data")
            if value < 0 or (name != "volume" and value == 0):
                raise ValueError("Prices must be positive and volume nonnegative")
        if self.high < max(self.open, self.close, self.low) or self.low > min(self.open, self.close, self.high):
            raise ValueError("Invalid OHLC bounds")

    @property
    def closed_at(self):
        return self.opened_at + timedelta(seconds=self.timeframe.seconds)

    def require_complete(self, *, as_of: datetime):
        """Fixed-interval completion only, not exchange session validation."""
        as_of = utc_timestamp(as_of)
        if self.closed_at > as_of:
            raise ValueError("Future or incomplete candle")
        return self
