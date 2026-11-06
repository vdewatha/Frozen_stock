"""News-driven symbol discovery for paper learning trades.

The execution universe is a fixed list of ~45 liquid names, so a company in
the news outside that list was never even looked at.  This job reads Alpaca
news, finds companies with several positive stories, screens them (listed,
tradable, enough daily history, a price a one-share learning trade can buy,
enough dollar volume) and admits up to ``news_discovery_max_symbols`` of them
for ``news_discovery_admit_days``.

Admission only widens what the strategies look at.  The strategies still
decide every entry, each entry on a discovered symbol is learning-sized, and
its signal carries the news tone so loss diagnosis can tell a wrong news call
(``news_wrong``) from a wrong signal.  A discovered symbol whose admission
expired stays in the cycle for exits while it is still held.

State lives in the append-only audit log (one ``news_symbol_discovery`` row
per run), so there is no schema change and every admission stays on record.
"""
from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

import httpx
from sqlalchemy.orm import Session

from app.core.config import MAX_PAPER_EXECUTION_SYMBOLS, settings
from app.models import Asset, AuditLog, NewsArticle
from app.models.stock_paper import StockPaperPosition
from app.services.audit import write_audit_log
from app.services.news_sentiment import score_headline_sentiment

logger = logging.getLogger(__name__)
UTC = timezone.utc
EVENT_TYPE = "news_symbol_discovery"
SOURCE = "alpaca_news"
NEWS_PATH = "/v1beta1/news"
# Roundups name many tickers; their tone is not about any one company.
MAX_SYMBOLS_PER_ARTICLE = 3
LISTED_EXCHANGES = frozenset({"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"})
_SYMBOL = re.compile(r"[A-Z]{1,5}")
_CACHE_SECONDS = 60.0
_cache: dict = {"at": None, "symbols": frozenset()}


class NewsUnavailable(RuntimeError):
    pass


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _iso(value: datetime) -> str:
    return _aware(value).astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse(value) -> datetime | None:
    if isinstance(value, datetime):
        return _aware(value)
    try:
        return _aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def fetch_alpaca_news(start: datetime, end: datetime, *, max_pages: int = 6, client=None) -> list[dict]:
    """Every Alpaca news item published in [start, end], newest first."""
    key, secret = settings.research_alpaca_credentials()
    if not key or not secret:
        raise NewsUnavailable("Alpaca market-data credentials are not configured")
    owned = client is None
    client = client or httpx.Client(base_url=settings.alpaca_data_url, timeout=20.0, headers={
        "APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"})
    articles: list[dict] = []
    token = None
    try:
        for _ in range(max_pages):
            params = {"start": _iso(start), "end": _iso(end), "limit": 50, "sort": "desc",
                      "include_content": "false"}
            if token:
                params["page_token"] = token
            response = client.get(NEWS_PATH, params=params)
            if response.status_code != 200:
                raise NewsUnavailable(f"Alpaca news returned HTTP {response.status_code}")
            payload = response.json()
            articles.extend(item for item in payload.get("news") or [] if isinstance(item, dict))
            token = payload.get("next_page_token")
            if not token:
                break
    except httpx.HTTPError as exc:
        raise NewsUnavailable(f"Alpaca news request failed: {exc.__class__.__name__}") from exc
    finally:
        if owned:
            client.close()
    return articles


def _article_symbols(article: dict) -> list[str]:
    symbols = []
    for value in article.get("symbols") or []:
        symbol = str(value).strip().upper()
        if _SYMBOL.fullmatch(symbol) and symbol not in symbols:
            symbols.append(symbol)
    return symbols


# Most Alpaca (Benzinga) items are analyst notes: "<Firm> Maintains Underweight
# on <Co>, Raises Price Target to $46". The keyword scorer read "raises" as
# good news on a bearish rating. A note's tone is its rating action alone.
_ANALYST_ACTION = re.compile(r"\b(Upgrades|Downgrades|Maintains|Reiterates|Initiates|Resumes|Assumes)\b")
_BULLISH_RATING = re.compile(r"\b(Strong Buy|Buy|Outperform|Overweight|Positive|Accumulate)\b", re.IGNORECASE)
# Housekeeping with no view on the business: board pay approvals, dividend
# declarations, call transcripts, conference slots ("eGain Approves ...
# Compensation" scored +1 on "approval").
_ROUTINE = re.compile(r"\bPrice Target\b|\bInsider\b|\bCompensation\b|\bDeclares\b.*\bDividend\b|"
                      r"\bTranscript\b|\bto (Present|Participate)\b|\bConference\b", re.IGNORECASE)
