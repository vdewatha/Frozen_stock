"""Market-wide gauges: the VIX and the share of S&P 500 members above their 200-day average.

One symbol's trend (SPY's ADX, the regime gate) says little about the whole
market. Two gauges do better:

* ``vix``: the CBOE volatility index ("fear index"), from yfinance.
* ``breadth_above_200d``: the percent of current S&P 500 members whose last
  close is above their 200-day average. Members come from Wikipedia's list,
  prices from one yfinance batch download (~15 seconds for 500 names).

Rows go into ``economic_indicators`` (one per gauge per day), so there is no
migration. Every paper BUY records the latest gauges on its signal, and loss
diagnosis flags ``high_vix_entry`` and ``weak_breadth_entry``. Sizing on the
gauges is opt-in (``PAPER_MARKET_GAUGE_SIZING_ENABLED``): the ADX gate helped
some strategies and hurt others, so this one waits for its own backtest.

Breadth gate and breadth history
--------------------------------
The live gauge has no past, so the ``weak_breadth_entry`` lesson could not be
replayed.  ``breadth_history`` rebuilds the gauge day by day from the
point-in-time S&P 500 membership (``point_in_time_universe``) and the cached
daily bars: on each date, the members of that date with 200 completed closes,
the share above their average.  The series is cached as
``history_dir()/universe/breadth_above_200d.csv`` (``date, pct, priced,
above``) and refreshed weekly (``load_breadth_history``).  Dates with fewer
than ``MIN_PRICED_HISTORY`` priced members are left unknown (NaN), and the
replay fails open on them as the live gate does.

The live gate: a strategy row with ``parameters["breadth_gate_min"] = P``
takes no new entry while the latest breadth gauge is below P percent
(``breadth_state``).  A missing or stale gauge fails *open* (the entry goes
through and the state says so), the way the VIX gate does: the gauge is an
external feed and an outage must not switch the book off.  Adoption is the
experiment engine's ``breadth_gate`` template.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import logging
from pathlib import Path
import re
from typing import Callable, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import EconomicIndicator
from app.services.audit import write_audit_log

logger = logging.getLogger(__name__)

UTC = timezone.utc
ET = ZoneInfo("America/New_York")
VIX = "vix"
BREADTH = "breadth_above_200d"
SOURCE = "yfinance"
MEMBERS_URL = "https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&action=raw"
USER_AGENT = "frozen-stock-research/1.0 (https://github.com/vdewatha/Frozen_stock)"
MIN_MEMBERS = 450  # fewer parsed names means the page changed; do not publish a breadth
MIN_PRICED = 400
STALE_AFTER = timedelta(days=4)  # a long weekend, not a broken job
# Thresholds for loss-diagnosis flags (evidence only).
FLAG_HIGH_VIX = 25.0
FLAG_WEAK_BREADTH = 40.0
GAUGE_SIZE_MULTIPLIER = Decimal("0.5")
# Breadth gate (strategy row parameter) and breadth history (replay).
BREADTH_GATE_KEY = "breadth_gate_min"
BREADTH_WINDOW = 200
MIN_PRICED_HISTORY = 100  # fewer members with 200 closes on a date: the historical gauge is unknown there
BREADTH_HISTORY_FILE = "breadth_above_200d.csv"
BREADTH_HISTORY_FRESH_FOR = timedelta(days=7)


def sp500_members(*, client: Optional[httpx.Client] = None) -> list[str]:
    owned = client is None
    # Wikimedia refuses bare user agents; it asks for a name and a project URL.
    client = client or httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    try:
        response = client.get(MEMBERS_URL)
        response.raise_for_status()
        text = response.text
    finally:
        if owned:
            client.close()
    return parse_members(text)


def parse_members(wikitext: str) -> list[str]:
    # The members table writes {{NyseSymbol|MMM}} / {{NasdaqSymbol|AAPL}};
    # yfinance spells class shares with a dash (BRK-B).
    symbols = re.findall(r"\{\{(?:NyseSymbol|NasdaqSymbol)\|([A-Z.]+)", wikitext)
    return sorted({symbol.replace(".", "-") for symbol in symbols})


def _download(symbols: list[str], period: str) -> pd.DataFrame:
    import yfinance as yf

    return yf.download(symbols, period=period, interval="1d", auto_adjust=True, progress=False,
                       threads=True, group_by="column")


def breadth_from_closes(closes: pd.DataFrame) -> dict:
    """Percent of names above their 200-day average, on each name's last close."""
    sma = closes.rolling(200, min_periods=200).mean()
    last, last_sma = closes.ffill().iloc[-1], sma.ffill().iloc[-1]
    valid = last.notna() & last_sma.notna()
    priced = int(valid.sum())
    above = int((last[valid] > last_sma[valid]).sum())
    return {"priced": priced, "above": above, "pct": round(100 * above / priced, 2) if priced else None,
            "as_of": pd.Timestamp(closes.index[-1]).date().isoformat() if len(closes.index) else None}


