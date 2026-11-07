"""The sector benchmark a stock is measured against, as a ``sector_close`` column on its daily frame.

Short-term reversal measured against the stock's own sector (Hameed & Mian
2015; Da, Liu & Schaumburg 2014) needs, for every bar, the close of the
sector ETF the stock belongs to.  ``rules.py`` reads it from an optional
``sector_close`` column (``rel_ret_N``, ``rel_rsi_N``, ``rel_z_N``); this
module decides which ETF that is and attaches the column.

* **Static map** (``static_sector``): the SPDR sector ETF of
  ``trading_universe.SECTOR_GROUPS`` (``market_gate.sector_symbol``), the same
  map the loss diagnosis and the sector gate use.  Index ETFs, the ETFs
  themselves and unmapped names have none.  Two SPDRs are younger than the
  history: before XLC (2018-06) and XLRE (2015-10) existed the chain uses
  XLK and XLF, where those companies sat in the GICS of the day.
* **Statistical map** (``assign_sectors``), for point-in-time S&P 500 members
  that are not on today's list (the static map knows only today's names):
  each month the stock is assigned to the sector ETF whose SPY-residual daily
  returns correlated most with the stock's own over the trailing 252 bars
  (at least 126), known at the last close of the previous month, so the
  assignment is causal.  Unmapped and unassigned bars carry NaN.
* The column is a *chained index* of the assigned ETF's daily returns on the
  stock's own dates, so a change of ETF (fallback or reassignment) never
  splices two price levels: every N-bar return of the column is the
  compounded return of the ETF(s) assigned on those bars.  Where one ETF
  covers the whole frame the column is that ETF's close up to a constant,
  which every relative feature ignores.
* At runtime (``runtime_sector_close``) the paper path attaches the static
  ETF's trusted daily closes from the database, cached per process.

Paper only; nothing here places or blocks an order.
"""
from __future__ import annotations

import logging
import threading
import time as time_module
from typing import Iterable, Mapping, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

SECTOR_CLOSE_COLUMN = "sector_close"
SECTOR_ETFS = ("XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY")
# Before the younger SPDRs existed their companies sat in these sectors.
FALLBACK_ETF = {"XLC": "XLK", "XLRE": "XLF"}
ASSIGN_WINDOW = 252
ASSIGN_MIN_OBSERVATIONS = 126
RUNTIME_FRESH_FOR = 600.0      # seconds a loaded ETF series is reused by the paper cycle
RUNTIME_RETRY_AFTER = 120.0    # seconds before a failed load is retried
_lock = threading.Lock()
_runtime: dict[str, tuple[float, Optional[pd.Series]]] = {}
_pinned: dict[str, Optional[pd.Series]] = {}


def static_sector(symbol: str) -> Optional[str]:
    """The SPDR sector ETF of a mapped single stock; ``None`` for ETFs and unmapped names."""
    from app.services.market_gate import sector_symbol
    from app.services.trading_universe import ETF_SYMBOLS

    symbol = str(symbol).strip().upper()
    if symbol in ETF_SYMBOLS:
        return None
    etf = sector_symbol(symbol)
    return etf if etf in SECTOR_ETFS else None


def _dates(frame: pd.DataFrame) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.to_datetime(frame["date"].astype(str).str[:10]))


def _close_series(frame: pd.DataFrame) -> pd.Series:
    close = pd.to_numeric(frame["close"], errors="coerce").astype(float).to_numpy()
    series = pd.Series(close, index=_dates(frame))
    return series[~series.index.duplicated(keep="last")].sort_index()


def etf_closes(frames: Mapping[str, pd.DataFrame], etfs: Iterable[str] = SECTOR_ETFS) -> dict[str, pd.Series]:
    """Date-indexed closes of the sector ETFs present in ``frames``."""
    return {etf: _close_series(frames[etf]) for etf in etfs if etf in frames}


def static_assignment(symbol: str, dates: pd.DatetimeIndex, closes: Mapping[str, pd.Series]) -> pd.Series:
    """Per date, the static ETF, or its fallback where the ETF has no close yet; None when unmapped."""
    etf = static_sector(symbol)
    out = pd.Series([None] * len(dates), index=dates, dtype=object)
    if etf is None:
        return out
    primary = closes.get(etf)
    first = primary.dropna().index.min() if primary is not None and primary.notna().any() else None
    fallback = FALLBACK_ETF.get(etf)
    if first is None:
        out[:] = fallback if fallback in closes else None
        return out
    out[dates >= first] = etf
    if fallback in closes:
        out[dates < first] = fallback
    return out


def assign_sectors(stock_close: pd.Series, closes: Mapping[str, pd.Series], spy_close: pd.Series, *,
                   window: int = ASSIGN_WINDOW, min_observations: int = ASSIGN_MIN_OBSERVATIONS) -> pd.Series:
    """Per stock date, the sector ETF its SPY-residual returns tracked best over the trailing window.

    Decided on the last bar of each month and used for every bar of the next
    month (causal); ``None`` before enough history exists.
    """
    stock_close = stock_close[~stock_close.index.duplicated(keep="last")].sort_index()
    dates = stock_close.index
    out = pd.Series([None] * len(dates), index=dates, dtype=object)
    if not closes or spy_close is None or len(dates) < min_observations:
        return out
    spy = spy_close.reindex(dates)
    market = spy.pct_change(fill_method=None)
    residual = stock_close.pct_change(fill_method=None) - market
    etf_residual = pd.DataFrame({etf: series.reindex(dates).pct_change(fill_method=None) - market
                                 for etf, series in closes.items()}, index=dates)
    corr = etf_residual.rolling(window, min_periods=min_observations).corr(residual)
    month = dates.to_period("M")
    month_end = corr.groupby(month).tail(1)
    finite = month_end.notna().any(axis=1)
    best = month_end.where(month_end.notna(), -np.inf).idxmax(axis=1).where(finite, None)
    by_month = pd.Series(best.to_numpy(), index=month[np.isin(dates, month_end.index)])
    by_month.index = by_month.index + 1        # decided at month end, used through the next month
    assigned = pd.Series(month, index=dates).map(by_month.to_dict())
    return assigned.where(assigned.notna(), None).astype(object)


