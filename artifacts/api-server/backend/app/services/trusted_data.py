"""Fail-closed market data boundary for persisted models and paper execution."""
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd

from app.models import Asset, IntradayBar
from app.services.market_data import get_price_histories, get_price_history
from app.services.intraday_data import (
    feed_status,
    MARKET_DATA_PROVIDER,
    MARKET_DATA_FEED_CLASS,
)

TRUSTED_SOURCES = frozenset({"yfinance", "yahoo_chart", "alpaca_sip_daily", "alpaca_iex_daily"})


class UntrustedMarketData(ValueError):
    pass

def validate_intraday_readiness(
    db, symbol: str, *, now: datetime | None = None, max_age_seconds: int = 120, status: dict | None = None,
) -> dict:
    """Require an authenticated, current execution observation.

    Historical repair gaps remain visible to research callers, but an incomplete
    decision session never becomes paper-decision evidence merely because its
    newest bar is current. Bounded backfill continues independently while this
    boundary remains fail-closed.
    """
    now = now or datetime.now(timezone.utc)
    if status is None:
        # ``status`` is a feed status the caller already read (feed_statuses).
        try:
            status = feed_status(db, symbol, now=now)
        except ValueError as exc:
            raise UntrustedMarketData(str(exc)) from exc
    # A current bar cannot mask a gap in the same decision session.
    if status["status"] not in {"ready", "market_closed"}:
        reason = status.get("unavailable_reason") or "Intraday feed is not ready."
        raise UntrustedMarketData(f"{reason}; paper decisions are blocked.")
    return status


def trusted_intraday_observation(db, symbol: str, *, now: datetime | None = None) -> dict:
    """Return the exact completed Tradier production bar eligible for a paper decision."""
    status = validate_intraday_readiness(db, symbol, now=now)
    row = (
        db.query(IntradayBar)
        .filter(
            IntradayBar.symbol == symbol.strip().upper(),
            IntradayBar.timeframe == "1m",
            IntradayBar.provider == MARKET_DATA_PROVIDER,
            IntradayBar.feed_class == MARKET_DATA_FEED_CLASS,
        )
        .order_by(IntradayBar.opened_at.desc())
        .first()
    )
    if row is None:
        raise UntrustedMarketData("Intraday feed has no eligible completed observation.")
    return {
        "provider": row.provider,
        "feed_class": row.feed_class,
        "timeframe": row.timeframe,
        "opened_at": row.opened_at,
        "exchange_timestamp": row.exchange_timestamp,
        "ingested_at": row.ingested_at,
        "close": float(row.close),
        "status": status["status"],
        "adjustment_policy": "raw current-session execution observation",
    }


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


def trusted_history(
    db,
    symbol: str,
    limit: int = 260,
    minimum: int = 60,
    require_active: bool = True,
    provider_source: str | None = None,
):
    symbol = symbol.strip().upper()
    if require_active and db.query(Asset).filter(Asset.symbol == symbol, Asset.is_active.is_(True)).one_or_none() is None:
        raise UntrustedMarketData(f"Requested instrument {symbol} is not active.")
    if provider_source is None:
        prices, source = get_price_history(db, symbol, limit, auto_seed=False)
    else:
        prices, source = get_price_history(
            db, symbol, limit, auto_seed=False, source_filter=provider_source
        )
    return _validated_and_adjusted(prices, minimum), source


def _validated_and_adjusted(prices: pd.DataFrame, minimum: int) -> pd.DataFrame:
    validate_history(prices, minimum=minimum)
    if "adjusted_close" in prices:
        adjusted = pd.to_numeric(prices["adjusted_close"], errors="coerce")
        raw_close = pd.to_numeric(prices["close"], errors="coerce")
        factor = adjusted / raw_close
        if not np.isfinite(factor).all() or (factor <= 0).any():
            raise UntrustedMarketData("Corporate-action adjustment factors are invalid.")
        prices = prices.copy()
        for column in ("open", "high", "low", "close"):
            prices[column] = pd.to_numeric(prices[column], errors="coerce") * factor
        prices["adjustment_policy"] = "yahoo_adjusted_close_factor"
    return prices


def daily_history_checks(db, symbols: list[str], limit: int = 260, minimum: int = 60) -> dict[str, dict]:
    """What ``trusted_history(db, symbol, require_active=False)`` decides, for many symbols.

    For each symbol: ``error`` (the reason it is not trusted, or None), and the
    newest stored price's ``latest_date`` and ``latest_source`` (None when the
    symbol has no prices). The histories come from two queries per 25 symbols
    (``get_price_histories``) and pass through the same validation as the
    single-symbol call, so a readiness build no longer asks the database
    three times per active symbol.
    """
    checks: dict[str, dict] = {}
    for symbol, (prices, _source) in get_price_histories(db, symbols, limit).items():
        error = None
        try:
            _validated_and_adjusted(prices, minimum)
        except UntrustedMarketData as exc:
            error = str(exc)
        newest = prices.iloc[-1] if not prices.empty else None
        checks[symbol] = {
            "error": error,
            "latest_date": date.fromisoformat(str(newest["date"])) if newest is not None else None,
            "latest_source": str(newest["source"]) if newest is not None else None,
        }
    return checks