def compute_gauges(*, members: Optional[Callable] = None, download: Optional[Callable] = None) -> dict:
    download = download or _download
    out: dict = {"errors": {}}
    try:
        frame = download(["^VIX"], "1mo")
        close = frame["Close"]
        series = (close["^VIX"] if isinstance(close, pd.DataFrame) else close).dropna()
        out[VIX] = {"value": round(float(series.iloc[-1]), 2),
                    "as_of": pd.Timestamp(series.index[-1]).date().isoformat(),
                    "change_5d": round(float(series.iloc[-1] - series.iloc[-6]), 2) if len(series) > 5 else None}
    except Exception as exc:
        out["errors"][VIX] = exc.__class__.__name__
    try:
        names = (members or sp500_members)()
        if len(names) < MIN_MEMBERS:
            raise ValueError(f"only {len(names)} S&P 500 members parsed")
        closes = download(names, "14mo")["Close"]
        breadth = breadth_from_closes(closes)
        if breadth["priced"] < MIN_PRICED:
            raise ValueError(f"only {breadth['priced']} members priced")
        out[BREADTH] = breadth | {"value": breadth["pct"], "members": len(names)}
    except Exception as exc:
        out["errors"][BREADTH] = str(exc) if isinstance(exc, ValueError) else exc.__class__.__name__
    return out


def refresh_market_gauges(db: Session, now: Optional[datetime] = None, **sources) -> dict:
    now = now or datetime.now(UTC)
    gauges = compute_gauges(**sources)
    stored = {}
    for name, unit in ((VIX, "index"), (BREADTH, "percent")):
        gauge = gauges.get(name)
        if not gauge or gauge.get("value") is None:
            continue
        day = date.fromisoformat(gauge["as_of"])
        row = db.query(EconomicIndicator).filter(
            EconomicIndicator.indicator_name == name, EconomicIndicator.observation_date == day).one_or_none()
        if row is None:
            row = EconomicIndicator(indicator_name=name, observation_date=day)
            db.add(row)
        row.value = Decimal(str(gauge["value"]))
        row.unit = unit
        row.source = SOURCE
        row.raw_payload = {key: value for key, value in gauge.items() if key != "value"} | {
            "observed_at": now.astimezone(UTC).isoformat()}
        stored[name] = gauge["value"]
    db.flush()
    errors = gauges["errors"]
    status = "complete" if not errors else ("partial" if stored else "failed")
    write_audit_log(
        db, event_type="market_gauges", entity_type="economic_indicator", action="refresh", status=status,
        message=", ".join(f"{name} {value}" for name, value in stored.items()) or "no gauges stored",
        payload={"stored": stored, "errors": errors},
    )
    db.commit()
    return {"status": status, "stored": stored, "errors": errors}


def latest_gauges(db: Session, now: Optional[datetime] = None) -> dict:
    """Latest value of each gauge with its date and whether it is fresh."""
    now = now or datetime.now(UTC)
    today = now.astimezone(ET).date()
    out = {}
    for name in (VIX, BREADTH):
        row = (db.query(EconomicIndicator).filter(EconomicIndicator.indicator_name == name)
               .order_by(EconomicIndicator.observation_date.desc()).first())
        if row is None or row.value is None:
            continue
        out[name] = {"value": float(row.value), "as_of": row.observation_date.isoformat(),
                     "fresh": today - row.observation_date <= STALE_AFTER}
    return out


