"""Fail-closed Tradier production completed one-minute bar ingestion."""
from __future__ import annotations

import json
import math
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.models import Asset, CorporateAction, IntradayBar
from app.services.audit import write_audit_log
from app.services.exchange_sessions import is_nyse_session, nyse_holidays, session_bounds

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
ALLOWED_SYMBOLS = frozenset({"AAPL", "MSFT", "QQQ", "SPY"})
ENTITLEMENT_VERIFICATION_MAX_AGE = timedelta(minutes=5)
BAR_CADENCE = timedelta(minutes=1)
LATE_TRADE_ALLOWANCE = timedelta(seconds=60)
INTRADAY_BACKFILL_WINDOW = timedelta(minutes=60)
POST_CLOSE_REPAIR_WINDOW = timedelta(hours=1)


MARKET_DATA_PROVIDER = settings.active_market_data_provider.strip().lower()
MARKET_DATA_FEED_CLASS = "iex" if MARKET_DATA_PROVIDER == "alpaca_iex" else "sip"
MARKET_DATA_EXECUTION_ELIGIBLE = (
    MARKET_DATA_PROVIDER == "tradier"
    or (
        MARKET_DATA_PROVIDER == "alpaca_iex"
        and settings.active_paper_broker == "alpaca_paper"
        and not settings.allow_live_trading
    )
)
TRADIER_PRODUCTION_MARKET_DATA_URL = "https://api.tradier.com/v1"


class TradierProviderError(RuntimeError):
    """A provider failure with a safe operator-visible classification."""

    def __init__(self, message: str, failure_class: str):
        super().__init__(message)
        self.failure_class = failure_class


def _previous_session(day: date) -> date:
    candidate = day - timedelta(days=1)
    while not is_nyse_session(candidate):
        candidate -= timedelta(days=1)
    return candidate


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        # SQLite drops offsets even for timezone=True columns; persisted values are UTC.
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def next_regular_session_open(now: datetime) -> datetime:
    """Return the next NYSE regular-session open after the observed time.

    The current day's open is eligible when the market has not opened yet.
    Otherwise, advance through weekends and full-day holidays until a session
    is found. The returned timestamp is timezone-aware UTC.
    """
    observed_at = _aware_utc(now)
    candidate = observed_at.astimezone(NY).date()
    bounds = session_bounds(candidate)
    if bounds is not None and observed_at < bounds[0]:
        return bounds[0]

    candidate += timedelta(days=1)
    while True:
        bounds = session_bounds(candidate)
        if bounds is not None:
            return bounds[0]
        candidate += timedelta(days=1)


def next_regular_session_gap(now: datetime, next_open: datetime) -> str | None:
    """Describe a non-session-day gap between the check and the next open."""
    start = _aware_utc(now).astimezone(NY).date()
    end = _aware_utc(next_open).astimezone(NY).date()
    if end <= start:
        return None

    gap_days = [start + timedelta(days=offset) for offset in range((end - start).days)]
    includes_weekend = any(day.weekday() >= 5 for day in gap_days)
    includes_holiday = any(
        day.weekday() < 5 and day in nyse_holidays(day.year) for day in gap_days
    )
    if includes_weekend and includes_holiday:
        return "weekend and NYSE holiday"
    if includes_weekend:
        return "weekend"
    if includes_holiday:
        return "NYSE holiday"
    return None


def _symbol(symbol: str) -> str:
    value = symbol.strip().upper()
    if value not in ALLOWED_SYMBOLS:
        raise ValueError("Intraday symbol must be one of AAPL, MSFT, QQQ, SPY")
    return value


def _parse_bar(raw: dict) -> tuple[datetime, dict]:
    if all(key in raw for key in ("t", "o", "h", "l", "c", "v")):
        raw = {
            "time": raw["t"],
            "open": raw["o"],
            "high": raw["h"],
            "low": raw["l"],
            "close": raw["c"],
            "volume": raw["v"],
        }
    required = ("open", "high", "low", "close", "volume")
    if any(key not in raw or isinstance(raw[key], bool) for key in required):
        raise ValueError("Incomplete Tradier timesales bar")
    try:
        timestamp = raw.get("time") or raw.get("timestamp")
        if timestamp is None:
            raise ValueError("missing timestamp")
        if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
            opened_at = datetime.fromtimestamp(
                float(timestamp) / (1000 if float(timestamp) > 10_000_000_000 else 1),
                tz=UTC,
            )
        else:
            opened_at = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        numbers = [float(raw[key]) for key in required]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid Tradier bar values") from exc
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=NY)
    if not all(math.isfinite(value) for value in numbers):
        raise ValueError("Non-finite Tradier bar")
    open_price, high, low, close, volume = numbers
    if min(open_price, high, low, close) <= 0 or volume < 0 or not volume.is_integer():
        raise ValueError("Invalid OHLCV")
    if high < max(open_price, close, low) or low > min(open_price, close, high):
        raise ValueError("Invalid OHLC bounds")
    return opened_at.astimezone(UTC), {
        "open": Decimal(str(raw["open"])),
        "high": Decimal(str(raw["high"])),
        "low": Decimal(str(raw["low"])),
        "close": Decimal(str(raw["close"])),
        "volume": int(volume),
    }


