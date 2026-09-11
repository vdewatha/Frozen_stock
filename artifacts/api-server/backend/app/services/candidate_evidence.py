from __future__ import annotations

from datetime import datetime

from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from app.models import Strategy
from app.services.backtester import BacktestConfig, run_backtest
from app.services.economic_data import summarize_macro_context
from app.services.market_regime import latest_market_regime
from app.services.news_sentiment import summarize_news_context
from app.services.portfolio_allocation import allocation_plan
from app.services.portfolio_risk import portfolio_risk_snapshot
from app.services.probabilistic_model import predict_probabilities
from app.services.readiness import readiness_snapshot
from app.services.trusted_data import trusted_history
from app.services.strategies.registry import get_strategy
from app.services.trade_candidates import get_trade_candidate_snapshot


def _find_cached_candidate(db: Session, symbol: str, strategy: str) -> dict:
    snapshot = get_trade_candidate_snapshot(db, limit=50, refresh=False, max_age_minutes=24 * 60)
    for candidate in snapshot.get("candidates") or []:
        if candidate.get("symbol") == symbol and candidate.get("strategy") == strategy:
            return {**candidate, "scanner_cache_status": snapshot.get("cache_status"), "scanner_generated_at": snapshot.get("generated_at")}
    raise ValueError("No cached scanner candidate matches that symbol and strategy. Refresh the scanner first.")


def _risk_room(db: Session, symbol: str, strategy: str) -> dict:
    risk = portfolio_risk_snapshot(db)
    symbol_row = next((row for row in risk["symbol_exposure"] if row["key"] == symbol), None)
    symbol_limit = float(risk["risk_limits"].get("max_symbol_exposure", 0))
    symbol_exposure = float((symbol_row or {}).get("exposure_pct") or 0)
    allocation = allocation_plan(db, limit=50)
    allocation_row = next(
        (row for row in allocation.get("recommendations", []) if row["symbol"] == symbol and row["strategy"] == strategy),
        None,
    )
    readiness = readiness_snapshot(db)
    blocked_checks = [check["name"] for check in readiness["checks"] if check["status"] == "blocked"]
    return {
        "paper_equity": risk["paper_equity"],
        "open_positions": risk["open_positions"],
        "gross_exposure": risk["gross_exposure"],
        "symbol_exposure_pct": round(symbol_exposure, 6),
        "symbol_limit_pct": symbol_limit,
        "symbol_room_pct": round(max(symbol_limit - symbol_exposure, 0), 6),
        "readiness_status": readiness["overall_status"],
        "paper_trading_allowed": readiness["paper_trading_allowed"],
        "blocked_checks": blocked_checks,
        "allocation_recommendation": (allocation_row or {}).get("recommendation", "not_in_positive_allocation_plan"),
        "allocation_reason": (allocation_row or {}).get("reason", "Candidate is not currently a positive BUY allocation row."),
        "target_exposure_pct": (allocation_row or {}).get("target_exposure_pct"),
        "current_exposure_pct": (allocation_row or {}).get("current_exposure_pct"),
        "alerts": risk["alerts"][:5],
    }


def candidate_evidence_drilldown(db: Session, symbol: str, strategy: str) -> dict:
    symbol = symbol.strip().upper()
    strategy = strategy.strip()
    strategy_row = db.query(Strategy).filter(Strategy.strategy_type == strategy).one_or_none()
    if not strategy_row:
        raise ValueError(f"Unknown strategy: {strategy}")

    prices, source = trusted_history(db, symbol, 420, minimum=140)
    cached = _find_cached_candidate(db, symbol, strategy)
    # Scanner caches may predate the trusted-evidence boundary. Expose only
    # market/model fields that cannot contain mock news or macro summaries.
    cached_candidate = {
        key: cached.get(key)
        for key in (
            "symbol", "strategy", "strategy_name", "strategy_status",
            "candidate_status", "score", "action", "probability_up",
            "expected_return", "scanner_cache_status", "scanner_generated_at",
        )
    }

    model = predict_probabilities(symbol, prices, source)
    signal = get_strategy(strategy_row.strategy_type, strategy_row.parameters).generate_signal(symbol, prices)
    backtest = run_backtest(symbol, strategy_row.strategy_type, prices.tail(320), BacktestConfig(), strategy_row.parameters)
    # Qualification evidence must not silently seed mock context.
    stored_news = summarize_news_context(db, symbol, auto_seed=False)
    trusted_headlines = [
        item for item in stored_news.get("top_headlines", [])
        if item.get("source") not in {"mock_news", "mock_news_fallback"}
    ]
    news = {
        "status": "available" if trusted_headlines else "excluded",
        "reason": None if trusted_headlines else "Mock news is not qualification evidence.",
        "top_headlines": trusted_headlines,
        "article_count": len(trusted_headlines),
    }
    macro = {"status": "excluded", "reason": "Synthetic macro context is not qualification evidence."}
    regime = latest_market_regime(db, auto_detect=False) or {}
    regime_macro = regime.get("features", {}).get("macro_context") or {}
    if regime_macro and regime_macro.get("status") != "excluded":
        regime = {"status": "excluded", "reason": "Legacy synthetic macro-derived regime excluded."}

    return jsonable_encoder(
        {
            "generated_at": datetime.utcnow(),
            "symbol": symbol,
            "strategy": strategy,
            "strategy_name": strategy_row.name,
            "strategy_status": strategy_row.current_status,
            "cached_candidate": cached_candidate,
            "market_data": {
                "source": source,
                "rows_used": len(prices),
                "latest_close": round(float(prices.iloc[-1]["close"]), 6) if not prices.empty else 0,
                "latest_date": str(prices.iloc[-1]["date"]) if not prices.empty else None,
            },
            "model_evidence": {
                "source": model.get("source"),
                "rows_used": model.get("rows_used", 0),
                "prediction_date": model.get("prediction_date"),
                "latest_features": model.get("latest_features", {}),
                "predictions": model.get("predictions", []),
                "walk_forward": model.get("walk_forward", [])[:9],
                "warnings": model.get("warnings", []),
            },
            "signal_evidence": {
                "action": signal.action,
                "probability_up": round(float(signal.probability_up), 6),
                "probability_down": round(float(signal.probability_down), 6),
                "confidence": round(float(signal.confidence), 6),
                "reason": signal.reason,
                "features": signal.features,
            },
            "backtest_evidence": {
                "score": backtest["score"],
                "rejected": backtest["rejected"],
                "rejection_reasons": backtest["rejection_reasons"],
                "total_return": backtest["total_return"],
                "annualized_return": backtest["annualized_return"],
                "sharpe_ratio": backtest["sharpe_ratio"],
                "sortino_ratio": backtest["sortino_ratio"],
                "max_drawdown": backtest["max_drawdown"],
                "win_rate": backtest["win_rate"],
                "profit_factor": backtest["profit_factor"],
                "number_of_trades": backtest["number_of_trades"],
            },
            "context_evidence": {
                "news": news,
                "macro": macro,
                "market_regime": regime.get("market_regime", "unclassified"),
            },
            "risk_room": _risk_room(db, symbol, strategy),
        }
    )
