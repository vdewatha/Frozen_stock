"""Free IEX observations, isolated from the consolidated-feed execution contract.

Sparse IEX minutes are observations, not a complete SIP tape. No synthetic bars,
forward fills, order placement, provider activation, or readiness overrides.
"""
from datetime import datetime, timedelta, timezone

from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import AuditLog, IntradayBar
from app.services.audit import write_audit_log
from app.services.intraday_data import (
    ALLOWED_SYMBOLS, NY, _aware_utc, _parse_bar, session_bounds, upsert_intraday_bars,
)

PROVIDER = "alpaca_iex"
FEED = "iex"
UTC = timezone.utc


class BoundedStockDataClient(StockHistoricalDataClient):
    """Pin the SDK transport's timeout, retries and destination for secret safety."""

    def __init__(self):
        key, secret = settings.research_alpaca_credentials()
        if not key or not secret:
            raise RuntimeError("Alpaca paper data credentials are not configured")
        super().__init__(api_key=key, secret_key=secret, raw_data=True)
        self._retry = 0

    def _one_request(self, method, url, opts, retry):
        if method.upper() != "GET" or url != "https://data.alpaca.markets/v2/stocks/bars":
            raise RuntimeError("Unexpected market-data destination")
        return super()._one_request(
            method, url, {**opts, "timeout": (3, 12), "allow_redirects": False}, 0
        )

    def close(self):
        self._session.close()


def collection_window(now: datetime) -> tuple[datetime, datetime]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("IEX observation clock must be timezone aware")
    observed = now.astimezone(UTC)
    day = observed.astimezone(NY).date()
    for _ in range(15):
        bounds = session_bounds(day)
        # Include only full minutes with the same 60-second correction allowance.
        end = min(bounds[1], (observed - timedelta(minutes=1)).replace(second=0, microsecond=0)) if bounds else None
        if bounds and end > bounds[0]:
            return bounds[0], end
        day -= timedelta(days=1)
    raise RuntimeError("No recent exchange session found")


def fetch_iex_bars(symbols: list[str], start: datetime, end: datetime, *, client=None) -> dict[str, list[dict]]:
    return _fetch_research_bars(symbols, start, end, feed=DataFeed.IEX, client=client)


def _fetch_research_bars(symbols: list[str], start: datetime, end: datetime, *, feed: DataFeed, client=None) -> dict[str, list[dict]]:
    """Bounded shared transport; feed-specific callers enforce their time cutoff."""
    if feed not in {DataFeed.IEX, DataFeed.SIP}:
        raise ValueError("Unsupported research feed")
    if not symbols or len(set(symbols)) != len(symbols) or not set(symbols) <= ALLOWED_SYMBOLS:
        raise ValueError("IEX collection requires unique supported symbols")
    if start.tzinfo is None or end.tzinfo is None or not timedelta(0) < end - start <= timedelta(hours=7):
        raise ValueError("IEX collection requires a bounded, timezone-aware session")
    request = StockBarsRequest(
        symbol_or_symbols=symbols, timeframe=TimeFrame.Minute, start=start,
        end=end - timedelta(microseconds=1), feed=feed,
        adjustment=Adjustment.RAW, limit=10000,
    )
    params = request.to_request_fields()
    owned = client is None
    client = client or BoundedStockDataClient()
    result = {symbol: [] for symbol in symbols}
    tokens = set()
    seen = set()
    try:
        for _ in range(4):
            payload = client.get("/stocks/bars", data=params)
            if not isinstance(payload, dict) or not isinstance(payload.get("bars"), dict):
                raise ValueError("Invalid IEX bars envelope")
            if set(payload["bars"]) - set(symbols):
                raise ValueError("Unexpected IEX symbol")
            for symbol, rows in payload["bars"].items():
                if not isinstance(rows, list) or len(rows) > 10000:
                    raise ValueError("Invalid IEX bar list")
                for row in rows:
                    if not isinstance(row, dict) or not isinstance(row.get("t"), str):
                        raise ValueError("Invalid IEX bar timestamp")
                    stamp = datetime.fromisoformat(row["t"].replace("Z", "+00:00"))
                    if stamp.tzinfo is None or stamp.utcoffset() is None:
                        raise ValueError("IEX bar timestamp lacks timezone")
                    opened, _values = _parse_bar(row)
                    if not start <= opened < end or opened.second or opened.microsecond:
                        raise ValueError("IEX bar outside requested minute boundaries")
                    identity = (symbol, opened)
                    if identity in seen:
                        raise ValueError("Duplicate IEX bar across pages")
                    seen.add(identity)
                    result[symbol].append(row)
            token = payload.get("next_page_token")
            if token is None:
                return result
            if not isinstance(token, str) or not token or token in tokens:
                raise ValueError("Invalid or repeated IEX page token")
            tokens.add(token)
            params["page_token"] = token
        raise ValueError("IEX pagination budget exhausted; partial response rejected")
    finally:
        if owned:
            client.close()