def _request(path: str, params: dict, attempts: int = 3) -> dict:
    configured_url = settings.tradier_market_data_url.rstrip("/")
    if configured_url != TRADIER_PRODUCTION_MARKET_DATA_URL:
        raise RuntimeError(
            "Tradier trusted market data must use the immutable production endpoint"
        )
    api_key = settings.tradier_market_data_api_key.get_secret_value()
    if not api_key:
        raise RuntimeError("Tradier production market-data credentials are not configured in workspace secrets")
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = Request(
                f"{TRADIER_PRODUCTION_MARKET_DATA_URL}{path}?{urlencode(params)}",
                headers=headers,
            )
            with urlopen(request, timeout=20) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code == 401:
                raise TradierProviderError(
                    "Tradier market-data authentication denied",
                    "authentication",
                ) from exc
            if exc.code == 403:
                raise TradierProviderError(
                    "Tradier market-data entitlement denied",
                    "entitlement",
                ) from exc
            last_error = exc
            if exc.code != 429 and exc.code < 500:
                break
        except Exception as exc:
            last_error = exc
        if attempt < attempts - 1:
            time.sleep(min(2**attempt, 4))
    raise RuntimeError(f"Tradier market-data request unavailable after {attempts} attempts: {last_error}") from last_error


def _fetch_alpaca_iex_bars(symbol: str, start: datetime, end: datetime) -> list[dict]:
    """Fetch bounded free IEX observations for the paper-only provider mode."""
    from app.services.alpaca_research_data import fetch_iex_bars

    return fetch_iex_bars([symbol], start, end).get(symbol, [])


def upsert_intraday_bars(
    db: Session,
    symbol: str,
    bars: list[dict],
    *,
    ingested_at: datetime | None = None,
    provider: str | None = None,
    feed_class: str | None = None,
) -> dict:
    # Resolve provenance at call time so a configured provider change cannot
    # leave persisted bars tagged with the provider active at import time.
    provider = provider or settings.active_market_data_provider.strip().lower()
    feed_class = feed_class or ("iex" if provider == "alpaca_iex" else "sip")
    if (provider, feed_class) not in {("tradier", "sip"), ("alpaca_iex", "iex"), ("alpaca_delayed_sip", "sip_delayed")}:
        raise ValueError("Unsupported intraday provenance")
    symbol = _symbol(symbol)
    observed_at = _aware_utc(ingested_at or datetime.now(UTC))
    parsed: list[tuple[datetime, dict]] = []
    for raw in bars:
        opened_at, values = _parse_bar(raw)
        if provider == "alpaca_delayed_sip" and opened_at + BAR_CADENCE + timedelta(minutes=16) > observed_at:
            raise ValueError("Delayed SIP bar is newer than the research cutoff")
        bounds = session_bounds(opened_at.astimezone(NY).date())
        if bounds is None or not (bounds[0] <= opened_at < bounds[1]):
            continue
        # Tradier may update a bar after the half-minute mark. Persist only after the
        # complete minute plus the selected 60-second late-trade allowance.
        if opened_at + BAR_CADENCE + LATE_TRADE_ALLOWANCE > observed_at:
            continue
        parsed.append((opened_at, values))

    timestamps = [timestamp for timestamp, _ in parsed]
    duplicate_count = len(timestamps) - len(set(timestamps))
    out_of_order = timestamps != sorted(timestamps)
    deduplicated = {timestamp: values for timestamp, values in parsed}
    existing_rows = (
        db.query(IntradayBar)
        .filter(
            IntradayBar.symbol == symbol,
            IntradayBar.timeframe == "1m",
            IntradayBar.provider == provider,
            IntradayBar.opened_at.in_(list(deduplicated)),
        )
        .all()
    )
    existing_by_timestamp = {
        _aware_utc(row.opened_at): row
        for row in existing_rows
    }
    new_rows = []
    for opened_at, values in sorted(deduplicated.items()):
        provenance = {
            **values,
            "provider": provider,
            "feed_class": feed_class,
            "exchange_timestamp": opened_at,
            "ingested_at": observed_at,
            # A successful authenticated refresh is evidence even when the
            # provider returns the same OHLCV values. Keep it separate from
            # ingested_at so research observation timestamps remain immutable.
            "last_verified_at": observed_at,
        }
        row = existing_by_timestamp.get(opened_at)
        if row:
            # Re-fetching identical research data must not erase when it became
            # known. Corrections retain the new observation time, never the old one.
            if (provider in {"alpaca_iex", "alpaca_delayed_sip"}
                    and row.feed_class == feed_class
                    and all(getattr(row, key) == value for key, value in values.items())
                    and row.exchange_timestamp is not None
                    and _aware_utc(row.exchange_timestamp) == opened_at):
                provenance.pop("ingested_at")
            for key, value in provenance.items():
                setattr(row, key, value)
        else:
            new_rows.append(
                IntradayBar(symbol=symbol, timeframe="1m", opened_at=opened_at, **provenance)
            )
    if new_rows:
        # Two scheduled collectors can legitimately fetch the same completed
        # minute at the same time.  The pre-query above avoids normal updates,
        # but it cannot prevent a race between separate PostgreSQL sessions.
        # Let the database enforce the unique key atomically so a duplicate
        # observation remains a successful idempotent ingestion.
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            values = [
                {
                    "symbol": row.symbol,
                    "timeframe": row.timeframe,
                    "opened_at": row.opened_at,
                    "open": row.open,
                    "high": row.high,
                    "low": row.low,
                    "close": row.close,
                    "volume": row.volume,
                    "provider": row.provider,
                    "feed_class": row.feed_class,
                    "exchange_timestamp": row.exchange_timestamp,
                    "ingested_at": row.ingested_at,
                    "last_verified_at": row.last_verified_at,
                }
                for row in new_rows
            ]
            statement = pg_insert(IntradayBar).values(values)
            excluded = statement.excluded
            statement = statement.on_conflict_do_update(
                constraint="uq_intraday_bar_provider",
                set_={
                    "open": excluded.open,
                    "high": excluded.high,
                    "low": excluded.low,
                    "close": excluded.close,
                    "volume": excluded.volume,
                    "feed_class": excluded.feed_class,
                    "exchange_timestamp": excluded.exchange_timestamp,
                    "ingested_at": excluded.ingested_at,
                    "last_verified_at": excluded.last_verified_at,
                },
            )
            db.execute(statement)
        else:
            db.add_all(new_rows)
    db.commit()
    return {
        "rows_imported": len(deduplicated),
        "duplicate_bars": duplicate_count,
        "out_of_order": out_of_order,
    }


