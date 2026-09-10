from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import quote
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

import yfinance as yf
from sqlalchemy.orm import Session

from app.models import NewsArticle
from app.services.audit import write_audit_log

POSITIVE_TERMS = {
    "beat",
    "beats",
    "growth",
    "upgrade",
    "upgrades",
    "resilient",
    "strong",
    "optimism",
    "expands",
    "record",
    "surge",
    "rally",
}
NEGATIVE_TERMS = {
    "miss",
    "misses",
    "downgrade",
    "downgrades",
    "risk",
    "risks",
    "weak",
    "pressure",
    "slump",
    "cuts",
    "lawsuit",
    "warning",
}


def _decimal(value: float) -> Decimal:
    return Decimal(str(round(float(value), 4)))


def score_headline_sentiment(text: str) -> float:
    words = {word.strip(".,:;!?()[]{}").lower() for word in text.split()}
    positive = len(words & POSITIVE_TERMS)
    negative = len(words & NEGATIVE_TERMS)
    if positive == negative:
        return 0.0
    return max(-1.0, min(1.0, (positive - negative) / max(positive + negative, 1)))


def _mock_headlines(symbol: str) -> list[dict]:
    symbol = symbol.upper()
    now = datetime.utcnow().replace(microsecond=0)
    templates = [
        (
            f"{symbol} holds gains as investors weigh resilient earnings growth",
            "Market participants focused on recent earnings resilience and broad risk appetite.",
        ),
        (
            f"{symbol} faces pressure as rate-cut timing remains uncertain",
            "Macro commentary pointed to uncertainty around rates and near-term valuation pressure.",
        ),
        (
            f"Analysts flag balanced setup for {symbol} after latest data",
            "The latest setup is mixed, with trend strength offset by policy and volatility risks.",
        ),
    ]
    return [
        {
            "symbol": symbol,
            "published_at": now - timedelta(hours=index * 6),
            "source": "mock_news",
            "title": title,
            "url": f"https://example.invalid/news/{symbol.lower()}/{index}",
            "summary": summary,
            "relevance_score": 0.90 - index * 0.08,
            "raw_payload": {"fallback": True, "provider": "mock_news", "rank": index + 1},
        }
        for index, (title, summary) in enumerate(templates)
    ]


def _upsert_news_payloads(db: Session, symbol: str, payloads: list[dict], source: str, action: str) -> dict:
    symbol = symbol.strip().upper()
    rows_imported = 0
    article_ids: list[int] = []
    for payload in payloads:
        existing = (
            db.query(NewsArticle)
            .filter(
                NewsArticle.symbol == symbol,
                NewsArticle.source == payload["source"],
                NewsArticle.title == payload["title"],
            )
            .one_or_none()
        )
        sentiment = score_headline_sentiment(f"{payload['title']} {payload['summary']}")
        if existing:
            article = existing
            article.published_at = payload["published_at"]
            article.url = payload["url"]
            article.summary = payload["summary"]
            article.sentiment_score = _decimal(sentiment)
            article.relevance_score = _decimal(payload["relevance_score"])
            article.raw_payload = payload["raw_payload"]
        else:
            article = NewsArticle(
                symbol=symbol,
                published_at=payload["published_at"],
                source=payload["source"],
                title=payload["title"],
                url=payload["url"],
                summary=payload["summary"],
                sentiment_score=_decimal(sentiment),
                relevance_score=_decimal(payload["relevance_score"]),
                raw_payload=payload["raw_payload"],
            )
            db.add(article)
            rows_imported += 1
        db.flush()
        article_ids.append(article.id)

    write_audit_log(
        db,
        event_type="news_ingestion",
        entity_type="news_article",
        entity_id=article_ids[0] if article_ids else None,
        action=action,
        status="complete",
        message=f"Stored {len(article_ids)} {source} news items for {symbol}.",
        payload={"symbol": symbol, "article_ids": article_ids, "source": source},
    )
    db.commit()
    return {"symbol": symbol, "rows_imported": rows_imported, "article_ids": article_ids, "source": source}


