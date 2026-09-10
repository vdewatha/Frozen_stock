from __future__ import annotations

import json
import time
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from urllib.request import Request, urlopen
from urllib.parse import quote as urlquote

import numpy as np
import pandas as pd
import yfinance as yf
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import Asset, MarketPrice


def _clean_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def _provider_symbol(symbol: str) -> str:
    symbol = _clean_symbol(symbol)
    if not re.fullmatch(r"\^?[A-Z0-9][A-Z0-9.=-]{0,23}", symbol):
        raise ValueError("Invalid Yahoo provider symbol")
    return symbol


def _validated_provider_frame(frame: pd.DataFrame, *, as_of: datetime | None = None) -> pd.DataFrame:
    """Reject incomplete OHLCV rows; adjusted close alone may use raw close.

    Missing adjusted close means no adjustment information is available, not
    an invented OHLC observation. Corporate-action normalization remains separate.
    Daily bars dated today or later in UTC are withheld conservatively. This
    one-day lag is not an exchange-session calendar or session-close detector.
    """
    required = ["date", "open", "high", "low", "close", "volume"]
    if frame.empty or not set(required).issubset(frame.columns) or frame.columns.duplicated().any():
        return pd.DataFrame()
    frame = frame.copy()
    frame["date"] = pd.to_datetime(frame["date"], utc=True, errors="coerce")
    clock = as_of if as_of is not None else datetime.now(timezone.utc)
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise ValueError("Provider validation clock must be timezone aware")
    cutoff = pd.Timestamp(clock).tz_convert("UTC").normalize()
    for column in required[1:]:
        # Boolean fields are not market observations.
        frame[column] = frame[column].map(lambda value: np.nan if isinstance(value, bool) else value)
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    values = frame[required[1:]]
    valid = frame["date"].notna() & np.isfinite(values).all(axis=1)
    valid &= frame["date"] < cutoff
    valid &= (values[["open", "high", "low", "close"]] > 0).all(axis=1) & (values.volume >= 0) & (values.volume % 1 == 0)
    valid &= values.high >= values[["open", "close", "low"]].max(axis=1)
    valid &= values.low <= values[["open", "close", "high"]].min(axis=1)
    frame = frame.loc[valid].copy()
    if frame.empty:
        return pd.DataFrame()
    if "adjusted_close" not in frame:
        frame["adjusted_close"] = frame["close"]
    else:
        adjusted = pd.to_numeric(frame["adjusted_close"], errors="coerce")
        frame["adjusted_close"] = adjusted.where(np.isfinite(adjusted) & (adjusted > 0), frame["close"])
    frame["date"] = frame["date"].dt.date
    if frame["date"].duplicated().any() or not frame["date"].is_monotonic_increasing:
        return pd.DataFrame()
    return frame[["date", "open", "high", "low", "close", "adjusted_close", "volume"]]


def _price_row_to_dict(row: MarketPrice) -> dict:
    return {
        "date": str(row.price_date),
        "open": float(row.open or 0),
        "high": float(row.high or 0),
        "low": float(row.low or 0),
        "close": float(row.close or 0),
        "adjusted_close": float(row.adjusted_close or row.close or 0),
        "volume": int(row.volume or 0),
        "source": row.source or "unknown",
    }


def fetch_yfinance_prices(symbol: str, period: str = "2y") -> pd.DataFrame:
    symbol = _provider_symbol(symbol)
    try:
        frame = yf.download(symbol, period=period, auto_adjust=False, progress=False, threads=False)
    except Exception:
        return pd.DataFrame()
    if frame.empty:
        return pd.DataFrame()
    if isinstance(frame.columns, pd.MultiIndex):
        if frame.columns.nlevels != 2 or set(frame.columns.get_level_values(1)) != {symbol}:
            return pd.DataFrame()
        frame.columns = frame.columns.get_level_values(0)
    frame = frame.reset_index()
    frame.columns = [str(column).lower().replace(" ", "_") for column in frame.columns]
    rename = {"adj_close": "adjusted_close"}
    frame = frame.rename(columns=rename)
    return _validated_provider_frame(frame)


def _period_to_days(period: str) -> int:
    normalized = (period or "2y").strip().lower()
    if normalized.endswith("y") and normalized[:-1].isdigit():
        return max(1, int(normalized[:-1]) * 365)
    if normalized.endswith("mo") and normalized[:-2].isdigit():
        return max(1, int(normalized[:-2]) * 31)
    if normalized.endswith("d") and normalized[:-1].isdigit():
        return max(1, int(normalized[:-1]))
    return 365 * 2