_BEARISH_RATING = re.compile(r"\b(Sell|Underperform|Underweight|Reduce|Negative)\b", re.IGNORECASE)


def analyst_note_sentiment(headline: str) -> float | None:
    """Tone of an analyst-rating headline; None when it is not one."""
    match = _ANALYST_ACTION.search(headline or "")
    if match is None:
        # "JPMorgan Adjusts Price Target on CDW", insider filings: routine.
        return 0.0 if _ROUTINE.search(headline or "") else None
    action = match.group(1)
    if action == "Upgrades":
        return 1.0
    if action == "Downgrades":
        return -1.0
    if action in {"Maintains", "Reiterates"}:
        return 0.0  # routine; a price-target tweak on an unchanged rating is not news
    rating = headline[match.end():]
    if _BEARISH_RATING.search(rating):
        return -0.5
    return 0.5 if _BULLISH_RATING.search(rating) else 0.0


def _article_sentiment(article: dict) -> float:
    analyst = analyst_note_sentiment(str(article.get("headline") or ""))
    if analyst is not None:
        return analyst
    return score_headline_sentiment(f"{article.get('headline') or ''} {article.get('summary') or ''}")


def store_articles(db: Session, articles: Iterable[dict]) -> int:
    """Persist focused articles as ``alpaca_news`` rows, one per named company.

    Core symbols get real stored news too, instead of the synthetic
    placeholders ``summarize_news_context`` seeds when nothing is stored.
    """
    rows = []
    for article in articles:
        symbols = _article_symbols(article)
        headline = str(article.get("headline") or "").strip()
        if not headline or not 1 <= len(symbols) <= MAX_SYMBOLS_PER_ARTICLE:
            continue
        published = _parse(article.get("created_at")) or datetime.now(UTC)
        rows.extend((symbol, headline, published, article, len(symbols)) for symbol in symbols)
    if not rows:
        return 0
    existing = {
        (symbol, title) for symbol, title in db.query(NewsArticle.symbol, NewsArticle.title).filter(
            NewsArticle.source == SOURCE, NewsArticle.symbol.in_({row[0] for row in rows}),
        ).all()
    }
    stored = 0
    for symbol, headline, published, article, count in rows:
        if (symbol, headline) in existing:
            continue
        existing.add((symbol, headline))
        db.add(NewsArticle(
            symbol=symbol,
            published_at=published.astimezone(UTC).replace(tzinfo=None),
            source=SOURCE,
            title=headline,
            url=article.get("url"),
            summary=article.get("summary") or headline,
            sentiment_score=round(_article_sentiment(article), 4),
            relevance_score=round(1 / count, 4),
            raw_payload={"provider": SOURCE, "id": article.get("id"), "publisher": article.get("source"),
                         "symbols": _article_symbols(article)},
        ))
        stored += 1
    db.flush()
    return stored


