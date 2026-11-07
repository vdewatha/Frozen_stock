"""Per symbol-day news tone: the series behind the ``news_tone`` rule features.

News chose which symbols the bot looks at (``news_discovery``); nothing let
a rule *test* news tone as an entry condition.  This module turns the stored
``alpaca_news`` articles into one row per symbol and session, ``(date, tone,
articles, directional)``, and keeps a cached backfill so discovery can replay
a news rule over years, not days:

* **Tone** is the discovery scorer's reading of each article (analyst-note
  rating actions, routine notes neutral, keyword tone otherwise), averaged
  over the *directional* stories of the session, as ``rank_candidates`` does:
  a maintained rating is coverage, not a view.  A session with news but no
  directional story has tone 0.  ``articles`` counts every focused story
  (at most ``MAX_SYMBOLS_PER_ARTICLE`` named companies, as stored).
* **Session** is the first session that could trade on the story: published
  before the 16:00 ET close on a weekday, that day; later, the next weekday.
  ``attach_tone`` rolls a session that has no bar (a holiday) forward to the
  next bar, so a rule evaluated on the close of bar t sees only stories
  published before that close.  ``news_tone_1`` is the previous bar's row.
* **Backfill**.  Alpaca's news archive starts in January 2015
  (``HISTORY_START``; a 2012 query returns nothing) and is read at 50 items a
  page under a 200-requests-a-minute limit.  ``backfill`` spends a fixed
  request budget per discovery run walking backwards in weekly windows for the
  single-stock universe (one query per window with every symbol in it), and
  forwards to yesterday so the cache stays current without the database.  A
  window is recorded as covered only once every page of it was read; a 429 or
  an exhausted budget drops the partial window and the next run retries it.
  Coverage is written next to the per-symbol CSVs under
  ``history_dir()/news/`` so the report can say how far back the series goes.
* **Runtime**.  ``runtime_tone`` merges the cache with stored articles
  outside the covered span (the last day or two) through a per-process cache,
  so the paper path's rule evaluation never waits on the news API.

$0 data budget: Alpaca news is in the paper account's market-data plan.
Paper only; nothing here places an order.
"""
from __future__ import annotations

import json
import logging
import threading
import time as time_module
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
TONE_COLUMN = "news_tone"
ARTICLES_COLUMN = "news_articles"
COLUMNS = ["date", "tone", "articles", "directional"]
SESSION_CLOSE = time(16, 0)
# Alpaca's (Benzinga) archive begins here; earlier windows return no items.
HISTORY_START = date(2015, 1, 1)
WINDOW_DAYS = 7
PAGE_SIZE = 50
MAX_SYMBOLS_PER_QUERY = 40
RUNTIME_FRESH_FOR = 600.0
RUNTIME_RETRY_AFTER = 120.0
COVERAGE_FILE = "coverage.json"

_runtime: dict[str, tuple[float, Optional[pd.DataFrame]]] = {}
_pinned: dict[str, pd.DataFrame] = {}
_lock = threading.Lock()


class BackfillStopped(RuntimeError):
    """The news API refused (rate limit, auth) or the budget ran out mid-window."""


# ------------------------------------------------------------ sessions and aggregation

def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def session_day(published: datetime) -> date:
    """The first weekday session that could trade on a story published at ``published``."""
    local = _aware(published).astimezone(ET)
    day = local.date()
    if local.weekday() < 5 and local.time() < SESSION_CLOSE:
        return day
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def empty_series() -> pd.DataFrame:
    return pd.DataFrame({"date": pd.Series(dtype=str), "tone": pd.Series(dtype=float),
                         "articles": pd.Series(dtype=int), "directional": pd.Series(dtype=int)})


def aggregate_daily(stories: Iterable[tuple[datetime, float]]) -> pd.DataFrame:
    """One row per session from ``(published_at, tone)`` pairs: directional mean, counts."""
    rows = [(session_day(published).isoformat(), float(tone)) for published, tone in stories]
    if not rows:
        return empty_series()
    frame = pd.DataFrame(rows, columns=["date", "score"])
    frame["directional"] = (frame["score"] != 0).astype(int)
    frame["weighted"] = frame["score"] * frame["directional"]
    grouped = frame.groupby("date").agg(articles=("score", "size"), directional=("directional", "sum"),
                                        weighted=("weighted", "sum")).reset_index()
    grouped["tone"] = np.where(grouped["directional"] > 0, grouped["weighted"] / grouped["directional"].clip(lower=1), 0.0)
    return grouped[COLUMNS].sort_values("date").reset_index(drop=True)


