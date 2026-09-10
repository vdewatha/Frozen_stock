from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models import Asset
from app.services.news_sentiment import import_company_news, summarize_news_context
from app.services.trusted_data import trusted_history, UntrustedMarketData
from app.services.trade_candidates import get_trade_candidate_snapshot


def _price_features(prices) -> dict:
    if prices.empty:
        return {
            "latest_close": 0.0,
            "return_20d": 0.0,
            "return_60d": 0.0,
            "ma_20": 0.0,
            "ma_50": 0.0,
            "trend_label": "no_data",
            "chart": [],
        }
    close = prices["close"].astype(float)
    latest_close = float(close.iloc[-1])
    return_20d = latest_close / float(close.iloc[-21]) - 1 if len(close) > 21 and float(close.iloc[-21]) else 0.0
    return_60d = latest_close / float(close.iloc[-61]) - 1 if len(close) > 61 and float(close.iloc[-61]) else 0.0
    ma_20 = float(close.tail(20).mean()) if len(close) >= 20 else latest_close
    ma_50 = float(close.tail(50).mean()) if len(close) >= 50 else latest_close
    if latest_close > ma_20 > ma_50:
        trend_label = "uptrend"
    elif latest_close < ma_20 < ma_50:
        trend_label = "downtrend"
    else:
        trend_label = "mixed"
    chart_rows = prices.tail(45).to_dict(orient="records")
    chart = [
        {
            "date": str(row["date"]),
            "close": round(float(row["close"]), 4),
        }
        for row in chart_rows
    ]
    return {
        "latest_close": round(latest_close, 4),
        "return_20d": round(return_20d, 6),
        "return_60d": round(return_60d, 6),
        "ma_20": round(ma_20, 4),
        "ma_50": round(ma_50, 4),
        "trend_label": trend_label,
        "chart": chart,
    }


def _opportunity_score(best_candidate: dict | None, news: dict, features: dict) -> float:
    candidate_score = float((best_candidate or {}).get("score") or 0)
    probability_up = float((best_candidate or {}).get("probability_up") or 0.5)
    expected_return = float((best_candidate or {}).get("expected_return") or 0)
    news_score = float(news.get("average_sentiment") or 0)
    trend_bonus = 0.06 if features["trend_label"] == "uptrend" else (-0.04 if features["trend_label"] == "downtrend" else 0)
    return round(candidate_score * 0.55 + (probability_up - 0.5) * 0.25 + expected_return * 4 + news_score * 0.08 + trend_bonus, 6)


def company_opportunity_radar(db: Session, limit: int = 8, refresh_news: bool = False, news_provider: str = "auto") -> dict:
    assets = db.query(Asset).filter(Asset.is_active.is_(True)).order_by(Asset.symbol).limit(max(1, min(limit, 20))).all()
    scanner = get_trade_candidate_snapshot(db, limit=50, refresh=False, max_age_minutes=24 * 60)
    candidates = scanner.get("candidates") or []
    news_imports = []
    rows = []
    blocked_assets = []
    for asset in assets:
        try:
            prices, source = trusted_history(db, asset.symbol, 120)
        except UntrustedMarketData as exc:
            blocked_assets.append({"symbol": asset.symbol, "reason": str(exc)})
            continue
        features = _price_features(prices)
        if refresh_news:
            news_imports.append(import_company_news(db, asset.symbol, news_provider))
        news = summarize_news_context(db, asset.symbol)
        symbol_candidates = [candidate for candidate in candidates if candidate.get("symbol") == asset.symbol]
        best_candidate = max(symbol_candidates, key=lambda item: float(item.get("score") or 0), default=None)
        positive_candidates = [candidate for candidate in symbol_candidates if candidate.get("candidate_status") == "positive_candidate"]
        score = _opportunity_score(best_candidate, news, features)
        rows.append(
            {
                "symbol": asset.symbol,
                "name": asset.name or asset.symbol,
                "sector": asset.sector,
                "data_source": source,
                "price": features,
                "news": news,
                "best_candidate": best_candidate,
                "positive_candidate_count": len(positive_candidates),
                "opportunity_score": score,
                "recommendation": "paper_trade_candidate" if positive_candidates and score > 0 else "watch",
            }
        )
    ranked = sorted(rows, key=lambda row: row["opportunity_score"], reverse=True)
    return {
        "generated_at": datetime.utcnow(),
        "asset_count": len(ranked),
        "blocked_assets": blocked_assets,
        "scanner_cache_status": scanner.get("cache_status"),
        "news_imports": news_imports,
        "opportunities": ranked,
    }