def entry_context(gauges: dict, events: Optional[list[dict]], now: datetime) -> dict:
    """What the market looked like at entry: recorded on the BUY's signal."""
    from app.services.market_events import next_events

    vix = (gauges.get(VIX) or {}).get("value")
    breadth = (gauges.get(BREADTH) or {}).get("value")
    upcoming = next_events(events, now, limit=1)
    return {
        "vix": vix, "vix_as_of": (gauges.get(VIX) or {}).get("as_of"),
        "breadth_above_200d": breadth, "breadth_as_of": (gauges.get(BREADTH) or {}).get("as_of"),
        "next_event": upcoming[0] if upcoming else None,
        "calendar_known": events is not None,
    }


def gauge_size_scale(gauges: dict) -> dict:
    """Opt-in sizing: half size when the VIX is high or breadth is weak; never above 1x."""
    reasons = []
    vix = (gauges.get(VIX) or {})
    breadth = (gauges.get(BREADTH) or {})
    if vix.get("fresh") and vix.get("value") is not None and vix["value"] >= settings.market_gauge_high_vix:
        reasons.append(f"VIX {vix['value']:.1f} >= {settings.market_gauge_high_vix:g}")
    if breadth.get("fresh") and breadth.get("value") is not None \
            and breadth["value"] < settings.market_gauge_weak_breadth_pct:
        reasons.append(f"{breadth['value']:.0f}% of S&P 500 above 200-day < {settings.market_gauge_weak_breadth_pct:g}%")
    return {"scale": GAUGE_SIZE_MULTIPLIER if reasons else Decimal("1"), "reasons": reasons}


# ------------------------------------------------------------ breadth gate

