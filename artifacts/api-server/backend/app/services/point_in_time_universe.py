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
  (``{"2010": {"members": n, "with_data": n, "coverage": share,
  "current_members": n, "current_with_data": n, "current_coverage": share,
  "past_members": n, "past_with_data": n, "past_coverage": share}}``; a
  *current* member still holds an open interval, a *past* member does not),
  ``coverage_since_2010`` / ``coverage_since_2010_current`` /
  ``coverage_since_2010_past`` (means of the yearly shares from 2010; the
  blended figure is a survivors' average and is never reported alone),
  ``groups`` (ticker counts per status for current and past members),
  ``available``, ``rejected`` and ``missing`` ticker lists, ``rejections``
  (``{ticker: [reason, ...]}``) and ``symbols`` (``status`` ok / rejected /
  empty / missing, ``rows``, ``first``, ``last`` and the ``identity`` record
  per ticker).  Refreshed after ``REFRESH_AFTER``.

Ticker identity: a history only counts when it is the member's own
------------------------------------------------------------------
yfinance serves a ticker's *current* instrument.  When a past member's symbol
was reused (BBT is Brookline Bancorp today, EP Empire Petroleum, CPWR an OTC
shell, GENZ a VanEck ETF, CCU a Chilean brewer's ADR) the bars on the old
member's dates belong to another company, and the 2026-11-10 coverage audit
(``reports/point-in-time-coverage-20261110.md``, section 4) found 18 such past
members that the old "60 overlapping rows" test had counted as available.
``identity_check`` therefore runs before any bar counts, on the bars that fall
on the ticker's own member days:

* at least ``MIN_MEMBER_BARS`` member-day bars (a history that starts after
  the company left is the next tenant's);
* a name match with the removal record, where the membership source or the
  caller gives one (``names``) and the data source says whose history it
  serves (``served_names``); the fja05680 file has no names, so this test is
  recorded as unchecked unless a caller supplies both;
* a dollar-volume floor: the median ``close x volume`` on member days at least
  ``MIN_MEMBER_DOLLAR_VOLUME`` (every genuine still-listed past member in the
  audit clears $2.4M; the substitutes with normal-looking bars sit at $0.1-1.2M);
* the cost-artifact test: the share of member-day bars whose one-side
  execution cost under the default ``BarrierConfig`` exceeds
  ``COST_ARTIFACT_SIDE_BPS`` (the per-bar form of the verdict-inputs rule, an
  intrabar stop exit losing more than 2 x stop + 100 bps, which needs about
  200 bps a side) must stay under ``MAX_COST_ARTIFACT_SHARE``: thin OTC prints
  push the impact term to its cap on most bars, a genuine crash on a few;
* the price-sanity test: no day-over-day adjusted close ratio outside
  ``PRICE_RATIO_BOUNDS`` on a member day that is not a split.

A rejected ticker is listed with its reasons in ``coverage()['rejections']``
and in ``ReplayUniverse.summary()['rejected_members']``; its symbol is left
out of the replay and ``ReplayUniverse.mask`` denies it on every bar.  The
check cannot recover a missing name (74% of past-member constituent-days have
no bar at all); it only stops a wrong one being used.
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
# Identity check (see the module docstring): thresholds from the 2026-11-10
# coverage audit, ``reports/point-in-time-coverage-20261110.md`` section 4.
MIN_MEMBER_BARS = MIN_HISTORY_ROWS
MIN_MEMBER_DOLLAR_VOLUME = 2_000_000.0
COST_ARTIFACT_SIDE_BPS = 200.0
MAX_COST_ARTIFACT_SHARE = 0.05
PRICE_RATIO_BOUNDS = (0.2, 5.0)
_NAME_NOISE = frozenset({
    "inc", "incorporated", "corp", "corporation", "co", "company", "companies", "ltd", "limited", "plc", "the",
    "holdings", "holding", "group", "sa", "nv", "ag", "llc", "lp", "trust", "and", "international", "intl",
})
# A ticker change inside one company: the old symbol's membership days belong
# to the symbol the daily history is cached under.
TICKER_ALIASES: dict[str, str] = {"FB": "META"}
ALWAYS_MEMBERS = frozenset(ETF_SYMBOLS)
# Earnings histories the point-in-time run fetches for the *extra* symbols per
# run (one yfinance call each); the rest fill over the following weekly runs.
EARNINGS_DOWNLOADS_PER_RUN = 60

# One daily bar is about 90 bytes of CSV; a file smaller than a few bars holds no usable history.
MIN_HISTORY_FILE_BYTES = 512

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
    # An optional company name (the fja05680 file has none; a richer source may).
    if "name" in frame.columns:
        out["name"] = frame["name"].map(lambda n: "" if pd.isna(n) else str(n).strip())
    else:
        out["name"] = ""
    out = out[out["ticker"].ne("") & out["ticker"].ne("NAN") & out["start"].notna()]
    out = out[out["end"].isna() | (out["end"] > out["start"])]
    merged = []
    for ticker, group in out.sort_values(["ticker", "start"]).groupby("ticker", sort=True):
        current_start, current_end = None, None
        name = next((n for n in group["name"] if n), "")
        for start, end in zip(group["start"], group["end"]):
            if current_start is None:
                current_start, current_end = start, end
                continue
            if pd.isna(current_end) or start <= current_end:  # touching or overlapping: extend
                if not pd.isna(current_end):
                    current_end = end if pd.isna(end) else max(current_end, end)
                continue
            merged.append((ticker, current_start, current_end, name))
            current_start, current_end = start, end
        merged.append((ticker, current_start, current_end, name))
    return pd.DataFrame(merged, columns=["ticker", "start", "end", "name"]).reset_index(drop=True)


@dataclass(frozen=True)
class Membership:
    """Membership intervals per ticker; callable as the ``universe`` mask of ``gate_replay.simulate``."""

    intervals: Mapping[str, tuple[tuple[pd.Timestamp, Optional[pd.Timestamp]], ...]]
    source: dict = field(default_factory=dict)
    # Company name per ticker where the membership source gives one (else absent).
    names: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_frame(cls, frame: pd.DataFrame, source: Optional[dict] = None) -> "Membership":
        intervals: dict[str, list] = {}
        names: dict[str, str] = {}
        for row in frame.itertuples(index=False):
            end = None if pd.isna(row.end) else pd.Timestamp(row.end)
            intervals.setdefault(str(row.ticker), []).append((pd.Timestamp(row.start), end))
            name = getattr(row, "name", "")
            if isinstance(name, str) and name and str(row.ticker) not in names:
                names[str(row.ticker)] = name
        return cls({ticker: tuple(sorted(spans, key=lambda span: span[0])) for ticker, spans in intervals.items()},
                   dict(source or {}), names)

    def is_current(self, ticker: str) -> bool:
        """``ticker`` still holds an open membership interval (a *current* member; else a *past* one)."""
        return any(end is None for _, end in self.intervals.get(str(ticker).upper(), ()))

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
        path = directory / f"{symbol}.csv"
        # A cached file with no bars (a header only) is not a history: fetch it again rather than trust it for a week.
        empty = path.exists() and path.stat().st_size < MIN_HISTORY_FILE_BYTES
        return load_daily_history(symbol, directory, download=empty or _stale(path, now, max_age))

    return load


def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    """Half-open intervals ``[start, end)`` (``None`` end = open) overlap."""
    return (a_end is None or a_end > b_start) and (b_end is None or b_end > a_start)


# ------------------------------------------------------------ identity check

def normalize_name(name) -> str:
    """Lower-case words of a company name without punctuation and legal-form noise (``Inc``, ``Corp``, ``The``)."""
    if name is None or (isinstance(name, float) and np.isnan(name)):
        return ""
    text = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in str(name).lower().replace("'", ""))
    return " ".join(word for word in text.split() if word not in _NAME_NOISE)


def names_match(expected, served) -> Optional[bool]:
    """Do two company names denote one company?  ``None`` when either is unknown.

    Normalised names are equal, one is a prefix of the other (``Wendys`` /
    ``Wendys International``), or they share the first significant word
    (``Compuware`` / ``Compuware Corp``).  ``El Paso`` against ``Empire
    Petroleum`` and ``BB&T`` against ``Brookline Bancorp`` do not match.
    """
    a, b = normalize_name(expected), normalize_name(served)
    if not a or not b:
        return None
    if a == b or a.startswith(b + " ") or b.startswith(a + " "):
        return True
    return a.split()[0] == b.split()[0]


def member_days_mask(dates, spans: Iterable[tuple], start: Optional[str] = None) -> np.ndarray:
    """Per-bar: the bar's date falls inside one of ``spans`` (and on or after ``start``)."""
    index = pd.DatetimeIndex(pd.to_datetime(dates)).normalize()
    out = np.zeros(len(index), dtype=bool)
    for span_start, span_end in spans:
        inside = index >= span_start
        if span_end is not None:
            inside &= index < span_end
        out |= np.asarray(inside)
    if start is not None:
        out &= np.asarray(index >= pd.Timestamp(start))
    return out


def identity_check(ticker: str, frame: Optional[pd.DataFrame], spans: Iterable[tuple], *, start: Optional[str] = FIRST_DATE,
                   expected_name: Optional[str] = None, served_name: Optional[str] = None,
                   min_member_bars: int = MIN_MEMBER_BARS, min_dollar_volume: float = MIN_MEMBER_DOLLAR_VOLUME,
                   cost_side_bps: float = COST_ARTIFACT_SIDE_BPS, max_cost_share: float = MAX_COST_ARTIFACT_SHARE,
                   ratio_bounds: tuple[float, float] = PRICE_RATIO_BOUNDS) -> dict:
    """Does ``frame`` (a daily history served under ``ticker``) belong to the member that held ``spans``?

    Returns ``ok`` plus every failed test in ``reasons`` and the measured
    values (``member_bars``, ``median_dollar_volume``, ``cost_artifact_share``,
    ``price_ratio_breaks``, ``name``) so the coverage cache says *why* a
    ticker was rejected.  The tests are described in the module docstring;
    the price tests run on the full contiguous series and are counted on the
    member days, so a membership gap never reads as a crash.
    """
    from app.services.triple_barrier import BarrierConfig, prior_liquidity, side_cost_bps

    spans = tuple(spans)
    reasons: list[str] = []
    name = {"expected": expected_name or None, "served": served_name or None,
            "match": names_match(expected_name, served_name)}
    if name["match"] is False:
        reasons.append(f"name_mismatch: membership record '{expected_name}', data source serves '{served_name}'")
    result = {"member_bars": 0, "median_dollar_volume": None, "cost_artifact_share": None, "price_ratio_breaks": None,
              "name": name}
    if frame is None or not len(frame) or "date" not in frame.columns or "close" not in frame.columns:
        reasons.append(f"member_bars_below_minimum: 0 bars on member days (need {min_member_bars})")
        return {"ok": False, "reasons": reasons, **result}
    bars = frame.copy()
    bars["date"] = pd.to_datetime(bars["date"])
    bars = bars.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    member = member_days_mask(bars["date"], spans, start)
    result["member_bars"] = int(member.sum())
    if result["member_bars"] < min_member_bars:
        reasons.append(f"member_bars_below_minimum: {result['member_bars']} bars on member days (need {min_member_bars})")
        return {"ok": False, "reasons": reasons, **result}
    close = pd.to_numeric(bars["close"], errors="coerce").astype(float)
    volume = pd.to_numeric(bars["volume"], errors="coerce").astype(float) if "volume" in bars.columns \
        else pd.Series(np.nan, index=bars.index)
    # Dollar-volume floor on member days (NaN volume unknown, zero volume counts as zero).
    dollar = (close * volume)[member]
    median_dollar = float(dollar.median()) if dollar.notna().any() else None
    result["median_dollar_volume"] = None if median_dollar is None else round(median_dollar, 2)
    if median_dollar is None or median_dollar < min_dollar_volume:
        shown = "unknown" if median_dollar is None else f"${median_dollar:,.0f}"
        reasons.append(f"dollar_volume_below_floor: median member-day dollar volume {shown} (floor ${min_dollar_volume:,.0f})")
    # Cost-artifact share: one-side execution cost of the default barrier order on each member-day bar.
    config = BarrierConfig()
    sigma_bps, adv = prior_liquidity(bars[["close", "volume"]] if "volume" in bars.columns else bars[["close"]],
                                     config.volatility_lookback)
    costs = np.array([side_cost_bps(float(sigma_bps[i]), float(adv[i]) if np.isfinite(adv[i]) else float("nan"),
                                    config.position_notional / close.iloc[i] if close.iloc[i] > 0 else 0.0, config)
                      for i in np.flatnonzero(member)])
    share = float(np.mean(costs > cost_side_bps)) if len(costs) else 0.0
    result["cost_artifact_share"] = round(share, 4)
    if share > max_cost_share:
        reasons.append(f"cost_artifacts: {share:.1%} of member-day bars cost over {cost_side_bps:.0f} bps a side "
                       f"(ceiling {max_cost_share:.0%})")
    # Price sanity: a day-over-day adjusted close ratio outside the bounds on a member day that is not a split.
    ratio = close / close.shift(1)
    split_column = next((c for c in bars.columns if str(c).lower() in {"stock splits", "splits", "split"}), None)
    is_split = pd.to_numeric(bars[split_column], errors="coerce").fillna(0).ne(0) if split_column is not None \
        else pd.Series(False, index=bars.index)
    low, high = ratio_bounds
    breaks = member & ratio.notna().to_numpy() & ((ratio < low) | (ratio > high)).to_numpy() & ~is_split.to_numpy()
    result["price_ratio_breaks"] = int(breaks.sum())
    if breaks.any():
        days = ", ".join(str(d.date()) for d in bars["date"][breaks].head(3))
        reasons.append(f"price_ratio_outside_bounds: {int(breaks.sum())} member-day close ratio(s) outside "
                       f"[{low}, {high}] without a split ({days})")
    return {"ok": not reasons, "reasons": reasons, **result}


def _lookup(source, ticker: str) -> Optional[str]:
    if source is None:
        return None
    if callable(source):
        try:
            value = source(ticker)
        except Exception as exc:  # a metadata lookup must never fail the coverage run
            logger.debug("name lookup failed for %s: %s", ticker, exc)
            return None
    else:
        value = source.get(ticker)
    return str(value) if value else None


# -------------------------------------------------------------------- coverage

def _coverage_row(members: int, with_data: int) -> dict:
    return {"members": members, "with_data": with_data, "coverage": round(with_data / members, 4) if members else None}


def coverage(membership: Membership, *, loader: Optional[Callable[[str], pd.DataFrame]] = None,
             download: bool = False, now: Optional[datetime] = None, start: str = FIRST_DATE,
             cache_path: Optional[Path] = None, refresh: bool = False,
             names: Optional[Mapping[str, str]] = None, served_names=None) -> dict:
    """Which members have a usable daily history of their own, and the yearly coverage, current and past apart.

    ``loader(ticker)`` returns a daily frame (default: the shared cache,
    downloading when ``download``); one failing ticker is recorded as missing,
    never raised.  A history that exists but fails ``identity_check`` (a
    reused ticker's bars) is ``rejected`` with its reasons.  ``names`` are the
    members' company names (default: those the membership source gives) and
    ``served_names`` (mapping or callable) the name the data source serves
    under each ticker; the name test runs where both are known.  The result
    is cached at ``cache_path`` for ``REFRESH_AFTER``.
    """
    now = now or datetime.now(UTC)
    if cache_path is not None and not refresh and not _stale(cache_path, now, REFRESH_AFTER):
        try:
            cached = json.loads(cache_path.read_text())
            if "rejections" in cached:  # a cache written before the identity check is recomputed
                return cached
        except ValueError:
            pass
    load = loader or _default_loader(download)
    first = pd.Timestamp(start)
    expected_names = dict(membership.names)
    expected_names.update({str(k).upper(): v for k, v in (names or {}).items()})
    symbols: dict[str, dict] = {}
    for ticker in membership.tickers(since=start):
        try:
            frame = load(ticker)
            dates = pd.to_datetime(frame["date"]) if frame is not None and len(frame) else pd.Series(dtype="datetime64[ns]")
            if len(dates) < MIN_HISTORY_ROWS:
                symbols[ticker] = {"status": "empty", "rows": int(len(dates))}
                continue
            identity = identity_check(ticker, frame, membership.intervals.get(ticker, ()), start=start,
                                      expected_name=expected_names.get(ticker), served_name=_lookup(served_names, ticker))
            info = {"status": "ok" if identity["ok"] else "rejected", "rows": int(len(dates)),
                    "first": str(dates.min().date()), "last": str(dates.max().date()),
                    "identity": {k: v for k, v in identity.items() if k not in ("ok", "reasons")}}
            if not identity["ok"]:
                info["reasons"] = identity["reasons"]
            symbols[ticker] = info
        except Exception as exc:  # a delisted ticker yfinance no longer serves
            symbols[ticker] = {"status": "missing", "error": f"{exc.__class__.__name__}: {exc}"[:160]}
    by_year: dict[str, dict] = {}
    for year in range(first.year, now.year + 1):
        year_start, year_end = pd.Timestamp(year=year, month=1, day=1), pd.Timestamp(year=year + 1, month=1, day=1)
        counts = {"current": [0, 0], "past": [0, 0]}
        for ticker, spans in membership.intervals.items():
            if ticker in ALWAYS_MEMBERS:
                continue
            active = [(s, e) for s, e in spans if _overlaps(s, e, year_start, year_end)]
            if not active:
                continue
            group = counts["current" if membership.is_current(ticker) else "past"]
            group[0] += 1
            info = symbols.get(ticker) or {}
            if info.get("status") != "ok":
                continue
            data_start, data_end = pd.Timestamp(info["first"]), pd.Timestamp(info["last"]) + pd.Timedelta(days=1)
            if any(_overlaps(data_start, data_end, max(s, year_start), min(e, year_end) if e is not None else year_end)
                   for s, e in active):
                group[1] += 1
        current, past = counts["current"], counts["past"]
        row = _coverage_row(current[0] + past[0], current[1] + past[1])
        row.update({f"current_{k}": v for k, v in _coverage_row(*current).items()})
        row.update({f"past_{k}": v for k, v in _coverage_row(*past).items()})
        by_year[str(year)] = row

    def since_2010(key: str) -> Optional[float]:
        recent = [row[key] for year, row in by_year.items() if int(year) >= COVERAGE_FROM_YEAR and row[key] is not None]
        return round(float(np.mean(recent)), 4) if recent else None

    groups = {}
    for group_name, is_group in (("current", membership.is_current), ("past", lambda t: not membership.is_current(t))):
        tickers = [t for t in symbols if is_group(t)]
        groups[group_name] = {"tickers": len(tickers),
                              **{status: sum(1 for t in tickers if symbols[t]["status"] == status)
                                 for status in ("ok", "rejected", "empty", "missing")}}
    result = {
        "checked_at": now.isoformat(), "start": start, "source": dict(membership.source),
        "by_year": by_year,
        "coverage_since_2010": since_2010("coverage"),
        "coverage_since_2010_current": since_2010("current_coverage"),
        "coverage_since_2010_past": since_2010("past_coverage"),
        "groups": groups,
        "available": sorted(t for t, info in symbols.items() if info["status"] == "ok"),
        "rejected": sorted(t for t, info in symbols.items() if info["status"] == "rejected"),
        "rejections": {t: info["reasons"] for t, info in sorted(symbols.items()) if info["status"] == "rejected"},
        "missing": sorted(t for t, info in symbols.items() if info["status"] in ("empty", "missing")),
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
    """Today's list plus every member whose own history passed the identity check (sorted, upper-case)."""
    return sorted({s.upper() for s in base_symbols} | set(coverage_result.get("available") or ()))


def rejecting_mask(membership: Membership, rejected: Iterable[str]) -> UniverseMask:
    """``membership.mask`` that also denies every bar of the ``rejected`` tickers (a reused symbol's history)."""
    denied = frozenset(str(t).upper() for t in rejected)

    def mask(symbol: str, dates) -> np.ndarray:
        if str(symbol).upper() in denied:
            return np.zeros(len(pd.DatetimeIndex(pd.to_datetime(dates))), dtype=bool)
        return membership.mask(symbol, dates)

    return mask


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
                "rejected_members": dict(self.coverage.get("rejections") or {}),
                "members": self.coverage.get("groups") or {},
                "coverage_since_2010": self.coverage.get("coverage_since_2010"),
                "coverage_since_2010_current": self.coverage.get("coverage_since_2010_current"),
                "coverage_since_2010_past": self.coverage.get("coverage_since_2010_past"),
                "coverage_by_year": {year: row["coverage"] for year, row in by_year.items()},
                "coverage_by_year_current": {year: row.get("current_coverage") for year, row in by_year.items()},
                "coverage_by_year_past": {year: row.get("past_coverage") for year, row in by_year.items()},
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
    refresh a symbol's bars only when its CSV is older than a week.  A member
    whose history failed the identity check is left out of ``symbols`` and
    denied by ``mask`` on every bar.
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
    rejected = coverage_result.get("rejected") or ()
    mask = rejecting_mask(membership, rejected) if rejected else membership.mask
    return ReplayUniverse(symbols=symbols, mask=mask, loader=frames_loader, earnings_loader=earnings,
                          coverage=dict(coverage_result))


# ------------------------------------------------------------------ liquidity
#
# The liquidity screen behind the exploration lane (``exploration_universe``)
# and ``scripts/run_liquid_universe_check.py``.  Both columns are known at the
# close of the bar they are stamped on (no look-ahead):
#
# * ``dollar_volume``: the median of ``close x volume`` over the last
#   ``LIQUIDITY_WINDOW`` bars (adjusted bars: split-adjusted volume times the
#   adjusted close, so a dividend payer's old dollar volume reads a little low);
# * ``spread``: the Corwin-Schultz (2012) high-low spread estimator, the mean
#   of the two-bar estimates over the same window, negative two-bar estimates
#   set to zero and the second bar's range shifted for the overnight gap as in
#   the paper.  It is a proxy (daily ranges also carry volatility, so it
#   overstates the quoted spread of liquid names); the lane compares it across
#   names and against a fixed ceiling, never as a cost on its own.

LIQUIDITY_WINDOW = 20
_CS_K = 3.0 - 2.0 * np.sqrt(2.0)


def corwin_schultz(frame: pd.DataFrame) -> np.ndarray:
    """Per-bar two-bar Corwin-Schultz spread estimate (bar t from bars t-1 and t; NaN on the first bar)."""
    high = pd.to_numeric(frame["high"], errors="coerce").to_numpy(float)
    low = pd.to_numeric(frame["low"], errors="coerce").to_numpy(float)
    close = pd.to_numeric(frame["close"], errors="coerce").to_numpy(float)
    out = np.full(len(high), np.nan)
    if len(high) < 2:
        return out
    h0, l0, c0 = high[:-1], low[:-1], close[:-1]
    h1, l1 = high[1:].copy(), low[1:].copy()
    # Overnight adjustment: a gap moves the second bar's whole range.
    up = l1 > c0
    down = h1 < c0
    shift = np.where(up, l1 - c0, np.where(down, h1 - c0, 0.0))
    h1, l1 = h1 - shift, l1 - shift
    with np.errstate(divide="ignore", invalid="ignore"):
        beta = np.log(h0 / l0) ** 2 + np.log(h1 / l1) ** 2
        gamma = np.log(np.maximum(h0, h1) / np.minimum(l0, l1)) ** 2
        alpha = (np.sqrt(2.0 * beta) - np.sqrt(beta)) / _CS_K - np.sqrt(gamma / _CS_K)
        spread = 2.0 * (np.exp(alpha) - 1.0) / (1.0 + np.exp(alpha))
    valid = np.isfinite(spread) & (h0 > 0) & (l0 > 0) & (h1 > 0) & (l1 > 0)
    out[1:] = np.where(valid, np.maximum(spread, 0.0), np.nan)
    return out


def liquidity_frame(frame: pd.DataFrame, window: int = LIQUIDITY_WINDOW) -> pd.DataFrame:
    """``date, close, dollar_volume, spread`` per bar (rolling ``window``; NaN until it fills)."""
    close = pd.to_numeric(frame["close"], errors="coerce").astype(float)
    volume = pd.to_numeric(frame["volume"], errors="coerce").astype(float) if "volume" in frame \
        else pd.Series(np.nan, index=frame.index)
    dollar = (close * volume).where(volume > 0)
    spread = pd.Series(corwin_schultz(frame), index=frame.index)
    return pd.DataFrame({
        "date": pd.to_datetime(frame["date"]).dt.normalize().to_numpy(),
        "close": close.to_numpy(),
        "dollar_volume": dollar.rolling(window, min_periods=window).median().to_numpy(),
        "spread": spread.rolling(window, min_periods=window).mean().to_numpy(),
    })


def liquidity_panel(frames: Mapping[str, pd.DataFrame], window: int = LIQUIDITY_WINDOW
                    ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(dollar volume, spread) as date x symbol frames over every frame's dates."""
    dollar, spread = {}, {}
    for symbol, frame in frames.items():
        if frame is None or not len(frame) or not {"high", "low", "close"} <= set(frame.columns):
            continue
        liquid = liquidity_frame(frame, window).drop_duplicates("date", keep="last").set_index("date")
        dollar[symbol], spread[symbol] = liquid["dollar_volume"], liquid["spread"]
    return pd.DataFrame(dollar).sort_index(), pd.DataFrame(spread).sort_index()


def tercile_panel(dollar_volume: pd.DataFrame, membership: Optional[Membership] = None) -> pd.DataFrame:
    """Per date and single stock: liquidity tercile 1 (most liquid) .. 3, 0 when unranked.

    Ranked among the stocks that were S&P 500 members on the date (all stocks
    when ``membership`` is None) and have a full-window dollar volume; ETFs
    are never ranked.
    """
    columns = [c for c in dollar_volume.columns if c not in ALWAYS_MEMBERS]
    panel = dollar_volume[columns]
    if membership is not None:
        member = pd.DataFrame({c: membership.mask(c, panel.index) for c in columns}, index=panel.index)
        panel = panel.where(member)
    ranks = panel.rank(axis=1, ascending=False, method="first")
    counts = panel.notna().sum(axis=1)
    share = ranks.div(counts.where(counts > 0), axis=0)
    out = np.ceil(share * 3).clip(1, 3)
    return out.fillna(0).astype(int)


def tercile_on(terciles: pd.DataFrame, symbol: str, dates) -> np.ndarray:
    """The tercile of ``symbol`` on each of ``dates`` (0 when unranked or unknown)."""
    index = pd.DatetimeIndex(pd.to_datetime(dates)).normalize()
    if symbol not in terciles.columns:
        return np.zeros(len(index), dtype=int)
    return terciles[symbol].reindex(index).fillna(0).astype(int).to_numpy()