def _session_missing(
    db: Session,
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    cap: int = 400,
) -> list[str]:
    rows = (
        db.query(IntradayBar.opened_at)
        .filter(
            IntradayBar.symbol == symbol,
            IntradayBar.timeframe == "1m",
            IntradayBar.provider == MARKET_DATA_PROVIDER,
            IntradayBar.opened_at >= start,
            IntradayBar.opened_at < end,
        )
        .all()
    )
    existing = {_aware_utc(item.opened_at) for item in rows}
    missing: list[str] = []
    cursor = start
    while cursor < end and len(missing) < cap:
        if cursor not in existing:
            missing.append(cursor.isoformat())
        cursor += BAR_CADENCE
    return missing


def _bounded_missing_window(
    db: Session,
    symbol: str,
    start: datetime,
    end: datetime,
) -> tuple[datetime, datetime] | None:
    """Return the next missing slice without turning one poll into a full replay."""
    missing = _session_missing(db, symbol, start, end, cap=1)
    if not missing:
        return None
    window_start = datetime.fromisoformat(missing[0])
    return window_start, min(end, window_start + INTRADAY_BACKFILL_WINDOW)


def _repair_metadata(
    missing: list[str],
    ranges: list[tuple[datetime, datetime]],
) -> tuple[dict[str, str] | None, str | None]:
    """Describe the next bounded repair slice without changing readiness semantics."""
    if not missing:
        return None, None

    oldest = missing[0]
    oldest_at = datetime.fromisoformat(oldest)
    matching_range = next(
        ((start, end) for start, end in ranges if start <= oldest_at < end),
        None,
    )
    if matching_range is None:
        return None, oldest

    _, range_end = matching_range
    deferred_end = min(range_end, oldest_at + INTRADAY_BACKFILL_WINDOW)
    return {"start": oldest, "end": deferred_end.isoformat()}, oldest


def upsert_corporate_actions(db: Session, symbol: str, actions: list[dict]) -> int:
    """Persist complete split/dividend actions; raw intraday bars remain unadjusted."""
    symbol = _symbol(symbol)
    count = 0
    for raw in actions:
        action_type = str(raw.get("type") or "").lower()
        if action_type not in {"cash_dividend", "stock_dividend", "forward_split", "reverse_split"}:
            continue
        ex_date_value = raw.get("ex_date") or raw.get("ex_date_type")
        if not ex_date_value:
            continue
        ex_date = date.fromisoformat(str(ex_date_value)[:10])
        value_raw = (
            raw.get("cash")
            or raw.get("rate")
            or raw.get("new_rate")
            or raw.get("old_rate")
            or 0
        )
        value = Decimal(str(value_raw))
        if value <= 0:
            continue
        existing = (
            db.query(CorporateAction)
            .filter_by(symbol=symbol, action_type=action_type, ex_date=ex_date, value=value)
            .one_or_none()
        )
        if not existing:
            db.add(
                CorporateAction(
                    symbol=symbol,
                    action_type=action_type,
                    ex_date=ex_date,
                    value=value,
                    provider=MARKET_DATA_PROVIDER,
                    raw_payload=raw,
                )
            )
            count += 1
    db.commit()
    return count


