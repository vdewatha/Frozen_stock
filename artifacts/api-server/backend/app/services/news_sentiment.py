from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
import re
from typing import Optional
from urllib.parse import quote
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

import yfinance as yf
from sqlalchemy.orm import Session

from app.models import NewsArticle
from app.services.audit import write_audit_log

# Word forms, not just base words: "shares fell", "cut price targets" and
# "guided below forecasts" scored as good news when only "falls" and "cuts"
# were listed (MCHP, 2026-11-06). Every match counts, so a beat with a weak
# guide and a falling stock nets negative.
#
# 2026-11-14 audit (reports/public-information-tests-20261114.md): on 200
# hand-labelled stories the reader called half of the plainly good or bad
# ones neutral, and was wrong on words that carry no direction by themselves.
# So the lists now leave out the bare nouns ("profit", "loss", "risks",
# "expands": a net loss figure, weight loss, a risk disclaimer and an
# expanded partnership are not news about the stock's direction), skip
# "record date" and the Korean won, and read the shapes headlines actually
# use: "trading higher", "shares rose", "Up From ... YoY", a dividend increase,
# a share repurchase, "secures"/"awarded" a contract, "FDA clears", a trial
# that "met its primary endpoint", "failed", "lose money", "bullish"/"bearish".
# Each new pattern was then read against 100,000 archive stories, which is why
# "rose" needs a price or a business line beside it ("death toll rises" and
# "rising costs" are not good news) and "failure" skips "heart failure".
_WORDS_UP = (
    r"beats?|beating|tops|topped|surg(?:e|es|ed|ing)|soar(?:s|ed|ing)?|jump(?:s|ed|ing)?|"
    r"rall(?:y|ies|ied|ying)|climb(?:s|ed|ing)?|gain(?:s|ed|ing)?|rais(?:e|es|ed|ing)|"
    r"upgrad(?:e|es|ed|ing)|record(?!\s+(?:date|holders?))|strong(?:er|est)?|outperform(?:s|ed|ing)?|"
    r"buybacks?|approvals?|approved|wins?|won|exceed(?:s|ed|ing)?|boost(?:s|ed|ing)?|above|upbeat|"
    r"optimism|resilient|growth|bullish|secures|awarded|clears|cleared|clearance|greenlights"
)
_PHRASES_UP = (
    r"bull\s+case|up\s+from|green-lights|better[-\s]than[-\s](?:expected|anticipated|feared|estimated)|"
    # "shares rose", "sales rising", "rose 4.4%".
    r"(?:shares?|stocks?|sales|revenues?|earnings|income|eps|ebitda|margins?|demand|deliveries|orders|bookings|"
    r"subscribers|volumes?|production|shipments)\s+(?:\w+\s+){0,4}?(?:rose|rises?|rising)|"
    r"(?:rose|rises?|rising)\s+(?:by\s+|about\s+|nearly\s+|over\s+|more\s+than\s+|~\s?)?\d+(?:\.\d+)?(?=\s?%)|"
    r"(?:share|stock)\s+repurchases?|repurchase\s+(?:program|plan|authori[sz]ation)|"
    r"(?:increas\w+|hik\w+)\b[^.;|]{0,40}?\bdividend|(?:inaugural|special)\s+(?:cash\s+)?dividend|"
    # "shares are trading higher", "spike higher": the move, not "higher costs".
    r"(?:trad\w+|mov\w+|spik\w+|shares|stock|gapp?\w*|open\w*|clos\w+|ended|ends|tick\w*|edg\w+|head\w*)"
    r"\s+(?:\w+\s+){0,2}?higher|"
    r"met\s+(?:its\s+|the\s+|all\s+)?(?:primary|co-primary)\s+endpoints?"
)
_WORDS_DOWN = (
    r"miss(?:es|ed|ing)?|fell|falls?|falling|drop(?:s|ped|ping)?|slid(?:e|es|ing)?|sank|sinks?|sinking|"
    r"plung(?:e|es|ed|ing)|tumbl(?:e|es|ed|ing)|slump(?:s|ed|ing)?|downgrad(?:e|es|ed|ing)|cuts?|cutting|"
    r"slash(?:es|ed|ing)?|lower(?:s|ed|ing)?|weak(?:er|ness)?|warn(?:s|ed|ing|ings)?|below|prob(?:e|es|ed)|"
    r"recalls?|recalled|layoffs?|bankrupt(?:cy)?|delay(?:s|ed)?|underperform(?:s|ed|ing)?|"
    r"lawsuits?|pressure|declin(?:e|es|ed|ing)|disappoint(?:s|ed|ing)?|worse|worsen(?:s|ed|ing)?|bearish|"
    r"fail(?:s|ed|ing)?|los(?:e|es|ing|t)|"
    r"(?<!heart )(?<!kidney )(?<!renal )(?<!liver )(?<!organ )(?<!treatment )(?<!respiratory )failures?"
)
_PHRASES_DOWN = r"bear\s+case|down\s+from|did\s+not\s+meet"

