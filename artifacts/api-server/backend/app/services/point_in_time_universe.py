"""Point-in-time S&P 500 universe for replays, at $0 data cost.

Every replay, discovery run and experiment so far used today's 45-symbol list
(``DEFAULT_PAPER_EXECUTION_SYMBOLS``): megacaps that *survived* to 2026 plus
index and sector ETFs.  Replaying a single-stock rule over the 2000-2026 history
of names chosen in 2026 flatters every number (survivorship bias), and every
report has had to say so.  This module gives the bot the universe as it stood on
each date, so a replay can only enter a single stock while it was an S&P 500
member.

Source (free, redistributable, refreshed monthly)
-------------------------------------------------
``sp500_ticker_start_end.csv`` from https://github.com/fja05680/sp500 (MIT
licence, Farrell J. Aultman): one row per membership interval of a ticker,
``ticker,start_date,end_date``, 1996-01-02 to the last update (2026-08-18 when
this was written).  The repository derives it from Andreas Clenow's original
1996-2019 list and Wikipedia's "Selected changes" table after 2019; the author
notes the first ~5 years may miss a few names (487 symbols on the first row,
494 by 2001).  ``end_date`` is the first date the ticker was *out* (exclusive):
AAL is listed on 1997-01-13 and absent on 1997-01-15, its ``end_date``.  An
empty ``end_date`` means still a member.  Ticker changes appear as one interval
ending and another starting on the same day (FB -> META on 2022-06-09);
``TICKER_ALIASES`` credits the old ticker's days to the symbol the history is
cached under.

Cached data format (all under ``meta_label_learning.history_dir()``)
---------------------------------------------------------------------
* ``universe/sp500_membership.csv``: the source file as downloaded (columns
  ``ticker,start_date,end_date``, ISO dates, empty end = still a member).
* ``universe/sp500_membership.json``: ``source`` (URL), ``repository``,
  ``licence``, ``downloaded_at`` (UTC ISO), ``rows``, ``first_date``,
  ``last_date``.  The membership file is refreshed after ``REFRESH_AFTER``
  (30 days); a failed refresh keeps the old file.
* ``universe/sp500_coverage.json``: ``checked_at``, ``start``, ``by_year``
  (``{"2010": {"members": n, "with_data": n, "coverage": share}}``),
  ``coverage_since_2010`` (mean of the yearly shares from 2010), ``available``
  and ``missing`` ticker lists and ``symbols`` (``status`` ok / empty / missing,
  ``rows``, ``first``, ``last`` per ticker).  Refreshed after ``REFRESH_AFTER``.
* ``<YAHOO>.csv``: the shared adjusted daily history cache
  (``strategy_scorecard.load_daily_history``); dotted tickers are stored under
  their Yahoo spelling (``BRK.B`` -> ``BRK-B.csv``).

What a replay gets
------------------
``Membership.mask(symbol, dates)`` is the per-bar entry mask ``gate_replay``
ANDs into its entries (``simulate(..., universe=mask)``): index and sector ETFs
(``trading_universe.ETF_SYMBOLS``) are always in, a single stock only on the
days it was a member, an unknown ticker never.  ``replay_universe`` builds the
whole context for the experiment engine and strategy discovery: the broader
symbol list (today's list plus every past member whose history yfinance still
serves), the mask, and cache-first history / earnings loaders that keep the
weekly jobs inside their budget.

Many delisted tickers have no yfinance history at all (acquired or bankrupt
names drop out of Yahoo), so even this universe keeps some survivorship; the
coverage report says how much, year by year, and ``scripts/
run_survivorship_check.py`` measures the gap on the live strategies.  Nothing
here places or blocks an order.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

import numpy as np
import pandas as pd

from app.services.trading_universe import ETF_SYMBOLS

logger = logging.getLogger(__name__)
UTC = timezone.utc

SOURCE_REPOSITORY = "https://github.com/fja05680/sp500"
SOURCE_URL = "https://raw.githubusercontent.com/fja05680/sp500/master/sp500_ticker_start_end.csv"
SOURCE_LICENCE = "MIT (Copyright (c) 2019-2020 Farrell J. Aultman)"
REFRESH_AFTER = timedelta(days=30)
HISTORY_FRESH_FOR = timedelta(days=7)
FIRST_DATE = "2000-01-01"
COVERAGE_FROM_YEAR = 2010
MIN_HISTORY_ROWS = 60
MIN_SOURCE_ROWS = 400
# A ticker change inside one company: the old symbol's membership days belong
# to the symbol the daily history is cached under.
TICKER_ALIASES: dict[str, str] = {"FB": "META"}
ALWAYS_MEMBERS = frozenset(ETF_SYMBOLS)
# Earnings histories the point-in-time run fetches for the *extra* symbols per
# run (one yfinance call each); the rest fill over the following weekly runs.
EARNINGS_DOWNLOADS_PER_RUN = 60

UniverseMask = Callable[[str, pd.DatetimeIndex], np.ndarray]


def universe_dir() -> Path:
    from app.services.meta_label_learning import history_dir

    return history_dir() / "universe"


def membership_path() -> Path:
    return universe_dir() / "sp500_membership.csv"


def coverage_path() -> Path:
    return universe_dir() / "sp500_coverage.json"


def yahoo_symbol(ticker: str) -> str:
    """The spelling yfinance (and the shared history cache) uses: ``BRK.B`` -> ``BRK-B``."""
    return ticker.strip().upper().replace(".", "-")


# ------------------------------------------------------------------ membership

def parse_membership(source: str | pd.DataFrame) -> pd.DataFrame:
    """``ticker,start,end`` intervals (``end`` NaT = still a member) from the source CSV.

    Aliases are applied, tickers upper-cased, bad or inverted rows dropped and
    touching or overlapping intervals of one ticker merged, so FB + META become
    one interval.
    """
    frame = pd.read_csv(StringIO(source)) if isinstance(source, str) else source.copy()
    frame.columns = [str(column).strip().lower() for column in frame.columns]
    needed = {"ticker", "start_date", "end_date"}
    if not needed <= set(frame.columns):
        raise ValueError(f"membership source needs columns {sorted(needed)}, got {list(frame.columns)}")
    out = pd.DataFrame({
        "ticker": frame["ticker"].astype(str).str.strip().str.upper().map(lambda t: TICKER_ALIASES.get(t, t)),
        "start": pd.to_datetime(frame["start_date"], errors="coerce"),
        "end": pd.to_datetime(frame["end_date"], errors="coerce"),
    })
    out = out[out["ticker"].ne("") & out["ticker"].ne("NAN") & out["start"].notna()]
    out = out[out["end"].isna() | (out["end"] > out["start"])]
    merged = []
    for ticker, group in out.sort_values(["ticker", "start"]).groupby("ticker", sort=True):
        current_start, current_end = None, None
        for start, end in zip(group["start"], group["end"]):
            if current_start is None:
                current_start, current_end = start, end
                continue
            if pd.isna(current_end) or start <= current_end:  # touching or overlapping: extend
                if not pd.isna(current_end):
                    current_end = end if pd.isna(end) else max(current_end, end)
                continue
            merged.append((ticker, current_start, current_end))
            current_start, current_end = start, end
        merged.append((ticker, current_start, current_end))
    return pd.DataFrame(merged, columns=["ticker", "start", "end"]).reset_index(drop=True)


@dataclass(frozen=True)
class Membership:
    """Membership intervals per ticker; callable as the ``universe`` mask of ``gate_replay.simulate``."""

    intervals: Mapping[str, tuple[tuple[pd.Timestamp, Optional[pd.Timestamp]], ...]]
    source: dict = field(default_factory=dict)

    @classmethod
    def from_frame(cls, frame: pd.DataFrame, source: Optional[dict] = None) -> "Membership":
        intervals: dict[str, list] = {}
        for row in frame.itertuples(index=False):
            end = None if pd.isna(row.end) else pd.Timestamp(row.end)
            intervals.setdefault(str(row.ticker), []).append((pd.Timestamp(row.start), end))
        return cls({ticker: tuple(sorted(spans, key=lambda span: span[0])) for ticker, spans in intervals.items()},
                   dict(source or {}))

    def tickers(self, since: Optional[str] = FIRST_DATE) -> list[str]:
        """Tickers that were members on or after ``since`` (single stocks only)."""
        cutoff = pd.Timestamp(since) if since else None
        out = []
        for ticker, spans in self.intervals.items():
            if ticker in ALWAYS_MEMBERS:
                continue
            if cutoff is None or any(end is None or end > cutoff for _, end in spans):
                out.append(ticker)
        return sorted(out)

    def members_on(self, date) -> frozenset[str]:
        """Single stocks in the index on ``date`` (in on ``start``, out from ``end``)."""
        day = pd.Timestamp(date).normalize()
        return frozenset(ticker for ticker, spans in self.intervals.items()
                         if any(start <= day and (end is None or day < end) for start, end in spans))

    def mask(self, symbol: str, dates) -> np.ndarray:
        """Per-bar: ``symbol`` may be entered (ETFs always, a single stock while a member)."""
        index = pd.DatetimeIndex(pd.to_datetime(dates)).normalize()
        symbol = str(symbol).upper()
        if symbol in ALWAYS_MEMBERS:
            return np.ones(len(index), dtype=bool)
        out = np.zeros(len(index), dtype=bool)
        for start, end in self.intervals.get(symbol, ()):
            span = index >= start
            if end is not None:
                span &= index < end
            out |= np.asarray(span)
        return out

    def __call__(self, symbol: str, dates) -> np.ndarray:
        return self.mask(symbol, dates)


def fetch_membership(url: str = SOURCE_URL, timeout: float = 30.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 (fixed https source)
        return response.read().decode("utf-8")


def _stale(path: Path, now: datetime, max_age: timedelta) -> bool:
    if not path.exists():
        return True
    return now - datetime.fromtimestamp(path.stat().st_mtime, UTC) > max_age


def load_membership(*, download: bool = True, now: Optional[datetime] = None,
                    fetch: Optional[Callable[[], str]] = None, path: Optional[Path] = None) -> Membership:
    """The cached membership, refreshed from the source after ``REFRESH_AFTER`` when ``download``.

    A refresh that fails or returns an implausible file keeps the cache; with
    no cache at all it raises, so the caller falls back to today's list.
    """
    now = now or datetime.now(UTC)
    path = Path(path) if path is not None else membership_path()
    meta_path = path.with_suffix(".json")
    if download and _stale(path, now, REFRESH_AFTER):
        try:
            text = (fetch or fetch_membership)()
            frame = parse_membership(text)
            if len(frame) < MIN_SOURCE_ROWS or "AAPL" not in set(frame["ticker"]):
                raise ValueError(f"implausible membership file: {len(frame)} intervals")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            meta_path.write_text(json.dumps({
                "source": SOURCE_URL, "repository": SOURCE_REPOSITORY, "licence": SOURCE_LICENCE,
                "downloaded_at": now.isoformat(), "rows": int(len(frame)),
                "first_date": str(frame["start"].min().date()),
                "last_date": str(max(frame["start"].max(), frame["end"].max()).date()),
            }, indent=2))
        except Exception as exc:  # keep the old cache on a source failure
            logger.warning("S&P 500 membership refresh failed: %s", exc)
    if not path.exists():
        raise FileNotFoundError(f"no cached S&P 500 membership at {path}")
    source = {}
    if meta_path.exists():
        try:
            source = json.loads(meta_path.read_text())
        except ValueError:
            source = {}
    return Membership.from_frame(parse_membership(path.read_text()), source)


_current: dict = {}


def current_membership(*, download: bool = True, now: Optional[datetime] = None) -> Membership:
    """Process-cached membership (one load per hour)."""
    now = now or datetime.now(UTC)
    cached = _current.get("membership")
    loaded_at = _current.get("loaded_at")
    if cached is not None and loaded_at is not None and now - loaded_at < timedelta(hours=1):
        return cached
    membership = load_membership(download=download, now=now)
    _current.update({"membership": membership, "loaded_at": now})
    return membership


def members_on(date, membership: Optional[Membership] = None) -> frozenset[str]:
    """S&P 500 single-stock members on ``date`` (ETFs are not index members)."""
    return (membership or current_membership()).members_on(date)


def membership_mask(symbol: str, dates, membership: Optional[Membership] = None) -> np.ndarray:
    """Per-bar entry mask for ``symbol`` over ``dates``."""
    return (membership or current_membership()).mask(symbol, dates)


# -------------------------------------------------------------------- coverage

def _default_loader(download: bool) -> Callable[[str], pd.DataFrame]:
    from app.services.meta_label_learning import history_dir
    from app.services.strategy_scorecard import load_daily_history

    def load(ticker: str) -> pd.DataFrame:
        return load_daily_history(yahoo_symbol(ticker), history_dir(), download=download)

    return load


def cache_first_loader(max_age: timedelta = HISTORY_FRESH_FOR, now: Optional[datetime] = None) -> Callable:
    """History loader that downloads only when the cached CSV is missing or older than ``max_age``."""
    from app.services.meta_label_learning import history_dir
    from app.services.strategy_scorecard import load_daily_history

    now = now or datetime.now(UTC)

    def load(ticker: str) -> pd.DataFrame:
        directory = history_dir()
        symbol = yahoo_symbol(ticker)
        return load_daily_history(symbol, directory, download=_stale(directory / f"{symbol}.csv", now, max_age))

    return load


def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    """Half-open intervals ``[start, end)`` (``None`` end = open) overlap."""
    return (a_end is None or a_end > b_start) and (b_end is None or b_end > a_start)


def coverage(membership: Membership, *, loader: Optional[Callable[[str], pd.DataFrame]] = None,
             download: bool = False, now: Optional[datetime] = None, start: str = FIRST_DATE,
             cache_path: Optional[Path] = None, refresh: bool = False) -> dict:
    """Which past members still have daily history, and the yearly coverage share.

    ``loader(ticker)`` returns a daily frame (default: the shared cache,
    downloading when ``download``); one failing ticker is recorded as missing,
    never raised.  The result is cached at ``cache_path`` for ``REFRESH_AFTER``.
    """
    now = now or datetime.now(UTC)
    if cache_path is not None and not refresh and not _stale(cache_path, now, REFRESH_AFTER):
        try:
            return json.loads(cache_path.read_text())
        except ValueError:
            pass
    load = loader or _default_loader(download)
    first = pd.Timestamp(start)
    symbols: dict[str, dict] = {}
    for ticker in membership.tickers(since=start):
        try:
            frame = load(ticker)
            dates = pd.to_datetime(frame["date"]) if frame is not None and len(frame) else pd.Series(dtype="datetime64[ns]")
            if len(dates) < MIN_HISTORY_ROWS:
                symbols[ticker] = {"status": "empty", "rows": int(len(dates))}
            else:
                symbols[ticker] = {"status": "ok", "rows": int(len(dates)), "first": str(dates.min().date()),
                                   "last": str(dates.max().date())}
        except Exception as exc:  # a delisted ticker yfinance no longer serves
            symbols[ticker] = {"status": "missing", "error": f"{exc.__class__.__name__}: {exc}"[:160]}
    by_year: dict[str, dict] = {}
    for year in range(first.year, now.year + 1):
        year_start, year_end = pd.Timestamp(year=year, month=1, day=1), pd.Timestamp(year=year + 1, month=1, day=1)
        members, with_data = 0, 0
        for ticker, spans in membership.intervals.items():
            if ticker in ALWAYS_MEMBERS:
                continue
            active = [(s, e) for s, e in spans if _overlaps(s, e, year_start, year_end)]
            if not active:
                continue
            members += 1
            info = symbols.get(ticker) or {}
            if info.get("status") != "ok":
                continue
            data_start, data_end = pd.Timestamp(info["first"]), pd.Timestamp(info["last"]) + pd.Timedelta(days=1)
            if any(_overlaps(data_start, data_end, max(s, year_start), min(e, year_end) if e is not None else year_end)
                   for s, e in active):
                with_data += 1
        by_year[str(year)] = {"members": members, "with_data": with_data,
                              "coverage": round(with_data / members, 4) if members else None}
    recent = [row["coverage"] for year, row in by_year.items()
              if int(year) >= COVERAGE_FROM_YEAR and row["coverage"] is not None]
    result = {
        "checked_at": now.isoformat(), "start": start, "source": dict(membership.source),
        "by_year": by_year,
        "coverage_since_2010": round(float(np.mean(recent)), 4) if recent else None,
        "available": sorted(t for t, info in symbols.items() if info["status"] == "ok"),
        "missing": sorted(t for t, info in symbols.items() if info["status"] != "ok"),
        "symbols": symbols,
    }
    if cache_path is not None:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(result, indent=1))
        except OSError as exc:
            logger.warning("could not cache the universe coverage at %s: %s", cache_path, exc)
    return result


def replay_symbols(base_symbols: Iterable[str], coverage_result: Mapping) -> list[str]:
    """Today's list plus every past member with history (sorted, upper-case)."""
    return sorted({s.upper() for s in base_symbols} | set(coverage_result.get("available") or ()))