def ingest_corporate_actions(db: Session, symbols: list[str] | None = None) -> dict:
    selected = [_symbol(value) for value in (symbols or sorted(ALLOWED_SYMBOLS))]
    results = [
        {
            "symbol": symbol,
            "status": "unavailable",
            "failure_class": "configuration",
            "unavailable_reason": (
                "Tradier market-data integration does not provide a corporate-actions "
                "endpoint; adjustment-dependent readiness remains blocked"
            ),
        }
        for symbol in selected
    ]
    return {
        "provider": MARKET_DATA_PROVIDER,
        "adjustment_policy": "intraday_raw; daily_adjusted_close_for_training",
        "results": results,
    }


def _fetch_bars(
    symbol: str, start: datetime, end: datetime, *, max_pages: int = 4
) -> tuple[list[dict], int, bool]:
    if end <= start or end - start > INTRADAY_BACKFILL_WINDOW:
        raise ValueError("Market-data request exceeds the bounded one-hour window")
    configured_provider = settings.active_market_data_provider.strip().lower()
    if configured_provider == "alpaca_iex":
        page = _fetch_alpaca_iex_bars(symbol, start, end)
        timestamps = [item.get("t") for item in page]
        return page, len(timestamps) - len(set(timestamps)), timestamps != sorted(timestamps)
    if configured_provider != "tradier":
        raise RuntimeError("Configured market-data provider is not supported")
    params = {
        "symbol": symbol,
        "interval": "1min",
        "start": start.astimezone(NY).strftime("%Y-%m-%d %H:%M"),
        "end": end.astimezone(NY).strftime("%Y-%m-%d %H:%M"),
        "session_filter": "open",
    }
    payload = _request("/markets/timesales", params)
    series = payload.get("series") or {}
    page = series.get("data") or []
    if isinstance(page, dict):
        page = [page]
    if not isinstance(page, list):
        raise ValueError("Tradier timesales response is invalid")
    timestamps = [item.get("time") or item.get("timestamp") for item in page]
    return page, len(timestamps) - len(set(timestamps)), timestamps != sorted(timestamps)