POSITIVE_PATTERN = re.compile(rf"\b(?:{_WORDS_UP}|{_PHRASES_UP})\b", re.IGNORECASE)
NEGATIVE_PATTERN = re.compile(rf"\b(?:{_WORDS_DOWN}|{_PHRASES_DOWN})\b", re.IGNORECASE)

# "1.1T Won Investment", "500 billion won": the currency, not a win.
_KOREAN_WON = re.compile(r"(\d[\d.,]*\s*(?:trillion|billion|million|[TBMK])?\s+)won\b", re.IGNORECASE)

# Numbers against their reference.  Most older headlines carry no verdict
# word: "Q2 Adj. EPS $0.59 vs $0.55 Est., Sales $282.2M vs $275.3M Est." and
# "Sees FY24 Adjusted EBITDA Of $1.22B-$1.27B (Prior View: $1.20B-$1.26B)".
# A figure (or a whole range) above its estimate is a vote up, below it a vote
# down, a range around the estimate no vote.  Against a prior view both ends
# must move the same way.  Amounts keep their K/M/B unit only when it is
# attached ("$5B", never the T of "$5 To").
_AMOUNT = r"\$?\(?-?\$?\d[\d,]*(?:\.\d+)?\)?(?:[KMBT](?![A-Za-z])|\s?(?:million|billion|trillion))?%?"
_BETWEEN = r"(?:\s*[-\u2013]\s*|\s+to\s+)"  # "$1.78-$1.82" and "$1.78 to $1.82"
_RANGE = rf"{_AMOUNT}(?:{_BETWEEN}{_AMOUNT})?"
_AMOUNT_PARTS = re.compile(
    r"\$?(\()?(-)?\$?(\d[\d,]*(?:\.\d+)?)\)?\s?([KMBT]|million|billion|trillion)?%?", re.IGNORECASE)
_RANGE_PARTS = re.compile(rf"({_AMOUNT})(?:{_BETWEEN}({_AMOUNT}))?", re.IGNORECASE)
_VERSUS_ESTIMATE = re.compile(rf"({_RANGE})\s*,?\s*(?:vs\.?|versus)\s*({_AMOUNT})\s*(?:consensus\s+)?est", re.IGNORECASE)
_VERSUS_PRIOR = re.compile(rf"({_RANGE})\s*\(\s*prior(?:\s+view)?\s*:?\s*(?:of\s+)?({_RANGE})\s*\)", re.IGNORECASE)
# A bigger number is worse news when it is a loss, a decline or a cost written as a plain figure
# ("Sees Q1 loss per share $0.10-$0.12 vs $0.08 Est"); a loss written as a negative ("Loss/Share $(2.94)
# vs. $(0.86) Est.") already compares the right way round.
_LOWER_IS_BETTER = re.compile(
    r"\b(?:loss(?:es)?|declin\w+|decreas\w+|drop\w*|outflows?|costs?|expenses?|capex|charges?)\b[^,;()]{0,40}$",
    re.IGNORECASE)
# "France HICP (MoM) -0.2% Vs. 0.1% Est.", "Jobless Claims 210K vs 225K Est.": a number under its estimate
# is not bad news there, and these name an index fund, not a company.
_ECONOMIC_RELEASE = re.compile(
    r"\((?:MoM|YoY|QoQ|WoW)\)|\b(?:PMI|CPI|PPI|GDP|HICP|Jobless|Payrolls|Retail\s+Sales|Home\s+Sales|"
    r"Housing\s+Starts|Building\s+Permits|Consumer\s+(?:Confidence|Sentiment)|Trade\s+Balance|Inventories|"
    r"Rig\s+Count|Purchasing\s+Managers|Employment\s+Change|Unemployment|Industrial\s+Production|"
    r"Durable\s+Goods|Factory\s+Orders)\b|;[^;]*\bPrior\s*$", re.IGNORECASE)
_UNITS = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12, "million": 1e6, "billion": 1e9, "trillion": 1e12}


def _amount(text: str) -> Optional[tuple[float, str]]:
    match = _AMOUNT_PARTS.fullmatch(text.strip())
    if match is None:
        return None
    unit = (match.group(4) or "").lower()
    value = float(match.group(3).replace(",", "")) * _UNITS.get(unit, 1.0)
    return (-value if match.group(1) or match.group(2) else value), unit


