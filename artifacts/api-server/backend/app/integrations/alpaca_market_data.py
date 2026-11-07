"""Read-only Alpaca historical bars for any symbol, IEX or delayed SIP.

The free Alpaca plan serves the consolidated tape (``feed=sip``) for any
query whose end is at least 15 minutes old, and real-time data only from IEX
(about 3% of US volume).  This client is the one place feed hygiene fetches
bars from, so the rules live here:

* **Delay guard.**  A SIP query must end at least ``SIP_DELAY`` (16 minutes,
  one minute of margin) before ``now``; anything later raises
  ``DelayedSipTooRecent`` before a request is made.  Callers never get a
  real-time consolidated entitlement by accident.
* **Rate limit.**  Requests are paced ``min_interval`` apart (default 0.35 s,
  ~170/min against the plan's 200/min), a 429 is retried with exponential
  back-off, and one client stops after ``max_requests`` so a backfill cannot
  starve the rest of the app.
* **Fixed destination.**  Only ``GET https://data.alpaca.markets/v2/stocks/bars``;
  credentials travel in headers only, never in the query string.

Nothing here places, changes or reads orders.  ``transport`` is injectable so
tests run without the network.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

UTC = timezone.utc
BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
SIP_DELAY = timedelta(minutes=16)
FEEDS = frozenset({"iex", "sip"})
TIMEFRAMES = frozenset({"1Min", "1Day"})
MAX_PAGES = 20
MAX_SYMBOLS_PER_REQUEST = 50

Transport = Callable[[str, dict], dict]


class DelayedSipTooRecent(ValueError):
    """A consolidated (SIP) query ended inside the free plan's 15-minute delay."""


class MarketDataUnavailable(RuntimeError):
    """The provider could not answer (no credentials, HTTP error, bad envelope, budget spent)."""

    def __init__(self, message: str, *, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone aware")
    return value.astimezone(UTC)


def assert_sip_delay(end: datetime, now: datetime, *, delay: timedelta = SIP_DELAY) -> None:
    """Raise ``DelayedSipTooRecent`` unless ``end`` is at least ``delay`` before ``now``."""
    end, now = _aware(end, "end"), _aware(now, "now")
    if end > now - delay:
        raise DelayedSipTooRecent(
            f"delayed SIP requires a query end at least {int(delay.total_seconds() // 60)} minutes old "
            f"(end {end.isoformat()}, now {now.isoformat()})")


def latest_sip_end(now: datetime, *, delay: timedelta = SIP_DELAY) -> datetime:
    """The latest whole-minute end a SIP query may use at ``now``."""
    return (_aware(now, "now") - delay).replace(second=0, microsecond=0)


def _default_transport(url: str, headers: dict) -> dict:
    if not url.startswith(BARS_URL + "?"):
        raise MarketDataUnavailable("unexpected market-data destination")
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=20) as response:  # noqa: S310 - fixed https destination above
            return json.load(response)
    except HTTPError as exc:
        raise MarketDataUnavailable(f"alpaca bars HTTP {exc.code}", status_code=exc.code) from None


def _parse_time(raw) -> Optional[datetime]:
    if not isinstance(raw, str):
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp.astimezone(UTC) if stamp.tzinfo else None