# ------------------------------------------------------------- replay context

@dataclass(frozen=True)
class ReplayUniverse:
    """What a point-in-time replay needs: the symbols, the mask and the budgeted loaders."""

    symbols: list[str]
    mask: UniverseMask
    loader: Callable[[str], pd.DataFrame]
    earnings_loader: Callable[[str], pd.DataFrame]
    coverage: dict

    def summary(self) -> dict:
        by_year = self.coverage.get("by_year") or {}
        return {"mode": "point_in_time", "symbols": len(self.symbols),
                "available_members": len(self.coverage.get("available") or ()),
                "missing_members": len(self.coverage.get("missing") or ()),
                "coverage_since_2010": self.coverage.get("coverage_since_2010"),
                "coverage_by_year": {year: row["coverage"] for year, row in by_year.items()},
                "source": self.coverage.get("source") or {}}


def budgeted_earnings_loader(base_symbols: Iterable[str], *, budget: int = EARNINGS_DOWNLOADS_PER_RUN,
                             now: Optional[datetime] = None) -> Callable[[str], pd.DataFrame]:
    """Earnings surprises: today's symbols refresh weekly as before; extra symbols fill ``budget`` per run."""
    from app.services import earnings_history
    from app.services.meta_label_learning import history_dir

    base = {s.upper() for s in base_symbols}
    remaining = {"downloads": int(budget)}

    def load(symbol: str) -> pd.DataFrame:
        symbol = symbol.upper()
        if symbol in base:
            return earnings_history.load_surprises(symbol, history_dir(), download=True, now=now)
        path = history_dir() / "earnings" / f"{symbol}.csv"
        download = not path.exists() and remaining["downloads"] > 0
        if download:
            remaining["downloads"] -= 1
        return earnings_history.load_surprises(symbol, history_dir(), download=download, now=now)

    return load


def replay_universe(base_symbols: Iterable[str], *, loader: Optional[Callable] = None,
                    earnings_loader: Optional[Callable] = None, download: bool = True,
                    now: Optional[datetime] = None, membership: Optional[Membership] = None,
                    coverage_result: Optional[Mapping] = None) -> ReplayUniverse:
    """The point-in-time context for one engine or discovery run.

    With the default loaders the first run downloads every past member's
    history once (the coverage check); later runs read the monthly cache and
    refresh a symbol's bars only when its CSV is older than a week.
    """
    now = now or datetime.now(UTC)
    base = sorted({s.upper() for s in base_symbols})
    membership = membership or load_membership(download=download, now=now)
    frames_loader = loader or (cache_first_loader(now=now) if download else _default_loader(False))
    if coverage_result is None:
        coverage_result = coverage(membership, loader=frames_loader, now=now,
                                   cache_path=None if loader is not None else coverage_path())
    symbols = replay_symbols(base, coverage_result)
    earnings = earnings_loader or budgeted_earnings_loader(base, now=now)
    return ReplayUniverse(symbols=symbols, mask=membership.mask, loader=frames_loader, earnings_loader=earnings,
                          coverage=dict(coverage_result))
