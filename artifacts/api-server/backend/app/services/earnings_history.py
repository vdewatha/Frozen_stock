"""Past earnings surprises, aligned to the session that first traded on them.

The ``earn_surprise`` rule feature (post-earnings drift) needs, for each daily
bar, the EPS surprise of a report the market reacted to on that bar.  yfinance
gives roughly 100 quarters of report timestamps (New York time) with the
surprise in percent.  A report before the 09:30 open is traded that session;
anything later (after the close, or during the session) is traded the next
session.  Reports with no time of day are dropped: guessing the session would
put a day of look-ahead or lag into the reaction-day return.

Discovery reads a CSV cache next to the price history; the paper path fetches
through a per-process cache with a timeout.  A provider failure leaves the
column empty, so the rule simply does not fire.  Paper only.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, time, timedelta, timezone
import logging
from pathlib import Path
import threading
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
SURPRISE_COLUMN = "earn_surprise"
MARKET_OPEN = time(9, 30)
# yfinance reports a few absurd surprises when the estimate was near zero.
MAX_ABS_SURPRISE_PCT = 500.0
CSV_FRESH_FOR = timedelta(days=7)
RUNTIME_FRESH_FOR = timedelta(hours=12)
RUNTIME_RETRY_AFTER = timedelta(minutes=30)
FETCH_TIMEOUT_SECONDS = 10
COLUMNS = ["reported_at", "surprise_pct"]

_cache: dict[str, tuple[datetime, Optional[pd.DataFrame]]] = {}
_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="earnings-history")


def fetch_surprises(symbol: str, limit: int = 100) -> pd.DataFrame:
    """Raw yfinance history: one row per report with a known surprise."""
    import yfinance as yf

    raw = yf.Ticker(symbol).get_earnings_dates(limit=limit)
    if raw is None or raw.empty or "Surprise(%)" not in raw.columns:
        return pd.DataFrame(columns=COLUMNS)
    return pd.DataFrame({"reported_at": pd.to_datetime(raw.index, utc=True).tz_convert(ET),
                         "surprise_pct": pd.to_numeric(raw["Surprise(%)"], errors="coerce").to_numpy()})


def clean_surprises(events: pd.DataFrame) -> pd.DataFrame:
    """Usable reports: known surprise, sane size, a time of day, one row per report day."""
    if events is None or events.empty:
        return pd.DataFrame(columns=COLUMNS)
    out = events.loc[:, COLUMNS].copy()
    out["reported_at"] = pd.to_datetime(out["reported_at"], utc=True).dt.tz_convert(ET)
    out["surprise_pct"] = pd.to_numeric(out["surprise_pct"], errors="coerce")
    out = out.loc[np.isfinite(out["surprise_pct"]) & (out["surprise_pct"].abs() <= MAX_ABS_SURPRISE_PCT)]
    clock = out["reported_at"].dt.time
    out = out.loc[clock != time(0, 0)]
    # Two different surprises on one day is a data error: keep neither.
    day = out["reported_at"].dt.date
    out = out.loc[~day.duplicated(keep=False)]
    return out.sort_values("reported_at").reset_index(drop=True)


def reaction_positions(dates: pd.Series, events: pd.DataFrame) -> np.ndarray:
    """Index of the first bar that traded on each report (-1 when beyond the frame)."""
    bar_days = pd.to_datetime(dates.astype(str).str[:10]).to_numpy().astype("datetime64[D]")
    if events.empty or not len(bar_days):
        return np.full(len(events), -1)
    stamps = pd.to_datetime(events["reported_at"], utc=True).dt.tz_convert(ET)
    report_days = stamps.dt.tz_localize(None).dt.normalize().to_numpy().astype("datetime64[D]")
    before_open = (stamps.dt.time < MARKET_OPEN).to_numpy()
    positions = np.where(before_open, np.searchsorted(bar_days, report_days, side="left"),
                         np.searchsorted(bar_days, report_days, side="right"))
    return np.where(positions < len(bar_days), positions, -1)


def attach_surprises(frame: pd.DataFrame, events: Optional[pd.DataFrame]) -> pd.DataFrame:
    """``frame`` with ``earn_surprise`` set on reaction bars (NaN elsewhere, all NaN when unknown)."""
    out = frame.reset_index(drop=True).copy()
    values = np.full(len(out), np.nan)
    if events is not None and not events.empty:
        positions = reaction_positions(out["date"], events)
        # Two reports traded on one bar cannot be told apart: leave that bar empty.
        bars, counts = np.unique(positions[positions >= 0], return_counts=True)
        hit = np.isin(positions, bars[counts == 1])
        values[positions[hit]] = events["surprise_pct"].to_numpy(float)[hit]
    out[SURPRISE_COLUMN] = values
    return out


def load_surprises(symbol: str, data_dir: Path, *, download: bool = False,
                   fetch: Optional[Callable[[str], pd.DataFrame]] = None,
                   now: Optional[datetime] = None) -> pd.DataFrame:
    """Cleaned history from ``<data_dir>/earnings/<symbol>.csv``, refreshed weekly when ``download``."""
    path = Path(data_dir) / "earnings" / f"{symbol.upper()}.csv"
    now = now or datetime.now(UTC)
    stale = not path.exists() or now - datetime.fromtimestamp(path.stat().st_mtime, UTC) > CSV_FRESH_FOR
    if download and stale:
        try:
            fresh = clean_surprises((fetch or fetch_surprises)(symbol))
            path.parent.mkdir(parents=True, exist_ok=True)
            fresh.to_csv(path, index=False)
        except Exception as exc:  # keep the old cache on a provider failure
            logger.warning("earnings history unavailable for %s: %s", symbol, exc)
    if not path.exists():
        return pd.DataFrame(columns=COLUMNS)
    return clean_surprises(pd.read_csv(path))


def runtime_surprises(symbol: str, now: Optional[datetime] = None,
                      fetch: Optional[Callable[[str], pd.DataFrame]] = None) -> Optional[pd.DataFrame]:
    """Cleaned history for the paper path, or ``None`` when the provider is unavailable."""
    symbol = symbol.upper()
    now = now or datetime.now(UTC)
    with _lock:
        cached = _cache.get(symbol)
    if cached is not None:
        fetched_at, events = cached
        if now - fetched_at < (RUNTIME_FRESH_FOR if events is not None else RUNTIME_RETRY_AFTER):
            return events
    try:
        events = clean_surprises(_pool.submit(fetch or fetch_surprises, symbol).result(timeout=FETCH_TIMEOUT_SECONDS))
    except FutureTimeout:
        logger.info("earnings history timed out for %s", symbol)
        events = None
    except Exception as exc:  # provider errors must not stop the cycle
        logger.info("earnings history unavailable for %s: %s", symbol, exc.__class__.__name__)
        events = None
    with _lock:
        _cache[symbol] = (now, events)
    return events


def clear_cache() -> None:
    with _lock:
        _cache.clear()