def fetch_yahoo_chart_prices(symbol: str, period: str = "2y") -> pd.DataFrame:
    symbol = _provider_symbol(symbol)
    period2 = int(time.time())
    period1 = period2 - (_period_to_days(period) * 24 * 60 * 60)
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{urlquote(symbol, safe='')}"
        f"?period1={period1}&period2={period2}&interval=1d&events=history&includeAdjustedClose=true"
    )
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except Exception:
        return pd.DataFrame()

    try:
        chart = payload["chart"]
        if chart.get("error"):
            return pd.DataFrame()
        results = chart["result"]
        if not isinstance(results, list) or len(results) != 1:
            return pd.DataFrame()
        result = results[0]
        meta = result["meta"]
        if meta.get("symbol") != symbol or meta.get("dataGranularity") != "1d":
            return pd.DataFrame()
        timestamps = result["timestamp"]
        quotes = result["indicators"]["quote"]
        if not isinstance(timestamps, list) or not timestamps or len(quotes) != 1:
            return pd.DataFrame()
        quote = quotes[0]
        if any(not isinstance(quote.get(key), list) or len(quote[key]) != len(timestamps) for key in ("open", "high", "low", "close", "volume")):
            return pd.DataFrame()
        adjusted = ((result["indicators"].get("adjclose") or [{}])[0]).get("adjclose") or []
        if not isinstance(adjusted, list):
            return pd.DataFrame()
    except (KeyError, TypeError, AttributeError, IndexError):
        return pd.DataFrame()

    def value_at(values: list, index: int, default=None):
        return values[index] if index < len(values) else default

    open_values = quote.get("open") or []
    high_values = quote.get("high") or []
    low_values = quote.get("low") or []
    close_values = quote.get("close") or []
    volume_values = quote.get("volume") or []
    rows = []
    for index, timestamp in enumerate(timestamps):
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not np.isfinite(timestamp):
            continue
        try:
            candle_date = datetime.fromtimestamp(timestamp, timezone.utc).date()
        except (ValueError, OverflowError, OSError):
            continue
        close = value_at(close_values, index)
        rows.append(
            {
                "date": candle_date,
                "open": value_at(open_values, index),
                "high": value_at(high_values, index),
                "low": value_at(low_values, index),
                "close": close,
                "adjusted_close": adjusted[index] if index < len(adjusted) and adjusted[index] is not None else close,
                "volume": value_at(volume_values, index),
            }
        )
    return _validated_provider_frame(pd.DataFrame(rows))


def upsert_prices(db: Session, symbol: str, prices: pd.DataFrame, source: str = "unknown") -> int:
    symbol = _clean_symbol(symbol)
    if prices.empty:
        return 0

    asset = db.query(Asset).filter(Asset.symbol == symbol).one_or_none()
    if not asset:
        db.add(Asset(symbol=symbol, name=symbol))
        db.flush()

    rows = [
        {
            "symbol": symbol,
            "price_date": row.date,
            "open": Decimal(str(round(float(row.open), 6))),
            "high": Decimal(str(round(float(row.high), 6))),
            "low": Decimal(str(round(float(row.low), 6))),
            "close": Decimal(str(round(float(row.close), 6))),
            "adjusted_close": Decimal(str(round(float(row.adjusted_close), 6))),
            "volume": int(row.volume),
            "source": source,
            "imported_at": datetime.utcnow(),
        }
        for row in prices.itertuples()
    ]

    if db.bind and db.bind.dialect.name == "postgresql":
        stmt = pg_insert(MarketPrice).values(rows)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_market_prices_symbol_date",
            set_={
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "adjusted_close": stmt.excluded.adjusted_close,
                "volume": stmt.excluded.volume,
                "source": stmt.excluded.source,
                "imported_at": func.now(),
            },
        )
        db.execute(stmt)
    else:
        existing_dates = {
            item[0]
            for item in db.query(MarketPrice.price_date)
            .filter(MarketPrice.symbol == symbol, MarketPrice.price_date.in_([row["price_date"] for row in rows]))
            .all()
        }
        for row in rows:
            if row["price_date"] in existing_dates:
                db.query(MarketPrice).filter(MarketPrice.symbol == symbol, MarketPrice.price_date == row["price_date"]).update(row)
            else:
                db.add(MarketPrice(**row))
    db.commit()
    return len(rows)


def import_market_prices(db: Session, symbol: str, period: str = "2y") -> dict:
    symbol = _clean_symbol(symbol)
    prices = fetch_yfinance_prices(symbol, period)
    source = "yfinance"
    if prices.empty:
        prices = fetch_yahoo_chart_prices(symbol, period)
        source = "yahoo_chart" if not prices.empty else "unavailable"
    rows_imported = upsert_prices(db, symbol, prices, source)
    return {
        "symbol": symbol,
        "rows_imported": rows_imported,
        "start_date": prices["date"].min() if not prices.empty else None,
        "end_date": prices["date"].max() if not prices.empty else None,
        "source": source,
    }


def get_price_history(db: Session, symbol: str, limit: int = 260, auto_seed: bool = True) -> tuple[pd.DataFrame, str]:
    symbol = _clean_symbol(symbol)
    rows = (
        db.query(MarketPrice)
        .filter(MarketPrice.symbol == symbol)
        .order_by(MarketPrice.price_date.desc())
        .limit(limit)
        .all()
    )
    source = "database"
    if not rows and auto_seed:
        import_result = import_market_prices(db, symbol, "2y")
        source = import_result["source"]
        rows = (
            db.query(MarketPrice)
            .filter(MarketPrice.symbol == symbol)
            .order_by(MarketPrice.price_date.desc())
            .limit(limit)
            .all()
        )
    rows = list(reversed(rows))
    frame = pd.DataFrame([_price_row_to_dict(row) for row in rows])
    if not frame.empty:
        frame = frame.rename(columns={"date": "date"})
        sources = sorted(set(frame["source"].dropna().tolist())) if "source" in frame else []
        source = f"database:{sources[0]}" if len(sources) == 1 else f"database:mixed({','.join(sources)})" if sources else source
    return frame, source


def get_price_points(db: Session, symbol: str, limit: int = 260) -> tuple[list[dict], str]:
    frame, source = get_price_history(db, symbol, limit)
    if frame.empty:
        return [], source
    return frame.to_dict(orient="records"), source