def rank_candidates(articles: Iterable[dict], *, exclude: Iterable[str] = (),
                    min_articles: int | None = None, min_sentiment: float | None = None,
                    min_directional: int | None = None) -> list[dict]:
    """Companies outside ``exclude`` with enough positive, focused stories."""
    min_articles = settings.news_discovery_min_articles if min_articles is None else min_articles
    min_sentiment = settings.news_discovery_min_sentiment if min_sentiment is None else min_sentiment
    min_directional = settings.news_discovery_min_directional if min_directional is None else min_directional
    excluded = {str(value).upper() for value in exclude}
    seen: set = set()
    by_symbol: dict[str, list[tuple[float, dict]]] = defaultdict(list)
    for article in articles:
        key = article.get("id") or article.get("headline")
        if key in seen:
            continue
        seen.add(key)
        symbols = _article_symbols(article)
        if not 1 <= len(symbols) <= MAX_SYMBOLS_PER_ARTICLE:
            continue
        sentiment = _article_sentiment(article)
        for symbol in symbols:
            if symbol not in excluded:
                by_symbol[symbol].append((sentiment, article))
    ranked = []
    for symbol, items in by_symbol.items():
        scores = [score for score, _ in items]
        # Tone over the directional stories only: routine notes (a maintained
        # rating, a price-target tweak) are coverage, not a view, and used to
        # dilute an upgrade or a raised guide to nothing.
        directional = [score for score in scores if score != 0]
        average = sum(directional) / len(directional) if directional else 0.0
        if len(items) < min_articles or len(directional) < min_directional or average < min_sentiment:
            continue
        newest = sorted(items, key=lambda item: str(item[1].get("created_at") or ""), reverse=True)
        ranked.append({
            "symbol": symbol,
            "articles": len(items),
            "avg_sentiment": round(average, 4),
            "positive": sum(score > 0 for score in scores),
            "negative": sum(score < 0 for score in scores),
            # Tone times (capped) coverage: one glowing story is not a theme.
            "score": round(average * min(len(items), 5), 4),
            "headlines": [
                {"headline": article.get("headline"), "source": article.get("source"),
                 "created_at": article.get("created_at"), "url": article.get("url"),
                 "sentiment": round(score, 4)}
                for score, article in newest[:3]
            ],
        })
    return sorted(ranked, key=lambda row: (-row["score"], -row["articles"], row["symbol"]))


def alpaca_asset(symbol: str) -> dict | None:
    """The Alpaca asset record (tradability, exchange, name); None if unknown."""
    from app.services.stock_paper_ledger import ALPACA_PAPER_URL

    key, secret = settings.paper_broker_credentials()
    if not key or not secret:
        raise NewsUnavailable("Alpaca paper credentials are not configured")
    response = httpx.get(f"{ALPACA_PAPER_URL}/v2/assets/{symbol}", timeout=10.0, headers={
        "APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"})
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def _import_daily(db: Session, symbol: str) -> None:
    from app.services.market_data import import_market_prices

    import_market_prices(db, symbol, "2y")


def screen_symbol(db: Session, symbol: str, *, max_price: float,
                  lookup_asset: Callable[[str], dict | None] = alpaca_asset,
                  import_prices: Callable[[Session, str], None] = _import_daily) -> dict:
    """Whether a news candidate can be traded and learned from on paper."""
    from app.services.trusted_data import UntrustedMarketData, trusted_history

    result = {"symbol": symbol, "admitted": False}
    try:
        asset = lookup_asset(symbol)
    except Exception as exc:
        return result | {"reason": f"asset lookup failed: {exc.__class__.__name__}"}
    if not asset:
        return result | {"reason": "not an Alpaca asset"}
    result |= {"name": asset.get("name"), "exchange": asset.get("exchange")}
    if asset.get("class") != "us_equity" or asset.get("status") != "active" or not asset.get("tradable"):
        return result | {"reason": "not an active, tradable US equity"}
    if asset.get("exchange") not in LISTED_EXCHANGES:
        return result | {"reason": f"exchange {asset.get('exchange')} is not a listed exchange"}
    existed = db.query(Asset.id).filter(Asset.symbol == symbol).first() is not None
    try:
        import_prices(db, symbol)
        if not existed:
            # The price import creates an active asset row; a rejected
            # candidate must not join the nightly imports. Admission
            # reactivates it (and discovery then owns it).
            for asset_row in db.query(Asset).filter(Asset.symbol == symbol).all():
                asset_row.is_active = False
        prices, _source = trusted_history(db, symbol, require_active=False)
    except UntrustedMarketData as exc:
        return result | {"reason": f"daily history unavailable: {exc}"}
    except Exception as exc:
        db.rollback()
        return result | {"reason": f"daily import failed: {exc.__class__.__name__}"}
    close = prices["close"].astype(float)
    volume = prices["volume"].astype(float) if "volume" in prices else close * 0
    price = float(close.iloc[-1])
    dollar_volume = float((close * volume).tail(20).mean())
    result |= {"price": round(price, 4), "dollar_volume_20d": round(dollar_volume, 2), "daily_bars": len(prices)}
    if price < settings.news_discovery_min_price:
        return result | {"reason": f"price {price:.2f} is below {settings.news_discovery_min_price:.2f}"}
    if price > max_price:
        # Learning trades buy one share at most this much (the one-share floor).
        return result | {"reason": f"price {price:.2f} is above the one-share learning cap {max_price:.2f}"}
    if dollar_volume < settings.news_discovery_min_dollar_volume:
        return result | {"reason": f"20-day dollar volume {dollar_volume:,.0f} is below "
                                    f"{settings.news_discovery_min_dollar_volume:,.0f}"}
    return result | {"admitted": True, "reason": None}