def _number(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def parse_bar(raw: dict) -> Optional[dict]:
    """One Alpaca bar as ``{opened_at, open, high, low, close, volume, vwap, trades}``; ``None`` if invalid."""
    if not isinstance(raw, dict):
        return None
    opened = _parse_time(raw.get("t"))
    values = {key: _number(raw.get(short)) for key, short in
              (("open", "o"), ("high", "h"), ("low", "l"), ("close", "c"))}
    if opened is None or any(value is None or value <= 0 for value in values.values()):
        return None
    if values["high"] < max(values["open"], values["close"], values["low"]) or \
            values["low"] > min(values["open"], values["close"], values["high"]):
        return None
    vwap = _number(raw.get("vw"))
    return {"opened_at": opened, **values, "volume": _number(raw.get("v")) or 0.0,
            "vwap": vwap if vwap and vwap > 0 else None, "trades": _number(raw.get("n"))}


class AlpacaBarsClient:
    """Paced, bounded reader of ``/v2/stocks/bars`` (see the module docstring)."""

    def __init__(self, *, credentials: Optional[tuple[str, str]] = None, transport: Optional[Transport] = None,
                 min_interval: float = 0.35, max_requests: int = 120, max_retries: int = 3,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 now: Optional[Callable[[], datetime]] = None):
        if credentials is None:
            from app.core.config import settings

            credentials = settings.research_alpaca_credentials()
        self._key, self._secret = credentials or (None, None)
        self._transport = transport or _default_transport
        self.min_interval = float(min_interval)
        self.max_requests = int(max_requests)
        self.max_retries = int(max_retries)
        self._sleep, self._clock = sleep, clock
        self._now = now or (lambda: datetime.now(UTC))
        self._last: Optional[float] = None
        self.requests = 0
        self.retries = 0

    @property
    def configured(self) -> bool:
        return bool(self._key and self._secret)

    def _pace(self) -> None:
        if self._last is not None:
            wait = self.min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    def _get(self, params: dict) -> dict:
        if not self.configured:
            raise MarketDataUnavailable("alpaca market-data credentials are not configured")
        url = f"{BARS_URL}?{urlencode(params)}"
        headers = {"APCA-API-KEY-ID": self._key, "APCA-API-SECRET-KEY": self._secret, "Accept": "application/json"}
        attempt = 0
        while True:
            if self.requests >= self.max_requests:
                raise MarketDataUnavailable("market-data request budget for this run is spent", status_code=429)
            self._pace()
            self.requests += 1
            try:
                payload = self._transport(url, headers)
            except MarketDataUnavailable as exc:
                if exc.status_code == 429 and attempt < self.max_retries:
                    attempt += 1
                    self.retries += 1
                    self._sleep(min(2.0 ** attempt, 30.0))
                    continue
                raise
            if not isinstance(payload, dict) or not isinstance(payload.get("bars") or {}, dict):
                raise MarketDataUnavailable("invalid alpaca bars envelope")
            return payload

    def bars(self, symbols: Iterable[str], start: datetime, end: datetime, *, feed: str,
             timeframe: str = "1Min", adjustment: str = "raw") -> dict[str, list[dict]]:
        """Bars opened in ``[start, end)`` per symbol, in time order.

        ``feed="sip"`` is refused (``DelayedSipTooRecent``) unless ``end`` is at
        least 16 minutes old.  Symbols with no bars map to ``[]``.
        """
        names = sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
        if feed not in FEEDS:
            raise ValueError(f"unsupported feed {feed!r}")
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"unsupported timeframe {timeframe!r}")
        start, end = _aware(start, "start"), _aware(end, "end")
        if end <= start:
            raise ValueError("bars window must have end after start")
        if feed == "sip":
            assert_sip_delay(end, self._now())
        out: dict[str, list[dict]] = {name: [] for name in names}
        for offset in range(0, len(names), MAX_SYMBOLS_PER_REQUEST):
            chunk = names[offset:offset + MAX_SYMBOLS_PER_REQUEST]
            token: Optional[str] = None
            seen: set[str] = set()
            for _ in range(MAX_PAGES):
                params = {"symbols": ",".join(chunk), "timeframe": timeframe, "feed": feed,
                          "start": start.isoformat().replace("+00:00", "Z"),
                          "end": (end - timedelta(microseconds=1)).isoformat().replace("+00:00", "Z"),
                          "adjustment": adjustment, "limit": "10000", "sort": "asc"}
                if token:
                    params["page_token"] = token
                payload = self._get(params)
                for symbol, rows in (payload.get("bars") or {}).items():
                    if symbol not in out or not isinstance(rows, list):
                        continue
                    for row in rows:
                        bar = parse_bar(row)
                        if bar is not None and start <= bar["opened_at"] < end:
                            out[symbol].append(bar)
                token = payload.get("next_page_token")
                if not token:
                    break
                if not isinstance(token, str) or token in seen:
                    raise MarketDataUnavailable("alpaca bars pagination did not advance")
                seen.add(token)
            else:
                raise MarketDataUnavailable("alpaca bars pagination exceeded its page bound")
        for symbol, rows in out.items():
            unique = {bar["opened_at"]: bar for bar in rows}
            out[symbol] = [unique[key] for key in sorted(unique)]
        return out
