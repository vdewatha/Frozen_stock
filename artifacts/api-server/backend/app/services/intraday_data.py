"""Fail-closed Alpaca SIP completed one-minute bar ingestion."""
from __future__ import annotations

import json
import math
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Asset, CorporateAction, IntradayBar

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
ALLOWED_SYMBOLS = frozenset({"AAPL", "MSFT", "QQQ", "SPY"})
ENTITLEMENT_VERIFICATION_MAX_AGE = timedelta(minutes=5)
BAR_CADENCE = timedelta(minutes=1)
LATE_TRADE_ALLOWANCE = timedelta(seconds=60)
INTRADAY_BACKFILL_WINDOW = timedelta(minutes=60)


def _easter(year: int) -> date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    weekday = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * weekday) // 451
    month = (h + weekday - 7 * m + 114) // 31
    day = (h + weekday - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (occurrence - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    next_month = date(year + (month == 12), month % 12 + 1, 1)
    last = next_month - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed_fixed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def nyse_holidays(year: int) -> set[date]:
    """NYSE full-day closures for modern US equity sessions."""
    new_year = date(year, 1, 1)
    # Unlike other fixed holidays, NYSE does not close Friday for a Saturday New Year.
    observed_new_year = new_year + timedelta(days=1) if new_year.weekday() == 6 else new_year
    holidays = {
        observed_new_year,
        _nth_weekday(year, 1, 0, 3),  # MLK Day
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        _easter(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),  # Memorial Day
        _observed_fixed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed_fixed(date(year, 12, 25)),
    }
    if year >= 2022:
        holidays.add(_observed_fixed(date(year, 6, 19)))
    return holidays


def is_nyse_session(day: date) -> bool:
    return day.weekday() < 5 and day not in nyse_holidays(day.year)


def session_bounds(day: date) -> tuple[datetime, datetime] | None:
    """Return regular-session UTC bounds, including standard NYSE early closes."""
    if not is_nyse_session(day):
        return None
    close = dt_time(16, 0)
    thanksgiving = _nth_weekday(day.year, 11, 3, 4)
    if day == thanksgiving + timedelta(days=1):
        close = dt_time(13, 0)
    elif day.month == 12 and day.day == 24:
        close = dt_time(13, 0)
    elif day.month == 7 and day.day == 3 and day.weekday() < 5:
        close = dt_time(13, 0)
    opened_at = datetime.combine(day, dt_time(9, 30), NY).astimezone(UTC)
    closed_at = datetime.combine(day, close, NY).astimezone(UTC)
    return opened_at, closed_at


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


def _symbol(symbol: str) -> str:
    value = symbol.strip().upper()
    if value not in ALLOWED_SYMBOLS:
        raise ValueError("Intraday symbol must be one of AAPL, MSFT, QQQ, SPY")
    return value


def _parse_bar(raw: dict) -> tuple[datetime, dict]:
    required = ("t", "o", "h", "l", "c", "v")
    if any(key not in raw or isinstance(raw[key], bool) for key in required):
        raise ValueError("Incomplete Alpaca bar")
    try:
        opened_at = datetime.fromisoformat(str(raw["t"]).replace("Z", "+00:00"))
        numbers = [float(raw[key]) for key in ("o", "h", "l", "c", "v")]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid Alpaca bar values") from exc
    if opened_at.tzinfo is None:
        raise ValueError("Provider timestamp must be timezone aware")
    if not all(math.isfinite(value) for value in numbers):
        raise ValueError("Non-finite Alpaca bar")
    open_price, high, low, close, volume = numbers
    if min(open_price, high, low, close) <= 0 or volume < 0 or not volume.is_integer():
        raise ValueError("Invalid OHLCV")
    if high < max(open_price, close, low) or low > min(open_price, close, high):
        raise ValueError("Invalid OHLC bounds")
    return opened_at.astimezone(UTC), {
        "open": Decimal(str(raw["o"])),
        "high": Decimal(str(raw["h"])),
        "low": Decimal(str(raw["l"])),
        "close": Decimal(str(raw["c"])),
        "volume": int(volume),
    }


def _request(path: str, params: dict, attempts: int = 3) -> dict:
    api_key = settings.alpaca_api_key.get_secret_value()
    api_secret = settings.alpaca_api_secret.get_secret_value()
    if not api_key or not api_secret:
        raise RuntimeError("Alpaca credentials are not configured in workspace secrets")
    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
        "Accept": "application/json",
    }
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = Request(
                f"{settings.alpaca_data_url.rstrip('/')}{path}?{urlencode(params)}",
                headers=headers,
            )
            with urlopen(request, timeout=20) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise RuntimeError("Alpaca SIP authentication or entitlement denied") from exc
            last_error = exc
            if exc.code != 429 and exc.code < 500:
                break
        except Exception as exc:
            last_error = exc
        if attempt < attempts - 1:
            time.sleep(min(2**attempt, 4))
    raise RuntimeError(f"Alpaca request unavailable after {attempts} attempts: {last_error}") from last_error


def upsert_intraday_bars(
    db: Session,
    symbol: str,
    bars: list[dict],
    *,
    ingested_at: datetime | None = None,
) -> dict:
    symbol = _symbol(symbol)
    observed_at = _aware_utc(ingested_at or datetime.now(UTC))
    parsed: list[tuple[datetime, dict]] = []
    for raw in bars:
        opened_at, values = _parse_bar(raw)
        bounds = session_bounds(opened_at.astimezone(NY).date())
        if bounds is None or not (bounds[0] <= opened_at < bounds[1]):
            continue
        # Alpaca may update a bar after the half-minute mark. Persist only after the
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
            "provider": "alpaca",
            "feed_class": "sip",
            "exchange_timestamp": opened_at,
            "ingested_at": observed_at,
        }
        row = existing_by_timestamp.get(opened_at)
        if row:
            for key, value in provenance.items():
                setattr(row, key, value)
        else:
            new_rows.append(
                IntradayBar(symbol=symbol, timeframe="1m", opened_at=opened_at, **provenance)
            )
    if new_rows:
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
                    provider="alpaca",
                    raw_payload=raw,
                )
            )
            count += 1
    db.commit()
    return count