def latest_state(db: Session) -> dict:
    row = (db.query(AuditLog).filter(AuditLog.event_type == EVENT_TYPE)
           .order_by(AuditLog.id.desc()).first())
    return dict(row.payload or {}) if row is not None else {}


def news_universe(db: Session, now: datetime | None = None) -> tuple[dict[str, dict], set[str]]:
    """(admitted symbol -> evidence, symbols kept for exits only)."""
    now = _aware(now or datetime.now(UTC))
    state = latest_state(db)
    admitted, exits_only = {}, set(state.get("holding") or [])
    for row in state.get("admitted") or []:
        expires = _parse(row.get("expires_at"))
        if expires is not None and expires > now:
            admitted[row["symbol"]] = row
        else:  # expired since the last run; the next run drops it if not held
            exits_only.add(row["symbol"])
    return admitted, exits_only - set(admitted)


def discovered_symbols() -> frozenset[str]:
    """Admitted or still-held discovered symbols, cached for the intraday feed."""
    if not settings.news_discovery_enabled:
        return frozenset()
    now = time.monotonic()
    if _cache["at"] is not None and now - _cache["at"] < _CACHE_SECONDS:
        return _cache["symbols"]
    try:
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            admitted, exits_only = news_universe(db)
        _cache["symbols"] = frozenset(admitted) | frozenset(exits_only)
    except Exception as exc:  # keep the last known set; never break ingestion
        logger.info("news discovery state unavailable: %s", exc.__class__.__name__)
    _cache["at"] = now
    return _cache["symbols"]


def clear_cache() -> None:
    _cache.update(at=None, symbols=frozenset())


def _max_price(db: Session) -> float:
    from app.services.stock_paper_ledger import active_paper_account

    try:
        account = active_paper_account(db)
        equity = float(getattr(account, "equity", None) or 100_000.0)
    except Exception:  # called before any write in the run
        db.rollback()
        equity = 100_000.0
    return equity * float(settings.paper_min_one_share_equity_fraction)


def _held_symbols(db: Session, symbols: set[str]) -> set[str]:
    if not symbols:
        return set()
    rows = db.query(StockPaperPosition.symbol).filter(
        StockPaperPosition.symbol.in_(symbols), StockPaperPosition.quantity > 0).all()
    return {str(symbol).upper() for (symbol,) in rows}


def _activate(db: Session, symbol: str, name: str | None) -> bool:
    """Make the symbol an active asset; True when discovery owns that row."""
    asset = db.query(Asset).filter(Asset.symbol == symbol).one_or_none()
    if asset is None:
        db.add(Asset(symbol=symbol, name=name or symbol, asset_type="stock", sector="News discovered",
                     is_active=True))
        db.flush()
        return True
    if asset.is_active:
        return False  # someone else's active asset; never deactivate it
    asset.is_active = True
    if name and asset.name in (None, "", symbol):
        asset.name = name
    if not asset.sector:
        asset.sector = "News discovered"
    return True