def chained_index(dates: pd.DatetimeIndex, assignment: pd.Series, closes: Mapping[str, pd.Series]) -> np.ndarray:
    """Chained index of the assigned ETF's daily returns on ``dates``; NaN where unassigned or unpriced."""
    n = len(dates)
    if n == 0:
        return np.array([], dtype=float)
    returns = np.full(n, np.nan)
    known = np.zeros(n, dtype=bool)
    names = assignment.reindex(dates).to_numpy(dtype=object)
    for etf in {name for name in names if name}:
        series = closes.get(etf)
        if series is None:
            continue
        aligned = series.reindex(dates)
        filled = aligned.ffill()
        step = (filled / filled.shift(1) - 1).to_numpy(float)
        rows = names == etf
        returns[rows] = step[rows]
        known[rows] = aligned.notna().to_numpy()[rows]
    level = np.cumprod(1 + np.nan_to_num(returns, nan=0.0))
    return np.where(known, level, np.nan)


def attach_sector_close(frame: pd.DataFrame, sector_close: Optional[pd.Series]) -> pd.DataFrame:
    """``frame`` with a ``sector_close`` column aligned on its dates (exact match; NaN where missing)."""
    out = frame.copy()
    if sector_close is None or not len(sector_close):
        out[SECTOR_CLOSE_COLUMN] = np.nan
        return out
    series = pd.Series(pd.to_numeric(pd.Series(sector_close), errors="coerce").to_numpy(float),
                       index=pd.DatetimeIndex(pd.to_datetime(pd.Series(sector_close).index)).normalize())
    series = series[~series.index.duplicated(keep="last")]
    out[SECTOR_CLOSE_COLUMN] = series.reindex(_dates(frame).normalize()).to_numpy(float)
    return out


def attach_sector_closes(frames: Mapping[str, pd.DataFrame], *, statistical: bool = True) -> tuple[dict, dict]:
    """Every single-stock frame with its ``sector_close`` column; returns ``(frames, method by symbol)``.

    Today's mapped names use the static map; other single stocks the
    statistical assignment when ``statistical`` (the ETF and SPY frames must
    be in ``frames``); ETFs are returned unchanged.  ``method`` is
    ``static``, ``statistical`` or ``none``.
    """
    from app.services.trading_universe import ETF_SYMBOLS

    closes = etf_closes(frames)
    spy = _close_series(frames["SPY"]) if "SPY" in frames else None
    out, method = {}, {}
    for symbol, frame in frames.items():
        if symbol in ETF_SYMBOLS or not closes:
            out[symbol] = frame
            continue
        dates = _dates(frame)
        if static_sector(symbol) is not None:
            assignment, how = static_assignment(symbol, dates, closes), "static"
        elif statistical and spy is not None:
            assignment = assign_sectors(_close_series(frame), closes, spy).reindex(dates)
            how = "statistical"
        else:
            assignment, how = pd.Series([None] * len(dates), index=dates, dtype=object), "none"
        values = chained_index(dates, assignment, closes)
        out[symbol] = frame.copy()
        out[symbol][SECTOR_CLOSE_COLUMN] = values
        method[symbol] = how if np.isfinite(values).any() else "none"
    return out, method


# ---------------------------------------------------------------- runtime (paper path)

def set_runtime_sector(etf: str, series: Optional[pd.Series]) -> None:
    """Pin an ETF's close series (tests, offline runs); ``None`` pins "unavailable"."""
    with _lock:
        _pinned[etf.upper()] = series


def clear_cache() -> None:
    with _lock:
        _runtime.clear()
        _pinned.clear()


def _load_etf_closes(etf: str) -> pd.Series:
    from app.db.session import SessionLocal
    from app.services.trusted_data import trusted_history

    with SessionLocal() as db:
        frame = trusted_history(db, etf, limit=600, require_active=False)[0]
    return _close_series(frame)


def runtime_sector_close(symbol: str) -> Optional[pd.Series]:
    """The static sector ETF's trusted daily closes for ``symbol``; ``None`` when unmapped or unavailable."""
    etf = static_sector(symbol)
    if etf is None:
        return None
    now = time_module.monotonic()
    with _lock:
        if etf in _pinned:
            return _pinned[etf]
        cached = _runtime.get(etf)
    if cached is not None:
        loaded_at, series = cached
        if now - loaded_at < (RUNTIME_FRESH_FOR if series is not None else RUNTIME_RETRY_AFTER):
            return series
    try:
        series = _load_etf_closes(etf)
    except Exception as exc:  # the strategy stays neutral; retried after RUNTIME_RETRY_AFTER
        logger.info("sector history unavailable for %s (%s): %s", symbol, etf, exc.__class__.__name__)
        series = None
    with _lock:
        _runtime[etf] = (now, series)
    return series