def ingest_intraday(
    db: Session,
    symbols: list[str] | None = None,
    *,
    now: datetime | None = None,
) -> dict:
    selected = symbols or [
        asset.symbol for asset in db.query(Asset).filter(Asset.is_active.is_(True)).all()
        if asset.symbol in ALLOWED_SYMBOLS
    ]
    selected = [_symbol(value) for value in selected]
    observed_at = _aware_utc(now or datetime.now(UTC))
    local_now = observed_at.astimezone(NY)
    results = []
    for symbol in selected:
        try:
            windows: list[tuple[datetime, datetime]] = []
            target_ranges: list[tuple[datetime, datetime]] = []
            today_bounds = session_bounds(local_now.date())
            if today_bounds and observed_at >= today_bounds[0]:
                completed_through = min(
                    today_bounds[1],
                    (observed_at - BAR_CADENCE - LATE_TRADE_ALLOWANCE).replace(
                        second=0, microsecond=0
                    ) + BAR_CADENCE,
                )
                if completed_through > today_bounds[0]:
                    target_ranges.append((today_bounds[0], completed_through))
                    latest_start = max(
                        today_bounds[0],
                        completed_through - INTRADAY_BACKFILL_WINDOW,
                    )
                    # Always poll the newest completed slice so a historical
                    # repair cannot make the live feed stale.
                    windows.append((latest_start, completed_through))
                    if latest_start > today_bounds[0]:
                        backfill_window = _bounded_missing_window(
                            db, symbol, today_bounds[0], latest_start
                        )
                        if backfill_window:
                            windows.append(backfill_window)
            previous_bounds = session_bounds(_previous_session(local_now.date()))
            assert previous_bounds is not None
            if _session_missing(db, symbol, previous_bounds[0], previous_bounds[1]):
                target_ranges.append(previous_bounds)
                backfill_window = _bounded_missing_window(
                    db, symbol, previous_bounds[0], previous_bounds[1]
                )
                if backfill_window:
                    windows.append(backfill_window)
            if not windows:
                # Continuously verify the configured credentials and SIP
                # entitlement even when the latest session is already complete.
                windows.append((previous_bounds[1] - BAR_CADENCE, previous_bounds[1]))

            saved = {"rows_imported": 0, "duplicate_bars": 0, "out_of_order": False}
            provider_duplicates = 0
            provider_out_of_order = False
            missing: list[str] = []
            max_windows = 1 + settings.intraday_backfill_chunks_per_cycle
            for window_index in range(max_windows):
                if window_index >= len(windows):
                    break
                window_start, window_end = windows[window_index]
                rows, duplicates, out_of_order = _fetch_bars(symbol, window_start, window_end)
                persisted = upsert_intraday_bars(db, symbol, rows, ingested_at=observed_at)
                saved["rows_imported"] += persisted["rows_imported"]
                saved["duplicate_bars"] += persisted["duplicate_bars"]
                saved["out_of_order"] = saved["out_of_order"] or persisted["out_of_order"]
                provider_duplicates += duplicates
                provider_out_of_order = provider_out_of_order or out_of_order
                missing.extend(_session_missing(db, symbol, window_start, window_end))
                if window_index + 1 < max_windows:
                    next_window = _bounded_missing_window(
                        db, symbol, previous_bounds[0], previous_bounds[1]
                    )
                    if next_window and next_window not in windows:
                        windows.append(next_window)
            for target_start, target_end in target_ranges:
                missing.extend(_session_missing(db, symbol, target_start, target_end))
            missing = sorted(set(missing))
            deferred_window, oldest_unresolved_interval = _repair_metadata(
                missing,
                target_ranges + windows,
            )
            latest = (
                db.query(IntradayBar)
                .filter_by(
                    symbol=symbol,
                    timeframe="1m",
                    provider=MARKET_DATA_PROVIDER,
                )
                .order_by(IntradayBar.opened_at.desc())
                .first()
            )
            bar_close = _aware_utc(latest.opened_at) + BAR_CADENCE if latest else None
            results.append(
                {
                    "symbol": symbol,
                    "status": "incomplete" if missing else "complete",
                    "rows_imported": saved["rows_imported"],
                    "provider": MARKET_DATA_PROVIDER,
                    "feed_class": MARKET_DATA_FEED_CLASS,
                    "data_mode": "real-time",
                    "exchange_timestamp": _aware_utc(latest.exchange_timestamp).isoformat() if latest else None,
                    "ingestion_timestamp": observed_at.isoformat(),
                    "latency_seconds": (observed_at - bar_close).total_seconds() if bar_close else None,
                    "missing_intervals": missing,
                    "deferred_window": deferred_window,
                    "oldest_unresolved_interval": oldest_unresolved_interval,
                    "duplicate_bars": provider_duplicates + saved["duplicate_bars"],
                    "out_of_order": provider_out_of_order or saved["out_of_order"],
                    "failure_class": "incomplete_data" if missing else None,
                    "unavailable_reason": "Missing completed regular-session intervals" if missing else None,
                }
            )
        except Exception as exc:
            failure_class, reason = _provider_failure(exc)
            results.append(
                {
                    "symbol": symbol,
                    "status": "unavailable",
                    "failure_class": failure_class,
                    "missing_intervals": [],
                    "deferred_window": None,
                    "oldest_unresolved_interval": None,
                    "unavailable_reason": reason,
                }
            )
    return {
        "provider": MARKET_DATA_PROVIDER,
        "feed_class": MARKET_DATA_FEED_CLASS,
        "data_mode": "real-time",
        "cadence": "1m",
        "session": "regular",
        "adjustment_policy": "intraday_raw; daily_adjusted_close_for_training",
        "results": results,
    }


def _provider_failure(exc: Exception) -> tuple[str, str]:
    """Classify provider failures without returning provider or credential details."""
    provider = settings.active_market_data_provider.strip().lower()
    provider_label = "Tradier production" if provider == "tradier" else "Alpaca IEX"
    classified = getattr(exc, "failure_class", None)
    if classified == "authentication":
        return "authentication", f"{provider_label} market-data authentication denied"
    if classified == "entitlement":
        return "entitlement", f"{provider_label} market-data entitlement denied"
    message = str(exc).lower()
    if "credentials" in message or "not configured" in message:
        return "configuration", f"{provider_label} market-data credentials are not configured in workspace secrets"
    if "authentication" in message:
        return "authentication", f"{provider_label} market-data authentication denied"
    if "entitlement" in message:
        return "entitlement", f"Authenticated {provider_label} market-data entitlement is unavailable"
    if "tradier market-data provider" in message:
        return "configuration", f"{provider_label} market-data provider is not configured"
    if "invalid tradier" in message or "incomplete" in message or "pagination" in message:
        return "data_quality", f"{provider_label} returned invalid or incomplete bar data"
    return "availability", f"{provider_label} market-data service is unavailable"


