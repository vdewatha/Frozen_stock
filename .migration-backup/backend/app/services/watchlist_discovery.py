from __future__ import annotations

from datetime import datetime

from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from app.models import Asset
from app.services.audit import write_audit_log
from app.services.market_data import import_market_prices
from app.services.news_sentiment import import_company_news
from app.services.trade_candidates import get_trade_candidate_snapshot


STARTER_UNIVERSE = [
    {"symbol": "NVDA", "name": "NVIDIA Corporation", "sector": "Technology", "industry": "Semiconductors"},
    {"symbol": "AMZN", "name": "Amazon.com, Inc.", "sector": "Consumer Discretionary", "industry": "Internet Retail"},
    {"symbol": "META", "name": "Meta Platforms, Inc.", "sector": "Communication Services", "industry": "Internet Content"},
    {"symbol": "GOOGL", "name": "Alphabet Inc.", "sector": "Communication Services", "industry": "Internet Content"},
    {"symbol": "TSLA", "name": "Tesla, Inc.", "sector": "Consumer Discretionary", "industry": "Automobiles"},
    {"symbol": "JPM", "name": "JPMorgan Chase & Co.", "sector": "Financials", "industry": "Banks"},
    {"symbol": "AVGO", "name": "Broadcom Inc.", "sector": "Technology", "industry": "Semiconductors"},
    {"symbol": "LLY", "name": "Eli Lilly and Company", "sector": "Health Care", "industry": "Pharmaceuticals"},
    {"symbol": "UNH", "name": "UnitedHealth Group Incorporated", "sector": "Health Care", "industry": "Managed Health Care"},
    {"symbol": "XOM", "name": "Exxon Mobil Corporation", "sector": "Energy", "industry": "Integrated Oil & Gas"},
]


def discover_watchlist_candidates(db: Session, limit: int = 10) -> dict:
    existing = {symbol for (symbol,) in db.query(Asset.symbol).all()}
    candidates = [
        {**row, "already_active": row["symbol"] in existing}
        for row in STARTER_UNIVERSE[: max(1, min(limit, len(STARTER_UNIVERSE)))]
    ]
    return {
        "generated_at": datetime.utcnow(),
        "source": "curated_large_cap_universe",
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


def import_watchlist_candidates(
    db: Session,
    *,
    symbols: list[str] | None = None,
    limit: int = 6,
    period: str = "2y",
    import_prices: bool = True,
    import_news: bool = True,
    news_provider: str = "auto",
    refresh_candidates: bool = False,
) -> dict:
    selected_symbols = [symbol.strip().upper() for symbol in (symbols or []) if symbol.strip()]
    if not selected_symbols:
        selected_symbols = [row["symbol"] for row in STARTER_UNIVERSE[: max(1, min(limit, len(STARTER_UNIVERSE)))]]
    metadata = {row["symbol"]: row for row in STARTER_UNIVERSE}
    assets = []
    market_results = []
    news_results = []

    for symbol in selected_symbols:
        row = metadata.get(symbol, {"symbol": symbol, "name": symbol, "sector": "Discovered", "industry": None})
        asset = db.query(Asset).filter(Asset.symbol == symbol).one_or_none()
        if not asset:
            asset = Asset(
                symbol=symbol,
                name=row.get("name") or symbol,
                sector=row.get("sector"),
                industry=row.get("industry"),
                is_active=True,
            )
            db.add(asset)
            db.flush()
        else:
            asset.is_active = True
            asset.name = asset.name or row.get("name") or symbol
            asset.sector = asset.sector or row.get("sector")
            asset.industry = asset.industry or row.get("industry")
        assets.append(asset.symbol)
        if import_prices:
            market_results.append(import_market_prices(db, symbol, period))
        if import_news:
            news_results.append(import_company_news(db, symbol, news_provider))

    db.commit()
    candidate_scan = None
    if refresh_candidates:
        snapshot = get_trade_candidate_snapshot(db, limit=50, refresh=True)
        candidate_scan = {
            "cache_status": snapshot["cache_status"],
            "candidate_count": snapshot["candidate_count"],
            "positive_count": snapshot["positive_count"],
            "generated_at": snapshot["generated_at"],
            "top_candidates": [
                {
                    "symbol": candidate["symbol"],
                    "strategy": candidate["strategy"],
                    "strategy_name": candidate["strategy_name"],
                    "candidate_status": candidate["candidate_status"],
                    "score": candidate["score"],
                }
                for candidate in (snapshot.get("candidates") or [])[:5]
            ],
        }

    payload = jsonable_encoder(
        {
            "symbols": assets,
            "period": period,
            "market_results": market_results,
            "news_results": news_results,
            "refresh_candidates": refresh_candidates,
            "candidate_scan": candidate_scan,
        }
    )
    write_audit_log(
        db,
        event_type="watchlist_discovery",
        entity_type="asset",
        action="import_watchlist_candidates",
        status="complete",
        message=f"Imported or activated {len(assets)} watchlist candidates.",
        payload=payload,
    )
    db.commit()
    return jsonable_encoder({
        "generated_at": datetime.utcnow(),
        "source": "curated_large_cap_universe",
        "activated_symbols": assets,
        "market_results": market_results,
        "news_results": news_results,
        "candidate_scan": candidate_scan,
    })