def collect_iex_research(db: Session, *, now: datetime | None = None) -> dict:
    observed = now or datetime.now(UTC)
    start, end = collection_window(observed)
    symbols = sorted(ALLOWED_SYMBOLS)
    base = {
        "provider": PROVIDER, "feed_class": FEED, "research_only": True,
        "execution_eligible": False, "synthetic": False,
        "checked_at": observed.isoformat(), "window_start": start.isoformat(),
        "window_end": end.isoformat(), "adjustment": "raw",
        "coverage_policy": "Observed IEX minutes only; absent minutes remain unknown, never filled",
    }
    try:
        bars = fetch_iex_bars(symbols, start, end)
    except Exception as exc:
        # SDK exceptions can carry upstream response bodies. Never persist them.
        code = getattr(exc, "status_code", None)
        failure = "authentication" if code == 401 else "entitlement" if code == 403 else "rate_limit" if code == 429 else "unavailable_or_invalid"
        result = {**base, "status": "unavailable", "failure_class": failure, "results": []}
    else:
        results = []
        for symbol in symbols:
            saved = upsert_intraday_bars(
                db, symbol, bars[symbol], ingested_at=observed,
                provider=PROVIDER, feed_class=FEED,
            )
            results.append({"symbol": symbol, **saved})
        result = {
            **base, "status": "observed" if all(row["rows_imported"] for row in results) else "sparse_or_empty",
            "failure_class": None, "results": results,
        }
    write_audit_log(
        db, event_type="market_data", action="iex_research_collection",
        status=result["status"], entity_type="research_feed",
        message="IEX research collection; no execution eligibility granted", payload=result,
    )
    return result


def iex_research_status(db: Session, *, now: datetime | None = None) -> dict:
    from app.services.online_research import online_research_status
    observed = now or datetime.now(UTC)
    audit = db.query(AuditLog).filter_by(action="iex_research_collection").order_by(AuditLog.id.desc()).first()
    latest = audit.payload if audit else None
    symbols = []
    for symbol in sorted(ALLOWED_SYMBOLS):
        query = db.query(IntradayBar).filter_by(symbol=symbol, provider=PROVIDER, feed_class=FEED, timeframe="1m")
        bars = query.order_by(IntradayBar.opened_at.desc()).limit(60).all()
        symbols.append({
            "symbol": symbol, "total_bars": query.count(),
            "latest_bar": _aware_utc(bars[0].opened_at).isoformat() if bars else None,
            "points": [{"time": _aware_utc(bar.opened_at).isoformat(), "close": float(bar.close)} for bar in reversed(bars)],
        })
    checked = datetime.fromisoformat(latest["checked_at"]) if latest else None
    age = (observed - checked).total_seconds() if checked else None
    return {
        "provider": PROVIDER, "feed_class": FEED, "research_only": True,
        "execution_eligible": False, "enabled": settings.iex_research_enabled,
        "poll_fresh": age is not None and 0 <= age <= 120,
        "last_collection": latest, "symbols": symbols,
        "learning": online_research_status(db),
    }
