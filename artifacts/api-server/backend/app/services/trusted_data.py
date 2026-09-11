"""Fail-closed market data boundary for persisted models and paper execution."""
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from app.models import Asset
from app.services.market_data import get_price_history

TRUSTED_SOURCES = frozenset({"yfinance", "yahoo_chart"})


class UntrustedMarketData(ValueError):
    pass


def validate_history(prices: pd.DataFrame, minimum: int = 60, max_age_days: int = 7) -> None:
    required = {"date", "open", "high", "low", "close", "volume", "source"}
    if len(prices) < minimum or not required.issubset(prices.columns):
        raise UntrustedMarketData(f"At least {minimum} complete trusted observations are required.")
    if not prices["source"].isin(TRUSTED_SOURCES).all():
        raise UntrustedMarketData("History contains unknown or synthetic provenance.")
    if prices["source"].nunique() != 1:
        raise UntrustedMarketData("Mixed provider histories require explicit normalization before use.")
    dates = pd.to_datetime(prices["date"], utc=True, errors="coerce")
    now = pd.Timestamp(datetime.now(timezone.utc))
    if dates.isna().any() or dates.duplicated().any() or not dates.is_monotonic_increasing:
        raise UntrustedMarketData("Candle timestamps must be valid, unique, and increasing.")
    if (dates > now).any() or (now - dates.iloc[-1]).total_seconds() > max_age_days * 86400:
        raise UntrustedMarketData("Market prices are stale or future dated.")
    values = prices[["open", "high", "low", "close", "volume"]].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(values.to_numpy()).all() or (values.iloc[:, :4] <= 0).any().any() or (values["volume"] < 0).any():
        raise UntrustedMarketData("Candle values must be finite and prices positive.")
    if (values["high"] < values[["open", "close", "low"]].max(axis=1)).any() or (values["low"] > values[["open", "close", "high"]].min(axis=1)).any():
        raise UntrustedMarketData("Candle high/low bounds are invalid.")


def trusted_history(db, symbol: str, limit: int = 260, minimum: int = 60, require_active: bool = True):
    symbol = symbol.strip().upper()
    if require_active and db.query(Asset).filter(Asset.symbol == symbol, Asset.is_active.is_(True)).one_or_none() is None:
        raise UntrustedMarketData(f"Requested instrument {symbol} is not active.")
    prices, source = get_price_history(db, symbol, limit, auto_seed=False)
    validate_history(prices, minimum=minimum)
    return prices, source