def run_news_discovery(db: Session, *, now: datetime | None = None,
                       fetch_news: Callable[[datetime, datetime], list[dict]] | None = None,
                       lookup_asset: Callable[[str], dict | None] = alpaca_asset,
                       import_prices: Callable[[Session, str], None] = _import_daily) -> dict:
    from app.services.trading_universe import RESEARCH_CORE_SYMBOLS, execution_symbols

    if not settings.news_discovery_enabled:
        return {"status": "skipped", "reason": "news discovery is disabled"}
    now = _aware(now or datetime.now(UTC))
    core = set(execution_symbols()) | set(RESEARCH_CORE_SYMBOLS)
    cap = max(0, min(int(settings.news_discovery_max_symbols), MAX_PAPER_EXECUTION_SYMBOLS - len(core)))
    max_price = _max_price(db)
    state = latest_state(db)
    previous = {row["symbol"]: row for row in state.get("admitted") or []}
    managed = set(state.get("managed") or [])
    status, error, articles = "complete", None, []
    try:
        articles = (fetch_news or fetch_alpaca_news)(now - timedelta(hours=settings.news_discovery_lookback_hours), now)
    except Exception as exc:  # keep existing admissions; expire them on schedule
        status, error = "news_unavailable", f"{exc.__class__.__name__}: {str(exc)[:200]}"
    stored = store_articles(db, articles) if articles else 0
    # Each screen's price import commits, and a failed one rolls back: commit
    # every step so one bad candidate cannot undo another's admission.
    db.commit()
    ranked = rank_candidates(articles, exclude=core)

    expiry = now + timedelta(days=settings.news_discovery_admit_days)
    admitted: dict[str, dict] = {}
    for symbol, row in previous.items():
        expires = _parse(row.get("expires_at"))
        if symbol not in core and expires is not None and expires > now:
            admitted[symbol] = row
    rejected = []
    for candidate in ranked:
        symbol = candidate["symbol"]
        evidence = {key: candidate[key] for key in ("articles", "avg_sentiment", "positive", "negative",
                                                     "score", "headlines")}
        if symbol in admitted:
            # Still in the news: refresh the evidence and extend the window.
            admitted[symbol] = admitted[symbol] | evidence | {"expires_at": expiry.isoformat(),
                                                              "refreshed_at": now.isoformat()}
            continue
        if len(admitted) >= cap:
            continue
        screen = screen_symbol(db, symbol, max_price=max_price, lookup_asset=lookup_asset,
                               import_prices=import_prices)
        if not screen["admitted"]:
            rejected.append({"symbol": symbol, "reason": screen["reason"], "articles": candidate["articles"],
                             "avg_sentiment": candidate["avg_sentiment"]})
            continue
        if _activate(db, symbol, screen.get("name")):
            managed.add(symbol)
        db.commit()
        admitted[symbol] = {"symbol": symbol, **evidence, **{key: screen.get(key) for key in (
            "name", "exchange", "price", "dollar_volume_20d")},
            "admitted_at": now.isoformat(), "expires_at": expiry.isoformat()}

    # A symbol since added to the configured universe is no longer discovery's.
    managed -= core
    lapsed = (set(previous) | managed) - set(admitted) - core
    holding = _held_symbols(db, lapsed)
    for symbol in sorted(lapsed - holding):
        if symbol in managed:
            asset = db.query(Asset).filter(Asset.symbol == symbol).one_or_none()
            if asset is not None:
                asset.is_active = False
            managed.discard(symbol)
    payload = {
        "observed_at": now.isoformat(),
        "status": status,
        "error": error,
        "articles_fetched": len(articles),
        "articles_stored": stored,
        "cap": cap,
        "admitted": sorted(admitted.values(), key=lambda row: row["symbol"]),
        "newly_admitted": sorted(set(admitted) - set(previous)),
        "expired": sorted(set(previous) - set(admitted)),
        "holding": sorted(holding),
        "managed": sorted(managed),
        "rejected": rejected[:20],
        "top_candidates": [{key: row[key] for key in ("symbol", "articles", "avg_sentiment", "score")}
                           for row in ranked[:10]],
        "paper_only": True,
    }
    write_audit_log(db, event_type=EVENT_TYPE, entity_type="asset", action="run_news_discovery", status=status,
                    message=(f"{len(admitted)} news-discovered symbols admitted "
                             f"({len(payload['newly_admitted'])} new, {len(holding)} held for exits)"),
                    payload=payload)
    db.commit()
    clear_cache()
    return payload