def _record_preflight_audit(db: Session, result: dict, *, action: str = "sip_preflight") -> dict:
    """Persist only redacted feed evidence; broker credentials never enter the payload."""
    safe_results = []
    for item in result.get("results", []):
        safe_results.append(
            {
                key: item.get(key)
                for key in (
                    "symbol",
                    "status",
                    "failure_class",
                    "entitlement_state",
                    "exchange_timestamp",
                    "ingestion_timestamp",
                    "checked_at",
                    "latency_seconds",
                    "missing_intervals",
                    "deferred_window",
                    "oldest_unresolved_interval",
                    "rows_imported",
                    "unavailable_reason",
                )
                if key in item
            }
        )
    payload = {
        key: result.get(key)
        for key in (
            "provider",
            "feed_class",
            "data_mode",
            "cadence",
            "session",
            "adjustment_policy",
            "checked_at",
            "symbols",
            "status",
            "ready",
            "failure_class",
            "reason",
            "next_regular_session_open",
            "next_regular_session_gap",
            "collection_scope",
            "execution_eligible",
        )
        if key in result
    }
    payload["results"] = safe_results
    write_audit_log(
        db,
        event_type="market_data",
        action=action,
        status=result.get("status", "blocked"),
        message=result.get("reason") or "Authenticated Tradier production market-data preflight completed",
        entity_type="intraday_feed",
        payload=payload,
    )
    return result


def collect_scheduled_intraday(db: Session, symbols: list[str] | None = None, *, now: datetime | None = None) -> dict:
    """Finish session data after the close without granting trading readiness."""
    observed = _aware_utc(now or datetime.now(UTC))
    bounds = session_bounds(observed.astimezone(NY).date())
    post_close = bounds is not None and bounds[1] <= observed < bounds[1] + POST_CLOSE_REPAIR_WINDOW
    configured_provider = settings.active_market_data_provider.strip().lower()
    configured = configured_provider == MARKET_DATA_PROVIDER and bool(
        settings.tradier_market_data_api_key.get_secret_value()
        if configured_provider == "tradier"
        else settings.research_alpaca_credentials()[0]
    )
    if not post_close or not configured:
        return preflight_intraday(db, symbols, now=observed)
    selected = [_symbol(value) for value in (symbols or sorted(ALLOWED_SYMBOLS))]
    result = ingest_intraday(db, selected, now=observed)
    complete = bool(result["results"]) and all(row["status"] == "complete" for row in result["results"])
    return _record_preflight_audit(db, {
        **result, "checked_at": observed.isoformat(), "symbols": selected,
        "status": "complete" if complete else "incomplete", "ready": False,
        "execution_eligible": False, "collection_scope": "post_close_repair",
        "reason": "Post-close data repair only; regular-session preflight is required for trading",
        "next_regular_session_open": next_regular_session_open(observed).isoformat(),
    }, action="sip_post_close_collection")


