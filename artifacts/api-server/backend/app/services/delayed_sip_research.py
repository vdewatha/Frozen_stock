"""Delayed consolidated observations, never a real-time execution entitlement."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from alpaca.data.enums import DataFeed
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import AuditLog, IntradayBar
from app.services.alpaca_research_data import _fetch_research_bars, collection_window
from app.services.audit import write_audit_log
from app.services.intraday_data import (
    ALLOWED_SYMBOLS,
    _aware_utc,
    session_bounds,
    upsert_intraday_bars,
)

PROVIDER = "alpaca_delayed_sip"
FEED = "sip_delayed"
DELAY_MINUTES = 16
UTC = timezone.utc


def delayed_window(now: datetime) -> tuple[datetime, datetime]:
    # collection_window adds its own one-minute correction allowance.
    return collection_window(now - timedelta(minutes=DELAY_MINUTES - 1))


def fetch_delayed_sip_bars(symbols, start, end, *, now=None, client=None):
    observed = now or datetime.now(UTC)
    if (observed.tzinfo is None or observed.utcoffset() is None
            or end.tzinfo is None or end.utcoffset() is None
            or end > observed - timedelta(minutes=DELAY_MINUTES)):
        raise ValueError("Delayed SIP requires an end at least 16 minutes old")
    return _fetch_research_bars(symbols, start, end, feed=DataFeed.SIP, client=client)


def historical_session_window(session_day: date, *, now: datetime) -> tuple[datetime, datetime]:
    """Return one complete, bounded NYSE session that is old enough to research."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Historical delayed SIP clock must be timezone aware")
    bounds = session_bounds(session_day)
    if bounds is None:
        raise ValueError("Historical delayed SIP requires an NYSE session")
    observed = now.astimezone(UTC)
    cutoff = (observed - timedelta(minutes=DELAY_MINUTES)).replace(second=0, microsecond=0)
    end = min(bounds[1], cutoff)
    if end <= bounds[0]:
        raise ValueError("Historical delayed SIP session is not past the research cutoff")
    return bounds[0], end


def collect_delayed_sip_history(db: Session, session_day: date, *, symbols=None, now=None):
    """Backfill real delayed SIP bars for one bounded historical session.

    Delayed SIP provenance stays isolated from the execution feed, and missing
    minutes remain missing rather than being filled.
    """
    observed = now or datetime.now(UTC)
    selected = sorted(symbols or {"QQQ"})
    if not selected or not set(selected) <= ALLOWED_SYMBOLS or len(set(selected)) != len(selected):
        raise ValueError("Historical delayed SIP symbols must be unique supported symbols")
    start, end = historical_session_window(session_day, now=observed)
    base = {
        "provider": PROVIDER, "feed_class": FEED, "upstream_feed": "sip",
        "research_only": True, "execution_eligible": False, "synthetic": False,
        "delay_minutes": DELAY_MINUTES, "adjustment": "raw",
        "checked_at": observed.isoformat(), "session_day": session_day.isoformat(),
        "window_start": start.isoformat(), "window_end": end.isoformat(),
        "coverage_policy": "Observed delayed consolidated minutes; missing minutes remain unknown",
    }
    try:
        bars = fetch_delayed_sip_bars(selected, start, end, now=observed)
    except Exception as exc:
        code = getattr(exc, "status_code", None)
        failure = {401: "authentication", 403: "entitlement", 422: "invalid_request_or_entitlement", 429: "rate_limit"}.get(
            code, "unavailable_or_invalid"
        )
        return {**base, "status": "unavailable", "failure_class": failure, "results": []}

    results = [{
        "symbol": symbol,
        **upsert_intraday_bars(
            db, symbol, bars.get(symbol, []), ingested_at=observed,
            provider=PROVIDER, feed_class=FEED,
        ),
    } for symbol in selected]
    return {
        **base,
        "status": "observed" if all(row["rows_imported"] for row in results) else "sparse_or_empty",
        "failure_class": None,
        "results": results,
    }