def ingest_corporate_actions(db: Session, symbols: list[str] | None = None) -> dict:
    selected = [_symbol(value) for value in (symbols or sorted(ALLOWED_SYMBOLS))]
    end = datetime.now(UTC).date()
    start = end - timedelta(days=370)
    results = []
    for symbol in selected:
        try:
            actions: list[dict] = []
            page_token = None
            while True:
                params = {
                    "symbols": symbol,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "region": "us",
                    "data_quality": "complete",
                    "limit": 1000,
                    "sort": "asc",
                }
                if page_token:
                    params["page_token"] = page_token
                payload = _request("/v1/corporate-actions", params)
                actions.extend(payload.get("corporate_actions", []))
                page_token = payload.get("next_page_token")
                if not page_token:
                    break
            saved = upsert_corporate_actions(db, symbol, actions)
            results.append({"symbol": symbol, "status": "complete", "rows_imported": saved})
        except Exception as exc:
            results.append({"symbol": symbol, "status": "unavailable", "unavailable_reason": str(exc)})
    return {
        "provider": "alpaca",
        "adjustment_policy": "intraday_raw; daily_adjusted_close_for_training",
        "results": results,
    }


def _fetch_bars(
    symbol: str, start: datetime, end: datetime, *, max_pages: int = 4
) -> tuple[list[dict], int, bool]:
    if settings.alpaca_feed.strip().lower() != "sip":
        raise RuntimeError("Alpaca SIP feed is not configured")
    rows: list[dict] = []
    duplicate_count = 0
    out_of_order = False
    page_token = None
    seen_tokens: set[str] = set()
    for _page_number in range(max_pages):
        params = {
            "symbols": symbol,
            "timeframe": "1Min",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "feed": "sip",
            "limit": 10000,
            "sort": "asc",
        }
        if page_token:
            params["page_token"] = page_token
        payload = _request("/v2/stocks/bars", params)
        page = payload.get("bars", {}).get(symbol, [])
        timestamps = [item.get("t") for item in page]
        out_of_order = out_of_order or timestamps != sorted(timestamps)
        duplicate_count += len(timestamps) - len(set(timestamps))
        rows.extend(page)
        page_token = payload.get("next_page_token")
        if not page_token:
            return rows, duplicate_count, out_of_order
        if page_token in seen_tokens:
            raise RuntimeError("Alpaca pagination token repeated")
        seen_tokens.add(page_token)
    raise RuntimeError(f"Alpaca pagination exceeded the bounded {max_pages}-page limit")


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
            for window_start, window_end in windows[:2]:
                rows, duplicates, out_of_order = _fetch_bars(symbol, window_start, window_end)
                persisted = upsert_intraday_bars(db, symbol, rows, ingested_at=observed_at)
                saved["rows_imported"] += persisted["rows_imported"]
                saved["duplicate_bars"] += persisted["duplicate_bars"]
                saved["out_of_order"] = saved["out_of_order"] or persisted["out_of_order"]
                provider_duplicates += duplicates
                provider_out_of_order = provider_out_of_order or out_of_order
                missing.extend(_session_missing(db, symbol, window_start, window_end))
            for target_start, target_end in target_ranges:
                missing.extend(_session_missing(db, symbol, target_start, target_end))
            missing = sorted(set(missing))
            deferred_window, oldest_unresolved_interval = _repair_metadata(
                missing,
                target_ranges + windows,
            )
            latest = (
                db.query(IntradayBar)
                .filter_by(symbol=symbol, timeframe="1m")
                .order_by(IntradayBar.opened_at.desc())
                .first()
            )
            bar_close = _aware_utc(latest.opened_at) + BAR_CADENCE if latest else None
            results.append(
                {
                    "symbol": symbol,
                    "status": "incomplete" if missing else "complete",
                    "rows_imported": saved["rows_imported"],
                    "provider": "alpaca",
                    "feed_class": "sip",
                    "data_mode": "real-time",
                    "exchange_timestamp": _aware_utc(latest.exchange_timestamp).isoformat() if latest else None,
                    "ingestion_timestamp": observed_at.isoformat(),
                    "latency_seconds": (observed_at - bar_close).total_seconds() if bar_close else None,
                    "missing_intervals": missing,
                    "deferred_window": deferred_window,
                    "oldest_unresolved_interval": oldest_unresolved_interval,
                    "duplicate_bars": provider_duplicates + saved["duplicate_bars"],
                    "out_of_order": provider_out_of_order or saved["out_of_order"],
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
        "provider": "alpaca",
        "feed_class": "sip",
        "data_mode": "real-time",
        "cadence": "1m",
        "session": "regular",
        "adjustment_policy": "intraday_raw; daily_adjusted_close_for_training",
        "results": results,
    }


def _provider_failure(exc: Exception) -> tuple[str, str]:
    """Classify provider failures without returning provider or credential details."""
    message = str(exc).lower()
    if "credentials" in message or "not configured" in message:
        return "configuration", "Alpaca credentials are not configured in workspace secrets"
    if "authentication" in message or "entitlement" in message or "sip feed" in message:
        return "authentication_or_entitlement", "Authenticated Alpaca SIP entitlement is unavailable"
    if "invalid alpaca" in message or "incomplete" in message or "pagination" in message:
        return "data_quality", "Alpaca returned invalid or incomplete bar data"
    return "availability", "Alpaca data service is unavailable"


def preflight_intraday(
    db: Session,
    symbols: list[str] | None = None,
    *,
    now: datetime | None = None,
) -> dict:
    """Run a bounded authenticated SIP ingestion probe and aggregate readiness.

    This deliberately requires an active NYSE regular session. Cached data from a
    prior session, delayed data, and data from another feed cannot make a resume
    preflight pass.
    """
    selected = [_symbol(value) for value in (symbols or sorted(ALLOWED_SYMBOLS))]
    observed_at = _aware_utc(now or datetime.now(UTC))
    bounds = session_bounds(observed_at.astimezone(NY).date())
    base = {
        "provider": "alpaca",
        "feed_class": "sip",
        "data_mode": "real-time",
        "cadence": "1m",
        "session": "regular",
        "adjustment_policy": "intraday_raw; daily_adjusted_close_for_training",
        "checked_at": observed_at.isoformat(),
        "symbols": selected,
    }
    if settings.alpaca_feed.strip().lower() != "sip":
        return {
            **base,
            "status": "blocked",
            "ready": False,
            "failure_class": "configuration",
            "reason": "Alpaca SIP feed is not configured",
            "results": [
                {
                    "symbol": symbol,
                    "status": "unavailable",
                    "failure_class": "configuration",
                    "unavailable_reason": "Alpaca SIP feed is not configured",
                    "missing_intervals": [],
                    "deferred_window": None,
                    "oldest_unresolved_interval": None,
                }
                for symbol in selected
            ],
        }
    if not (
        settings.alpaca_api_key.get_secret_value()
        and settings.alpaca_api_secret.get_secret_value()
    ):
        return {
            **base,
            "status": "blocked",
            "ready": False,
            "failure_class": "configuration",
            "reason": "Alpaca credentials are not configured in workspace secrets",
            "results": [
                {
                    "symbol": symbol,
                    "status": "unavailable",
                    "failure_class": "configuration",
                    "unavailable_reason": "Alpaca credentials are not configured in workspace secrets",
                    "missing_intervals": [],
                    "deferred_window": None,
                    "oldest_unresolved_interval": None,
                }
                for symbol in selected
            ],
        }
    if bounds is None or not (bounds[0] <= observed_at < bounds[1]):
        reason = "Regular-session authenticated preflight is required"
        return {
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
        }

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
                result["failure_class"] = imported.get(
                    "failure_class",
                    "data_quality" if imported["status"] == "incomplete" else "availability",
                )
                result["unavailable_reason"] = imported.get(
                    "unavailable_reason"
                ) or "Authenticated Alpaca SIP ingestion did not complete"
            if result["status"] != "ready":
                result.setdefault(
                    "failure_class",
                    "data_quality"
                    if result["status"] in {"incomplete", "stale"}
                    else "availability",
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
    return {
        **base,
        "status": "ready" if not failed else "blocked",
        "ready": not failed,
        "failure_class": failed[0].get("failure_class") if failed else None,
        "reason": None if not failed else (
            f"{failed[0]['symbol']}: "
            f"{failed[0].get('unavailable_reason') or failed[0].get('status')}"
        ),
        "results": results,
    }


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
            IntradayBar.provider == "alpaca",
            IntradayBar.feed_class == "sip",
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
    configured = bool(
        settings.alpaca_api_key.get_secret_value() and settings.alpaca_api_secret.get_secret_value()
    )
    verification_age = (
        observed_at - _aware_utc(latest.ingested_at)
        if latest is not None and latest.ingested_at is not None
        else None
    )
    entitlement_verified = bool(
        configured
        and verification_age is not None
        and verification_age <= ENTITLEMENT_VERIFICATION_MAX_AGE
    )
    base = {
        "symbol": symbol,
        "provider": "alpaca",
        "feed_class": "sip",
        "data_mode": "real-time",
        "timeframe": "1m",
        "session": "regular",
        "entitlement_configured": configured,
        "entitlement_state": (
            "verified" if entitlement_verified else "unverified" if configured else "not_configured"
        ),
        "exchange_timestamp": _aware_utc(latest.exchange_timestamp) if latest else None,
        "ingestion_timestamp": _aware_utc(latest.ingested_at) if latest else None,
        "latency_seconds": (
            _aware_utc(latest.ingested_at) - (_aware_utc(latest.opened_at) + BAR_CADENCE)
        ).total_seconds() if latest else None,
        "missing_intervals": missing,
        "deferred_window": (
            {
                "start": missing[0],
                "end": min(
                    target_bounds[1],
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
            "unavailable_reason": "Alpaca credentials are not configured in workspace secrets",
        }
    if not entitlement_verified:
        return {
            **base,
            "status": "unavailable",
            "unavailable_reason": "Alpaca SIP entitlement has not been verified by a recent authenticated ingestion",
        }
    if market_open and _aware_utc(latest.opened_at) >= expected_end:
        return {
            **base,
            "status": "stale",
            "unavailable_reason": "Future regular-session observation cannot satisfy the current bar boundary",
        }
    if missing:
        return {
            **base,
            "status": "incomplete",
            "unavailable_reason": "Missing completed regular-session intervals",
        }
    if not latest:
        return {
            **base,
            "status": "unavailable",
            "unavailable_reason": "No completed Alpaca SIP bars are persisted",
        }
    if not market_open:
        return {**base, "status": "market_closed", "unavailable_reason": None}
    expected_latest_open = expected_end - BAR_CADENCE
    freshness = (expected_latest_open - _aware_utc(latest.opened_at)).total_seconds()
    if freshness > 0:
        return {
            **base,
            "status": "stale",
            "unavailable_reason": "Latest completed bar exceeds the selected 60-second delay limit",
        }
    return {**base, "status": "ready", "unavailable_reason": None}