def preflight_intraday(
    db: Session,
    symbols: list[str] | None = None,
    *,
    now: datetime | None = None,
) -> dict:
    """Run a bounded authenticated intraday ingestion probe and aggregate readiness.

    This deliberately requires an active NYSE regular session. Cached data from a
    prior session, delayed data, and data from another feed cannot make a resume
    preflight pass.
    """
    selected = [_symbol(value) for value in (symbols or sorted(ALLOWED_SYMBOLS))]
    observed_at = _aware_utc(now or datetime.now(UTC))
    bounds = session_bounds(observed_at.astimezone(NY).date())
    regular_session_open = bounds is not None and bounds[0] <= observed_at < bounds[1]
    next_open = None if regular_session_open else next_regular_session_open(observed_at)
    base = {
        "provider": MARKET_DATA_PROVIDER,
        "feed_class": MARKET_DATA_FEED_CLASS,
        "data_mode": "real-time",
        "cadence": "1m",
        "session": "regular",
        "adjustment_policy": "intraday_raw; daily_adjusted_close_for_training",
        "checked_at": observed_at.isoformat(),
        "symbols": selected,
        "next_regular_session_open": next_open.isoformat() if next_open else None,
        "next_regular_session_gap": (
            next_regular_session_gap(observed_at, next_open) if next_open else None
        ),
    }
    configured_provider = settings.active_market_data_provider.strip().lower()
    if configured_provider not in {"tradier", "alpaca_iex"}:
        return _record_preflight_audit(db, {
            **base,
            "status": "blocked",
            "ready": False,
            "failure_class": "configuration",
            "reason": "Configured market-data provider is not supported",
            "results": [
                {
                    "symbol": symbol,
                    "status": "unavailable",
                    "failure_class": "configuration",
                    "unavailable_reason": "Configured market-data provider is not supported",
                    "missing_intervals": [],
                    "deferred_window": None,
                    "oldest_unresolved_interval": None,
                }
                for symbol in selected
            ],
        })
    execution_eligible = (
        configured_provider == "tradier"
        or (
            configured_provider == "alpaca_iex"
            and settings.active_paper_broker == "alpaca_paper"
            and not settings.allow_live_trading
        )
    )
    if configured_provider == "alpaca_iex" and not execution_eligible:
        return _record_preflight_audit(db, {
            **base,
            "status": "blocked",
            "ready": False,
            "execution_eligible": False,
            "failure_class": "configuration",
            "reason": "Alpaca IEX is restricted to live-disabled Alpaca paper operation",
            "results": [],
        })
    market_data_key = (
        settings.tradier_market_data_api_key.get_secret_value()
        if configured_provider == "tradier"
        else settings.research_alpaca_credentials()[0]
    )
    if not market_data_key:
        return _record_preflight_audit(db, {
            **base,
            "status": "blocked",
            "ready": False,
            "failure_class": "configuration",
            "reason": "Configured market-data credentials are not available in workspace secrets",
            "results": [
                {
                    "symbol": symbol,
                    "status": "unavailable",
                    "failure_class": "configuration",
                    "unavailable_reason": "Configured market-data credentials are not available in workspace secrets",
                    "missing_intervals": [],
                    "deferred_window": None,
                    "oldest_unresolved_interval": None,
                }
                for symbol in selected
            ],
        })
    if not regular_session_open:
        reason = "Regular-session authenticated preflight is required"
        return _record_preflight_audit(db, {
            **base,
            "status": "blocked",
            "ready": False,
            "failure_class": "timing",
            "reason": reason,
            "results": [
                {
                    "symbol": symbol,
                    "status": "out_of_session",
                    "failure_class": "timing",
                    "unavailable_reason": reason,
                    "missing_intervals": [],
                    "deferred_window": None,
                    "oldest_unresolved_interval": None,
                }
                for symbol in selected
            ],
        })

    ingestion = ingest_intraday(db, selected, now=observed_at)
    ingestion_by_symbol = {item["symbol"]: item for item in ingestion["results"]}
    results = []
    for symbol in selected:
        imported = ingestion_by_symbol.get(symbol, {})
        try:
            status = feed_status(db, symbol, now=observed_at)
            result = {
                **status,
                "exchange_timestamp": (
                    status["exchange_timestamp"].isoformat()
                    if status.get("exchange_timestamp")
                    else None
                ),
                "ingestion_timestamp": (
                    status["ingestion_timestamp"].isoformat()
                    if status.get("ingestion_timestamp")
                    else None
                ),
                "checked_at": (
                    status["checked_at"].isoformat()
                    if status.get("checked_at")
                    else observed_at.isoformat()
                ),
                "rows_imported": imported.get("rows_imported", 0),
            }
            if imported.get("status") in {"unavailable", "incomplete"}:
                result["status"] = imported["status"]
                result["failure_class"] = imported.get("failure_class") or {
                    "incomplete": "incomplete_data",
                    "stale": "stale_data",
                }.get(imported["status"], "availability")
                result["unavailable_reason"] = imported.get(
                    "unavailable_reason"
                ) or "Authenticated Tradier production market-data ingestion did not complete"
                result["missing_intervals"] = imported.get(
                    "missing_intervals",
                    result.get("missing_intervals", []),
                )
                result["deferred_window"] = imported.get(
                    "deferred_window",
                    result.get("deferred_window"),
                )
                result["oldest_unresolved_interval"] = imported.get(
                    "oldest_unresolved_interval",
                    result.get("oldest_unresolved_interval"),
                )
            if result["status"] != "ready":
                result.setdefault(
                    "failure_class",
                    {
                        "incomplete": "incomplete_data",
                        "stale": "stale_data",
                    }.get(result["status"], "availability"),
                )
        except Exception as exc:
            failure_class, reason = _provider_failure(exc)
            result = {
                "symbol": symbol,
                "status": "unavailable",
                "failure_class": failure_class,
                "unavailable_reason": reason,
                "missing_intervals": imported.get("missing_intervals", []),
                "deferred_window": imported.get("deferred_window"),
                "oldest_unresolved_interval": imported.get("oldest_unresolved_interval"),
                "rows_imported": imported.get("rows_imported", 0),
            }
        results.append(result)

    failed = [item for item in results if item.get("status") != "ready"]
    return _record_preflight_audit(db, {
        **base,
        "status": "ready" if not failed else "blocked",
        "ready": not failed,
        "failure_class": failed[0].get("failure_class") if failed else None,
        "reason": None if not failed else (
            f"{failed[0]['symbol']}: "
            f"{failed[0].get('unavailable_reason') or failed[0].get('status')}"
        ),
        "results": results,
        "execution_eligible": execution_eligible,
    })