def import_mock_news(db: Session, symbol: str) -> dict:
    symbol = symbol.strip().upper()
    return _upsert_news_payloads(db, symbol, _mock_headlines(symbol), "mock_news", "import_mock_news")


def _extract_yfinance_payload(symbol: str, item: dict, index: int) -> Optional[dict]:
    content = item.get("content") if isinstance(item.get("content"), dict) else item
    title = content.get("title") or item.get("title")
    if not title:
        return None
    summary = content.get("summary") or content.get("description") or item.get("summary") or title
    publisher = content.get("provider", {}).get("displayName") if isinstance(content.get("provider"), dict) else None
    source = publisher or item.get("publisher") or "yfinance_news"
    url = content.get("canonicalUrl", {}).get("url") if isinstance(content.get("canonicalUrl"), dict) else None
    url = url or content.get("clickThroughUrl", {}).get("url") if isinstance(content.get("clickThroughUrl"), dict) else url
    url = url or item.get("link") or item.get("url")
    published = content.get("pubDate") or content.get("displayTime") or item.get("providerPublishTime")
    published_at = datetime.utcnow() - timedelta(hours=index)
    if isinstance(published, int):
        published_at = datetime.utcfromtimestamp(published)
    elif isinstance(published, str):
        try:
            published_at = datetime.fromisoformat(published.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            published_at = datetime.utcnow() - timedelta(hours=index)
    return {
        "symbol": symbol,
        "published_at": published_at,
        "source": "yfinance_news",
        "title": title,
        "url": url or f"https://finance.yahoo.com/quote/{symbol}",
        "summary": summary,
        "relevance_score": max(0.5, 0.95 - index * 0.06),
        "raw_payload": {"provider": source, "rank": index + 1, "raw": item},
    }


def import_yfinance_news(db: Session, symbol: str, limit: int = 8, fallback: bool = True) -> dict:
    symbol = symbol.strip().upper()
    try:
        raw_news = yf.Ticker(symbol).news or []
        payloads = [
            payload
            for index, item in enumerate(raw_news[: max(1, min(limit, 20))])
            for payload in [_extract_yfinance_payload(symbol, item, index)]
            if payload is not None
        ]
    except Exception as exc:
        if not fallback:
            raise RuntimeError(f"yfinance_news failed: {exc.__class__.__name__}: {str(exc)[:240]}") from exc
        return _fallback_news(db, symbol, f"{exc.__class__.__name__}: {str(exc)[:240]}")
    if not payloads:
        if not fallback:
            raise RuntimeError("yfinance_news failed: no payloads returned.")
        return _fallback_news(db, symbol, "No yfinance news payloads returned.")
    for payload in payloads:
        sentiment = score_headline_sentiment(f"{payload['title']} {payload['summary']}")
        payload["sentiment_score"] = sentiment
    return _upsert_news_payloads(db, symbol, payloads, "yfinance_news", "import_yfinance_news")


def _fallback_news(db: Session, symbol: str, error: str) -> dict:
    result = import_mock_news(db, symbol)
    result["source"] = "mock_news_fallback"
    result["error"] = error
    return result


def _text(node: Optional[ET.Element], default: str = "") -> str:
    return (node.text or default).strip() if node is not None else default


def import_nasdaq_rss_news(db: Session, symbol: str, limit: int = 8, fallback: bool = True) -> dict:
    symbol = symbol.strip().upper()
    url = f"https://www.nasdaq.com/feed/rssoutbound?symbol={quote(symbol.lower())}"
    try:
        request = Request(url, headers={"User-Agent": "Mozilla/5.0 trading-research-bot"})
        with urlopen(request, timeout=12) as response:
            raw_xml = response.read()
        root = ET.fromstring(raw_xml)
        items = root.findall(".//item")[: max(1, min(limit, 20))]
        payloads = []
        for index, item in enumerate(items):
            title = _text(item.find("title"))
            if not title:
                continue
            summary = _text(item.find("description"), title)
            pub_date = _text(item.find("pubDate"))
            try:
                published_at = parsedate_to_datetime(pub_date).replace(tzinfo=None) if pub_date else datetime.utcnow() - timedelta(hours=index)
            except (TypeError, ValueError):
                published_at = datetime.utcnow() - timedelta(hours=index)
            payloads.append(
                {
                    "symbol": symbol,
                    "published_at": published_at,
                    "source": "nasdaq_rss",
                    "title": title,
                    "url": _text(item.find("link"), url),
                    "summary": summary,
                    "relevance_score": max(0.5, 0.95 - index * 0.06),
                    "raw_payload": {"provider": "nasdaq_rss", "rank": index + 1, "feed_url": url},
                }
            )
    except Exception as exc:
        if not fallback:
            raise RuntimeError(f"nasdaq_rss failed: {exc.__class__.__name__}: {str(exc)[:240]}") from exc
        return _fallback_news(db, symbol, f"{exc.__class__.__name__}: {str(exc)[:240]}")
    if not payloads:
        if not fallback:
            raise RuntimeError("nasdaq_rss failed: no feed items returned.")
        return _fallback_news(db, symbol, "No Nasdaq RSS feed items returned.")
    return _upsert_news_payloads(db, symbol, payloads, "nasdaq_rss", "import_nasdaq_rss_news")


def import_company_news(db: Session, symbol: str, provider: str = "auto") -> dict:
    provider = provider.strip().lower()
    if provider == "mock":
        return import_mock_news(db, symbol)
    if provider == "yfinance":
        return import_yfinance_news(db, symbol)
    if provider in {"nasdaq", "nasdaq_rss", "rss"}:
        return import_nasdaq_rss_news(db, symbol)
    if provider == "auto":
        errors = []
        try:
            return import_yfinance_news(db, symbol, fallback=False)
        except RuntimeError as exc:
            errors.append(str(exc))
        try:
            return import_nasdaq_rss_news(db, symbol, fallback=False)
        except RuntimeError as exc:
            errors.append(str(exc))
        return _fallback_news(db, symbol, " | ".join(errors))
    raise ValueError(f"Unsupported news provider: {provider}")


def list_news_articles(db: Session, symbol: Optional[str] = None, limit: int = 25) -> list[NewsArticle]:
    query = db.query(NewsArticle)
    if symbol:
        query = query.filter(NewsArticle.symbol == symbol.strip().upper())
    return query.order_by(NewsArticle.published_at.desc(), NewsArticle.id.desc()).limit(min(limit, 200)).all()


def summarize_news_context(db: Session, symbol: str, limit: int = 10, auto_seed: bool = True) -> dict:
    symbol = symbol.strip().upper()
    rows = list_news_articles(db, symbol, limit)
    if not rows and auto_seed:
        import_mock_news(db, symbol)
        rows = list_news_articles(db, symbol, limit)
    if not rows:
        return {
            "symbol": symbol,
            "article_count": 0,
            "average_sentiment": 0.0,
            "sentiment_label": "neutral",
            "summary": "No stored news context is available.",
            "top_headlines": [],
        }

    scores = [float(row.sentiment_score or 0) for row in rows]
    average = sum(scores) / len(scores)
    if average >= 0.20:
        label = "positive"
    elif average <= -0.20:
        label = "negative"
    else:
        label = "neutral"
    top_rows = sorted(rows, key=lambda row: float(row.relevance_score or 0), reverse=True)[:3]
    top_headlines = [
        {
            "id": row.id,
            "title": row.title,
            "source": row.source,
            "published_at": row.published_at.isoformat() if row.published_at else None,
            "sentiment_score": float(row.sentiment_score or 0),
            "relevance_score": float(row.relevance_score or 0),
        }
        for row in top_rows
    ]
    summary = f"{symbol} news tone is {label} across {len(rows)} stored items; average sentiment {average:.2f}."
    return {
        "symbol": symbol,
        "article_count": len(rows),
        "average_sentiment": round(average, 4),
        "sentiment_label": label,
        "summary": summary,
        "top_headlines": top_headlines,
    }