def combine(*series: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Rows from several sources for the same session merged by directional weight."""
    parts = [normalize_series(part) for part in series if part is not None and len(part)]
    if not parts:
        return empty_series()
    frame = pd.concat(parts, ignore_index=True)
    frame["weighted"] = frame["tone"] * frame["directional"]
    grouped = frame.groupby("date").agg(articles=("articles", "sum"), directional=("directional", "sum"),
                                        weighted=("weighted", "sum")).reset_index()
    grouped["tone"] = np.where(grouped["directional"] > 0, grouped["weighted"] / grouped["directional"].clip(lower=1), 0.0)
    return grouped[COLUMNS].sort_values("date").reset_index(drop=True)


def normalize_series(series: Optional[pd.DataFrame]) -> pd.DataFrame:
    if series is None or len(series) == 0:
        return empty_series()
    out = pd.DataFrame({"date": series["date"].astype(str).str[:10],
                        "tone": pd.to_numeric(series["tone"], errors="coerce").fillna(0.0).astype(float),
                        "articles": pd.to_numeric(series["articles"], errors="coerce").fillna(0).astype(int)})
    out["directional"] = (pd.to_numeric(series["directional"], errors="coerce").fillna(0).astype(int)
                          if "directional" in series.columns else (out["tone"] != 0).astype(int))
    return out.sort_values("date").reset_index(drop=True)


def attach_tone(frame: pd.DataFrame, series: Optional[pd.DataFrame]) -> pd.DataFrame:
    """``frame`` with ``news_tone`` / ``news_articles`` per bar (0 where there is no news).

    A session without a bar (holiday) rolls forward to the next bar; stories
    after the frame's last bar are dropped, never pulled back.
    """
    out = frame.reset_index(drop=True).copy()
    tone = np.zeros(len(out))
    articles = np.zeros(len(out))
    series = normalize_series(series)
    if len(series) and len(out):
        bar_days = pd.to_datetime(out["date"].astype(str).str[:10]).to_numpy().astype("datetime64[D]")
        session_days = pd.to_datetime(series["date"]).to_numpy().astype("datetime64[D]")
        positions = np.searchsorted(bar_days, session_days, side="left")
        inside = positions < len(bar_days)
        if inside.any():
            weights = series["directional"].to_numpy(float)[inside]
            weighted = np.bincount(positions[inside], weights=series["tone"].to_numpy(float)[inside] * weights,
                                   minlength=len(out))
            directional = np.bincount(positions[inside], weights=weights, minlength=len(out))
            articles = np.bincount(positions[inside], weights=series["articles"].to_numpy(float)[inside],
                                   minlength=len(out))
            tone = np.where(directional > 0, weighted / np.maximum(directional, 1), 0.0)
    out[TONE_COLUMN] = tone
    out[ARTICLES_COLUMN] = articles
    return out


# ------------------------------------------------------------ stored articles

def stored_stories(db, symbol: str, *, exclude: Optional[tuple[datetime, datetime]] = None,
                   since: Optional[datetime] = None) -> list[tuple[datetime, float]]:
    """``(published_at, tone)`` of the stored ``alpaca_news`` rows, outside ``exclude`` = (start, end)."""
    from app.models import NewsArticle
    from app.services.news_discovery import SOURCE

    query = db.query(NewsArticle.published_at, NewsArticle.sentiment_score).filter(
        NewsArticle.source == SOURCE, NewsArticle.symbol == symbol.upper())
    if since is not None:
        query = query.filter(NewsArticle.published_at >= _aware(since).astimezone(UTC).replace(tzinfo=None))
    out = []
    for published, score in query.all():
        if published is None:
            continue
        stamp = _aware(published)
        if exclude is not None and exclude[0] <= stamp < exclude[1]:
            continue
        out.append((stamp, float(score or 0)))
    return out


def stored_tone_series(db, symbol: str) -> pd.DataFrame:
    """The series from stored articles alone (no cache)."""
    return aggregate_daily(stored_stories(db, symbol))


# ------------------------------------------------------------ cache

def cache_dir(data_dir: Optional[Path] = None) -> Path:
    if data_dir is None:
        from app.services.meta_label_learning import history_dir

        data_dir = history_dir()
    return Path(data_dir) / "news"


def load_coverage(data_dir: Optional[Path] = None) -> dict:
    path = cache_dir(data_dir) / COVERAGE_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _save_coverage(coverage: dict, data_dir: Optional[Path]) -> None:
    directory = cache_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / COVERAGE_FILE).write_text(json.dumps(coverage, indent=1, sort_keys=True))


def covered_span(symbol: str, data_dir: Optional[Path] = None,
                 coverage: Optional[dict] = None) -> Optional[tuple[datetime, datetime]]:
    """``(start, end)`` published-time span the cache holds for ``symbol``; None when nothing."""
    row = (coverage if coverage is not None else load_coverage(data_dir)).get(symbol.upper())
    if not row or not row.get("start") or not row.get("end"):
        return None
    return _boundary(date.fromisoformat(row["start"])), _boundary(date.fromisoformat(row["end"]))


def _boundary(day: date) -> datetime:
    """Window boundaries sit at midnight New York time, so a session is never split by the clock."""
    return datetime.combine(day, time(0, 0), tzinfo=ET).astimezone(UTC)


def cached_tone_series(symbol: str, data_dir: Optional[Path] = None) -> pd.DataFrame:
    path = cache_dir(data_dir) / f"{symbol.upper()}.csv"
    if not path.exists():
        return empty_series()
    try:
        return normalize_series(pd.read_csv(path))
    except (OSError, ValueError, KeyError) as exc:
        logger.warning("news history cache unreadable for %s: %s", symbol, exc)
        return empty_series()


def _write_series(symbol: str, series: pd.DataFrame, data_dir: Optional[Path]) -> None:
    directory = cache_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    normalize_series(series).to_csv(directory / f"{symbol.upper()}.csv", index=False)


def tone_series(symbol: str, db=None, data_dir: Optional[Path] = None) -> pd.DataFrame:
    """``DataFrame(date, tone, articles, directional)`` for ``symbol``: the cache, plus stored articles
    outside the cached span when ``db`` is given."""
    symbol = symbol.upper()
    cached = cached_tone_series(symbol, data_dir)
    if db is None:
        return cached
    span = covered_span(symbol, data_dir)
    recent = aggregate_daily(stored_stories(db, symbol, exclude=span))
    return combine(cached, recent)


# ------------------------------------------------------------ backfill from Alpaca

def _focused(article: dict) -> list[str]:
    from app.services.news_discovery import MAX_SYMBOLS_PER_ARTICLE, _article_symbols

    symbols = _article_symbols(article)
    return symbols if 1 <= len(symbols) <= MAX_SYMBOLS_PER_ARTICLE else []


def _published(article: dict) -> Optional[datetime]:
    from app.services.news_discovery import _parse

    return _parse(article.get("created_at"))


def _score(article: dict) -> float:
    from app.services.news_discovery import _article_sentiment

    return float(_article_sentiment(article))


def alpaca_news_pages(symbols: list[str], start: datetime, end: datetime, *, max_pages: int, client=None):
    """Yield pages of Alpaca news for ``symbols`` in ``[start, end]``.

    Raises ``BackfillStopped`` on a refusal (rate limit, auth, transport) or
    when ``max_pages`` pages were read and more remain.
    """
    import httpx

    from app.core.config import settings
    from app.services.news_discovery import NEWS_PATH, _iso

    key, secret = settings.research_alpaca_credentials()
    if not key or not secret:
        raise BackfillStopped("Alpaca market-data credentials are not configured")
    owned = client is None
    client = client or httpx.Client(base_url=settings.alpaca_data_url, timeout=20.0, headers={
        "APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"})
    token = None
    pages = 0
    try:
        while True:
            if pages >= max_pages:
                raise BackfillStopped("request budget exhausted inside a window")
            pages += 1
            params = {"start": _iso(start), "end": _iso(end), "symbols": ",".join(symbols), "limit": PAGE_SIZE,
                      "sort": "desc", "include_content": "false"}
            if token:
                params["page_token"] = token
            try:
                response = client.get(NEWS_PATH, params=params)
            except httpx.HTTPError as exc:
                raise BackfillStopped(f"Alpaca news request failed: {exc.__class__.__name__}") from exc
            if response.status_code == 429:
                raise BackfillStopped("Alpaca news rate limit reached")
            if response.status_code != 200:
                raise BackfillStopped(f"Alpaca news returned HTTP {response.status_code}")
            payload = response.json()
            yield [item for item in payload.get("news") or [] if isinstance(item, dict)]
            token = payload.get("next_page_token")
            if not token:
                return
    finally:
        if owned:
            client.close()


def _window_plan(symbols: list[str], coverage: dict, *, today: date, start: date) -> list[tuple[list[str], date, date]]:
    """Windows to fetch, most symbols first: a forward catch-up to yesterday, then backwards from each span."""
    forward: dict[date, list[str]] = {}
    backward: dict[date, list[str]] = {}
    for symbol in symbols:
        row = coverage.get(symbol) or {}
        if not row.get("start"):
            backward.setdefault(today, []).append(symbol)
            continue
        end = date.fromisoformat(row["end"])
        if end < today:
            forward.setdefault(end, []).append(symbol)
        first = date.fromisoformat(row["start"])
        if first > start:
            backward.setdefault(first, []).append(symbol)
    plan = []
    for end, members in sorted(forward.items(), key=lambda item: -len(item[1])):
        plan.append((members, end, min(end + timedelta(days=WINDOW_DAYS), today)))
    for first, members in sorted(backward.items(), key=lambda item: -len(item[1])):
        plan.append((members, max(first - timedelta(days=WINDOW_DAYS), start), first))
    return [(members[i:i + MAX_SYMBOLS_PER_QUERY], lo, hi)
            for members, lo, hi in plan for i in range(0, len(members), MAX_SYMBOLS_PER_QUERY)]


def _fetch_window(symbols: list[str], lo: date, hi: date, *, budget: int, fetch_pages) -> tuple[dict, int]:
    """Every page of one window; (stories per symbol, requests spent).  Raises when it cannot finish."""
    stories: dict[str, list] = {symbol: [] for symbol in symbols}
    wanted = set(symbols)
    spent = 0
    # ``end`` is inclusive in the API: stop one second before the next window's midnight.
    for page in fetch_pages(symbols, _boundary(lo), _boundary(hi) - timedelta(seconds=1), max_pages=budget):
        spent += 1
        for article in page:
            published = _published(article)
            if published is None:
                continue
            score = None
            for symbol in _focused(article):
                if symbol in wanted:
                    score = _score(article) if score is None else score
                    stories[symbol].append((published, score))
    return stories, spent


def backfill(symbols: Iterable[str], *, budget: int, data_dir: Optional[Path] = None, now: Optional[datetime] = None,
             start: date = HISTORY_START, fetch_pages: Optional[Callable] = None) -> dict:
    """Spend up to ``budget`` requests extending the cache for ``symbols``; coverage is updated per finished window."""
    symbols = sorted({str(s).upper() for s in symbols})
    now = _aware(now or datetime.now(UTC))
    today = now.astimezone(ET).date()
    coverage = load_coverage(data_dir)
    fetch_pages = fetch_pages or alpaca_news_pages
    result = {"requests": 0, "windows": 0, "stories": 0, "stopped": None, "symbols": len(symbols),
              "complete": [], "coverage": {}}
    if not symbols or budget <= 0:
        result["coverage"] = {s: coverage.get(s) for s in symbols}
        return result
    try:
        while True:
            plan = _window_plan(symbols, coverage, today=today, start=start)
            if not plan:
                break
            for members, lo, hi in plan:
                if result["requests"] >= budget:
                    raise BackfillStopped("request budget exhausted")
                stories, spent = _fetch_window(members, lo, hi, budget=budget - result["requests"],
                                               fetch_pages=fetch_pages)
                result["requests"] += spent
                result["windows"] += 1
                for symbol in members:
                    series = combine(cached_tone_series(symbol, data_dir), aggregate_daily(stories[symbol]))
                    _write_series(symbol, series, data_dir)
                    result["stories"] += len(stories[symbol])
                    row = coverage.get(symbol) or {}
                    first = min(lo, date.fromisoformat(row["start"])) if row.get("start") else lo
                    last = max(hi, date.fromisoformat(row["end"])) if row.get("end") else hi
                    coverage[symbol] = {"start": first.isoformat(), "end": last.isoformat(),
                                        "updated_at": now.isoformat(),
                                        "requests": int(row.get("requests") or 0) + spent}
                _save_coverage(coverage, data_dir)
    except BackfillStopped as exc:
        result["stopped"] = str(exc)
    except Exception as exc:  # a provider surprise must not fail the discovery job
        logger.warning("news history backfill failed: %s", exc)
        result["stopped"] = f"{exc.__class__.__name__}: {str(exc)[:160]}"
    result["complete"] = sorted(s for s in symbols if (coverage.get(s) or {}).get("start")
                                and date.fromisoformat(coverage[s]["start"]) <= start)
    result["coverage"] = {s: coverage.get(s) for s in symbols}
    return result


def coverage_summary(symbols: Iterable[str], data_dir: Optional[Path] = None) -> dict:
    """How far back the cache goes per symbol (for reports)."""
    coverage = load_coverage(data_dir)
    rows = {}
    for symbol in sorted({str(s).upper() for s in symbols}):
        row = coverage.get(symbol) or {}
        series = cached_tone_series(symbol, data_dir)
        rows[symbol] = {"start": row.get("start"), "end": row.get("end"), "sessions_with_news": int(len(series)),
                        "articles": int(series["articles"].sum()) if len(series) else 0}
    return rows


# ------------------------------------------------------------ runtime (paper path)

def set_runtime_tone(symbol: str, series: Optional[pd.DataFrame]) -> None:
    """Pin the series the paper path sees for ``symbol`` (tests); ``None`` unpins and forgets the cache."""
    with _lock:
        if series is None:
            _pinned.pop(symbol.upper(), None)
            _runtime.pop(symbol.upper(), None)
        else:
            _pinned[symbol.upper()] = normalize_series(series)


def runtime_tone(symbol: str, data_dir: Optional[Path] = None) -> Optional[pd.DataFrame]:
    """The merged series for the paper path, cached per process; ``None`` when the store is unavailable."""
    symbol = symbol.upper()
    now = time_module.monotonic()
    with _lock:
        if symbol in _pinned:
            return _pinned[symbol]
        cached = _runtime.get(symbol)
    if cached is not None:
        loaded_at, series = cached
        if now - loaded_at < (RUNTIME_FRESH_FOR if series is not None else RUNTIME_RETRY_AFTER):
            return series
    try:
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            series = tone_series(symbol, db, data_dir)
    except Exception as exc:  # the rule stays neutral; retried after RUNTIME_RETRY_AFTER
        logger.info("news history unavailable for %s: %s", symbol, exc.__class__.__name__)
        series = None
    with _lock:
        _runtime[symbol] = (now, series)
    return series


def clear_cache() -> None:
    with _lock:
        _runtime.clear()
        _pinned.clear()


# ------------------------------------------------------------ the record so far

def news_track_record(db, *, now: Optional[datetime] = None) -> dict:
    """News-found trades against core-universe trades from the attributions (the scoreboard's view),
    with the ``news_wrong`` count and the loss causes of the news-found trades."""
    from collections import Counter

    from app.services import learning_scoreboard as board
    from app.services import trade_attribution

    now = _aware(now or datetime.now(UTC))
    rows = [row for row in trade_attribution.current_attributions(db) if row.source_key.startswith("round_trip:")]
    runs = board._news_runs(db)
    ever = board._ever_admitted(runs)
    comparison = board._news_vs_core(rows, ever)
    news_rows = [row for row in rows if row.symbol in ever]
    causes = Counter(((row.detail or {}).get("diagnosis") or {}).get("cause") or "none"
                     for row in news_rows if row.is_loss)
    return {"as_of": now.isoformat(), "news_symbols_ever_admitted": sorted(ever), "discovery_runs": len(runs),
            "news": comparison["news"], "core": comparison["core"], "news_wrong": comparison["news_wrong"],
            "news_loss_causes": dict(sorted(causes.items())), "paper_only": True}


def track_record_table(record: dict) -> str:
    """The record as a Markdown table."""
    def cell(value, digits=4):
        return "-" if value is None else (f"{value:.{digits}f}" if isinstance(value, float) else str(value))

    lines = ["| Trades | Count | Wins | Win rate | Avg net return | Net P&L |", "|---|---|---|---|---|---|"]
    for label, key in (("News-found symbols", "news"), ("Core universe", "core")):
        row = record[key]
        lines.append(f"| {label} | {row['trades']} | {row['wins']} | {cell(row['win_rate'])} | "
                     f"{cell(row['avg_return'], 6)} | {cell(row['net_pnl'], 2)} |")
    lines.append(f"\n`news_wrong` losses among news-found trades: {record['news_wrong']}. "
                 f"News-found loss causes: {record['news_loss_causes'] or 'none'}.")
    return "\n".join(lines)