def feed_status(db: Session, symbol: str, *, now: datetime | None = None) -> dict:
    symbol = _symbol(symbol)
    observed_at = _aware_utc(now or datetime.now(UTC))
    local_now = observed_at.astimezone(NY)
    bounds = session_bounds(local_now.date())
    market_open = bounds is not None and bounds[0] <= observed_at < bounds[1] + LATE_TRADE_ALLOWANCE
    after_session = bounds is not None and observed_at >= bounds[1] + LATE_TRADE_ALLOWANCE
    target_day = local_now.date() if market_open or after_session else _previous_session(local_now.date())
    target_bounds = session_bounds(target_day)
    assert target_bounds is not None

    if market_open:
        expected_end = min(
            target_bounds[1],
            (observed_at - BAR_CADENCE - LATE_TRADE_ALLOWANCE).replace(second=0, microsecond=0)
            + BAR_CADENCE,
        )
    else:
        expected_end = target_bounds[1]
    latest = (
        db.query(IntradayBar)
        .filter(
            IntradayBar.symbol == symbol,
            IntradayBar.timeframe == "1m",
            IntradayBar.provider == MARKET_DATA_PROVIDER,
            IntradayBar.feed_class == MARKET_DATA_FEED_CLASS,
            IntradayBar.opened_at >= target_bounds[0],
            IntradayBar.opened_at < target_bounds[1],
        )
        .order_by(IntradayBar.opened_at.desc())
        .first()
    )
    missing = (
        _session_missing(db, symbol, target_bounds[0], expected_end)
        if expected_end > target_bounds[0]
        else []
    )
    configured_provider = settings.active_market_data_provider.strip().lower()
    configured = bool(
        settings.tradier_market_data_api_key.get_secret_value()
        if configured_provider == "tradier"
        else settings.research_alpaca_credentials()[0]
    )
    verification_timestamp = (
        latest.last_verified_at
        if latest is not None and latest.last_verified_at is not None
        else latest.ingested_at if latest is not None else None
    )
    verification_age = (
        observed_at - _aware_utc(verification_timestamp)
        if verification_timestamp is not None
        else None
    )
    entitlement_verified = bool(
        configured
        and verification_age is not None
        and timedelta(0) <= verification_age <= ENTITLEMENT_VERIFICATION_MAX_AGE
    )
    base = {
        "symbol": symbol,
        "provider": MARKET_DATA_PROVIDER,
        "feed_class": MARKET_DATA_FEED_CLASS,
        "data_mode": "real-time",
        "timeframe": "1m",
        "session": "regular",
        "execution_eligible": MARKET_DATA_EXECUTION_ELIGIBLE,
        "entitlement_configured": configured,
        "entitlement_state": (
            "verified" if entitlement_verified else "unverified" if configured else "not_configured"
        ),
        "exchange_timestamp": _aware_utc(latest.exchange_timestamp) if latest else None,
        "ingestion_timestamp": _aware_utc(verification_timestamp) if verification_timestamp else None,
        "latency_seconds": (
            _aware_utc(latest.ingested_at) - (_aware_utc(latest.opened_at) + BAR_CADENCE)
        ).total_seconds() if latest else None,
        "missing_intervals": missing,
        "deferred_window": (
            {
                "start": missing[0],
                "end": min(
                    expected_end,
                    datetime.fromisoformat(missing[0]) + INTRADAY_BACKFILL_WINDOW,
                ).isoformat(),
            }
            if missing
            else None
        ),
        "oldest_unresolved_interval": missing[0] if missing else None,
        "checked_at": observed_at,
        "adjustment_policy": "intraday_raw; daily_adjusted_close_for_training",
    }
    if not configured:
        return {
            **base,
            "status": "unavailable",
            "failure_class": "configuration",
            "unavailable_reason": "Configured market-data credentials are not available in workspace secrets",
        }
    if not entitlement_verified:
        return {
            **base,
            "status": "unavailable",
            "failure_class": "entitlement",
            "unavailable_reason": "Configured market-data entitlement has not been verified by a recent authenticated ingestion",
        }
    if market_open and _aware_utc(latest.opened_at) >= expected_end:
        return {
            **base,
            "status": "stale",
            "failure_class": "stale_data",
            "unavailable_reason": "Future regular-session observation cannot satisfy the current bar boundary",
        }
    if missing:
        return {
            **base,
            "status": "incomplete",
            "failure_class": "incomplete_data",
            "unavailable_reason": "Missing completed regular-session intervals",
        }
    if not latest:
        return {
            **base,
            "status": "unavailable",
            "failure_class": "availability",
            "unavailable_reason": f"No completed {MARKET_DATA_PROVIDER} bars are persisted",
        }
    if not market_open:
        return {**base, "status": "market_closed", "unavailable_reason": None}
    expected_latest_open = expected_end - BAR_CADENCE
    freshness = (expected_latest_open - _aware_utc(latest.opened_at)).total_seconds()
    if freshness > 0:
        return {
            **base,
            "status": "stale",
            "failure_class": "stale_data",
            "unavailable_reason": "Latest completed bar exceeds the selected 60-second delay limit",
        }
    return {**base, "status": "ready", "unavailable_reason": None}