def collect_delayed_sip(db: Session, *, now=None):
    observed = now or datetime.now(UTC)
    start, end = delayed_window(observed)
    symbols = sorted(ALLOWED_SYMBOLS)
    base = {"provider": PROVIDER, "feed_class": FEED, "upstream_feed": "sip",
            "research_only": True, "execution_eligible": False, "synthetic": False,
            "delay_minutes": DELAY_MINUTES, "adjustment": "raw",
            "checked_at": observed.isoformat(), "window_start": start.isoformat(), "window_end": end.isoformat(),
            "coverage_policy": "Observed delayed consolidated minutes; missing minutes remain unknown"}
    try:
        bars = fetch_delayed_sip_bars(symbols, start, end, now=observed)
    except Exception as exc:
        code = getattr(exc, "status_code", None)
        failure = {401: "authentication", 403: "entitlement", 422: "invalid_request_or_entitlement", 429: "rate_limit"}.get(code, "unavailable_or_invalid")
        result = {**base, "status": "unavailable", "failure_class": failure, "results": []}
    else:
        results = [{"symbol": symbol, **upsert_intraday_bars(
            db, symbol, bars[symbol], ingested_at=observed, provider=PROVIDER, feed_class=FEED,
        )} for symbol in symbols]
        result = {**base, "status": "observed" if all(row["rows_imported"] for row in results) else "sparse_or_empty",
                  "failure_class": None, "results": results}
    write_audit_log(db, event_type="market_data", action="delayed_sip_collection", status=result["status"],
                    entity_type="research_feed", message="Delayed SIP research collection; not execution data", payload=result)
    return result


def delayed_sip_status(db: Session, *, now=None):
    observed = now or datetime.now(UTC)
    audit = db.query(AuditLog).filter_by(action="delayed_sip_collection").order_by(AuditLog.id.desc()).first()
    latest = audit.payload if audit else None
    start, end = ((datetime.fromisoformat(latest["window_start"]), datetime.fromisoformat(latest["window_end"]))
                  if latest else delayed_window(observed))
    symbols = []
    for symbol in sorted(ALLOWED_SYMBOLS):
        query = db.query(IntradayBar).filter_by(symbol=symbol, provider=PROVIDER, feed_class=FEED, timeframe="1m")
        latest_bar = query.order_by(IntradayBar.opened_at.desc()).first()
        sip = {_aware_utc(row.opened_at): row.close for row in query.filter(IntradayBar.opened_at >= start, IntradayBar.opened_at < end)}
        iex = {_aware_utc(row.opened_at): row.close for row in db.query(IntradayBar).filter_by(
            symbol=symbol, provider="alpaca_iex", feed_class="iex", timeframe="1m",
        ).filter(IntradayBar.opened_at >= start, IntradayBar.opened_at < end)}
        matched = sip.keys() & iex.keys()
        gaps = [abs(sip[key] - iex[key]) / sip[key] * Decimal(10000) for key in matched]
        symbols.append({"symbol": symbol, "total_bars": query.count(), "window_bars": len(sip),
                        "iex_window_bars": len(iex), "matched_minutes": len(matched),
                        "mean_abs_close_gap_bps": round(float(sum(gaps) / len(gaps)), 4) if gaps else None,
                        "latest_bar": _aware_utc(latest_bar.opened_at).isoformat() if latest_bar else None})
    age = (observed - datetime.fromisoformat(latest["checked_at"])).total_seconds() if latest else None
    return {"provider": PROVIDER, "feed_class": FEED, "delay_minutes": DELAY_MINUTES,
            "enabled": settings.delayed_sip_research_enabled, "poll_fresh": age is not None and 0 <= age <= 600,
            "research_only": True, "execution_eligible": False, "last_collection": latest,
            "comparison_window_start": start.isoformat(), "comparison_window_end": end.isoformat(), "symbols": symbols}