def _span(text: str) -> Optional[tuple[float, float]]:
    """(low, high) of "$1.22B-$1.27B", "$108M-112M", "$4.97" or "5%"; None when it does not parse."""
    match = _RANGE_PARTS.fullmatch(text.strip())
    if match is None:
        return None
    low = _amount(match.group(1))
    high = _amount(match.group(2)) if match.group(2) else low
    if low is None or high is None:
        return None
    (low_value, low_unit), (high_value, high_unit) = low, high
    if high_unit and not low_unit:  # "$108-112M": the unit belongs to both ends
        low_value *= _UNITS[high_unit]
    if low_unit and not high_unit:
        high_value *= _UNITS[low_unit]
    return min(low_value, high_value), max(low_value, high_value)


def _against_estimate(now: tuple[float, float], estimate: tuple[float, float]) -> int:
    return 1 if now[0] > estimate[1] else -1 if now[1] < estimate[0] else 0


def _against_prior(now: tuple[float, float], prior: tuple[float, float]) -> int:
    if now[0] >= prior[0] and now[1] >= prior[1] and sum(now) > sum(prior):
        return 1
    if now[0] <= prior[0] and now[1] <= prior[1] and sum(now) < sum(prior):
        return -1
    return 0


def reference_votes(text: str) -> tuple[int, int]:
    """(up, down) from figures set against their estimate or their prior view."""
    up = down = 0
    if _ECONOMIC_RELEASE.search(text):
        return up, down
    for pattern, compare in ((_VERSUS_ESTIMATE, _against_estimate), (_VERSUS_PRIOR, _against_prior)):
        for match in pattern.finditer(text):
            now, reference = _span(match.group(1)), _span(match.group(2))
            if now is None or reference is None:
                continue
            vote = compare(now, reference)
            if _LOWER_IS_BETTER.search(text[:match.start()]) and min(now[0], reference[0]) >= 0:
                vote = -vote
            up += vote > 0
            down += vote < 0
    return up, down


SYNTHETIC_NEWS_SOURCES = {"mock_news"}


def _decimal(value: float) -> Decimal:
    return Decimal(str(round(float(value), 4)))


def score_headline_sentiment(text: str) -> float:
    text = _KOREAN_WON.sub(r"\1KRW", text or "")
    up, down = reference_votes(text)
    positive = len(POSITIVE_PATTERN.findall(text)) + up
    negative = len(NEGATIVE_PATTERN.findall(text)) + down
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


def import_company_news(db: Session, symbol: str, provider: str = "auto", *, mock_fallback: bool = True) -> dict:
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
        if not mock_fallback:
            return {"symbol": symbol, "source": "unavailable", "status": "unavailable",
                    "rows_imported": 0, "errors": errors}
        return _fallback_news(db, symbol, " | ".join(errors))
    raise ValueError(f"Unsupported news provider: {provider}")


def list_news_articles(db: Session, symbol: Optional[str] = None, limit: int = 25) -> list[NewsArticle]:
    query = db.query(NewsArticle)
    if symbol:
        query = query.filter(NewsArticle.symbol == symbol.strip().upper())
    return query.order_by(NewsArticle.published_at.desc(), NewsArticle.id.desc()).limit(min(limit, 200)).all()


def summarize_news_context(db: Session, symbol: str, limit: int = 10, auto_seed: bool = False) -> dict:
    """Tone of the stored real articles for ``symbol``.

    Synthetic placeholder rows (``mock_news``) never count: the scanner,
    opportunity radar and signal endpoint used to seed them for any symbol
    without news and then score their templated tone. ``auto_seed`` is kept
    for explicit demos only and is off by default.
    """
    symbol = symbol.strip().upper()
    if auto_seed and not list_news_articles(db, symbol, 1):
        import_mock_news(db, symbol)
    rows = [
        row for row in db.query(NewsArticle)
        .filter(NewsArticle.symbol == symbol, NewsArticle.source.notin_(SYNTHETIC_NEWS_SOURCES))
        .order_by(NewsArticle.published_at.desc(), NewsArticle.id.desc())
        .limit(min(limit, 200))
    ] or ([] if not auto_seed else list_news_articles(db, symbol, limit))
    if not rows:
        return {
            "symbol": symbol,
            "article_count": 0,
            "average_sentiment": 0.0,
            "sentiment_label": "neutral",
            "summary": "No stored news context is available.",
            "top_headlines": [],
            "synthetic_only": True,
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
    # Mock headlines are auto-seeded (and used as provider fallbacks); their
    # templated tone must never be read as market evidence.
    synthetic_only = all(str(row.source or "") in SYNTHETIC_NEWS_SOURCES for row in rows)
    summary = f"{symbol} news tone is {label} across {len(rows)} stored items; average sentiment {average:.2f}."
    if synthetic_only:
        summary += " All items are synthetic placeholders, not market evidence."
    return {
        "symbol": symbol,
        "article_count": len(rows),
        "average_sentiment": round(average, 4),
        "sentiment_label": label,
        "summary": summary,
        "top_headlines": top_headlines,
        "synthetic_only": synthetic_only,
    }
