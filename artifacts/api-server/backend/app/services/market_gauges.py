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
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import logging
import re
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import httpx
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