def breadth_gate_min(parameters: Optional[Mapping]) -> float:
    """The configured breadth floor in percent; 0 (off) for a missing, invalid or non-positive value."""
    try:
        value = float((parameters or {}).get(BREADTH_GATE_KEY) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if not np.isfinite(value) or value <= 0:
        return 0.0
    return min(value, 100.0)


def breadth_state(gauges: Optional[Mapping], minimum: float) -> dict:
    """The live breadth gate's decision from ``latest_gauges``: blocked below ``minimum``, open without a gauge."""
    if not minimum or minimum <= 0:
        return {"allowed": True, "minimum": 0.0, "reason": "breadth gate off"}
    gauge = (gauges or {}).get(BREADTH) or {}
    value = gauge.get("value")
    if value is None or not gauge.get("fresh", True):
        why = "missing" if value is None else f"stale (as of {gauge.get('as_of')})"
        return {"allowed": True, "minimum": float(minimum), "value": value, "as_of": gauge.get("as_of"),
                "fail_open": True,
                "reason": f"breadth gauge {why}; the breadth gate fails open and lets the entry through"}
    value = float(value)
    allowed = value >= float(minimum)
    return {"allowed": allowed, "minimum": float(minimum), "value": value, "as_of": gauge.get("as_of"),
            "fail_open": False,
            "reason": (f"{value:.1f}% of S&P 500 members above their 200-day average "
                       f"({'at or above' if allowed else 'below'} the {float(minimum):g}% floor"
                       f"{', as of ' + str(gauge.get('as_of')) if gauge.get('as_of') else ''})")}


# --------------------------------------------------------- breadth history

def breadth_history_path() -> Path:
    from app.services.point_in_time_universe import universe_dir

    return universe_dir() / BREADTH_HISTORY_FILE


def breadth_history(membership, *, loader: Callable[[str], pd.DataFrame], start: str = "2000-01-01",
                    tickers: Optional[Iterable[str]] = None) -> pd.DataFrame:
    """Daily share of the S&P 500 members *of each date* above their 200-day average.

    ``membership`` is a ``point_in_time_universe.Membership`` (its ``mask``
    decides who counts on each date); ``loader(ticker)`` returns a daily
    frame with ``date`` and ``close`` (a ticker whose history is missing is
    skipped).  Columns: ``date``, ``pct`` (NaN when fewer than
    ``MIN_PRICED_HISTORY`` members are priced), ``priced``, ``above``.
    """
    first = pd.Timestamp(start)
    closes: dict[str, pd.Series] = {}
    for ticker in (tickers if tickers is not None else membership.tickers(since=start)):
        try:
            frame = loader(ticker)
        except Exception:  # a delisted ticker the cache never had
            continue
        if frame is None or not len(frame) or "close" not in frame or "date" not in frame:
            continue
        series = pd.Series(pd.to_numeric(frame["close"], errors="coerce").to_numpy(float),
                           index=pd.DatetimeIndex(pd.to_datetime(frame["date"])).normalize())
        series = series[~series.index.duplicated(keep="last")].sort_index()
        if series.notna().sum() < BREADTH_WINDOW:
            continue
        closes[str(ticker).upper()] = series
    if not closes:
        return pd.DataFrame(columns=["date", "pct", "priced", "above"])
    table = pd.concat(closes, axis=1).sort_index()
    sma = table.rolling(BREADTH_WINDOW, min_periods=BREADTH_WINDOW).mean()
    member = pd.DataFrame({ticker: membership.mask(ticker, table.index) for ticker in table.columns},
                          index=table.index)
    valid = table.notna() & sma.notna() & member
    above = valid & (table > sma)
    priced = valid.sum(axis=1).astype(int)
    count = above.sum(axis=1).astype(int)
    pct = np.where(priced >= MIN_PRICED_HISTORY, 100.0 * count / priced.replace(0, np.nan), np.nan)
    out = pd.DataFrame({"date": table.index.strftime("%Y-%m-%d"), "pct": np.round(pct, 2),
                        "priced": priced.to_numpy(), "above": count.to_numpy()})
    return out.loc[pd.to_datetime(out["date"]) >= first].reset_index(drop=True)


def breadth_series(history: Optional[pd.DataFrame]) -> Optional[pd.Series]:
    """``pct`` indexed by date (NaN = unknown), the shape ``gate_replay.breadth_gate_on`` takes; None if empty."""
    if history is None or not len(history) or "pct" not in history:
        return None
    series = pd.Series(pd.to_numeric(history["pct"], errors="coerce").to_numpy(float),
                       index=pd.DatetimeIndex(pd.to_datetime(history["date"])).normalize())
    return series[~series.index.duplicated(keep="last")].sort_index()


def load_breadth_history(*, now: Optional[datetime] = None, membership=None, loader: Optional[Callable] = None,
                         path: Optional[Path] = None, refresh: bool = False,
                         start: str = "2000-01-01") -> pd.DataFrame:
    """The cached breadth history, rebuilt when missing or older than ``BREADTH_HISTORY_FRESH_FOR``.

    Default inputs: the cached point-in-time membership (no download) and the
    cache-first daily history loader (no download for a cached symbol).  A
    rebuild that fails keeps the old cache; without any cache it raises, so a
    caller falls back to replaying without the gate (fail open, as live).
    """
    from app.services import point_in_time_universe as pit

    now = now or datetime.now(UTC)
    path = Path(path) if path is not None else breadth_history_path()
    stale = refresh or not path.exists() or (
        now - datetime.fromtimestamp(path.stat().st_mtime, UTC) > BREADTH_HISTORY_FRESH_FOR)
    if stale:
        try:
            membership = membership or pit.load_membership(download=False, now=now)
            history = breadth_history(membership, loader=loader or pit.cache_first_loader(now=now), start=start)
            if not len(history):
                raise ValueError("no member history to build the breadth series from")
            path.parent.mkdir(parents=True, exist_ok=True)
            history.to_csv(path, index=False)
            return history
        except Exception as exc:  # keep the old cache
            logger.warning("breadth history rebuild failed: %s", exc)
    if not path.exists():
        raise FileNotFoundError(f"no cached breadth history at {path}")
    return pd.read_csv(path)
