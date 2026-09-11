from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from app.models import Asset, Strategy, StrategyMemory, TradeCandidateSnapshot
from app.services.audit import write_audit_log
from app.services.backtester import BacktestConfig, run_backtest
from app.services.economic_data import summarize_macro_context
from app.services.market_regime import latest_market_regime
from app.services.news_sentiment import summarize_news_context
from app.services.probabilistic_model import predict_probabilities
from app.services.trusted_data import trusted_history, UntrustedMarketData
from app.services.strategy_research import strategy_research_for
from app.services.strategies.registry import get_strategy


def _best_positive_prediction(predictions: list[dict]) -> Optional[dict]:
    candidates = [
        prediction
        for prediction in predictions
        if float(prediction.get("probability_up", 0)) >= 0.54 and float(prediction.get("expected_return", 0)) > 0.002
    ]
    return max(candidates, key=lambda item: (float(item.get("expected_return", 0)), float(item.get("probability_up", 0))), default=None)


def _journal_feedback_adjustment(db: Session, strategy_row: Strategy, symbol: str) -> dict:
    return {
        "source": "journal_feedback",
        "status": "quarantined",
        "sample_size": 0,
        "score_adjustment": 0.0,
        "review_threshold_adjustment": 0.0,
        "confidence_score": None,
        "avg_return": None,
        "win_rate": None,
        "notes": "Legacy journal/StrategyMemory feedback is excluded from stock-paper decisions.",
    }


def scan_trade_candidates(db: Session, limit: int = 12) -> dict:
    assets = db.query(Asset).filter(Asset.is_active.is_(True)).order_by(Asset.symbol).all()
    strategies = db.query(Strategy).order_by(Strategy.name).all()
    macro_context = summarize_macro_context(db)
    regime = latest_market_regime(db, auto_detect=False) or {}
    candidates: list[dict] = []
    blocked_assets: list[dict] = []

    for asset in assets:
        try:
            prices, source = trusted_history(db, asset.symbol, 420, minimum=140)
        except UntrustedMarketData as exc:
            blocked_assets.append({"symbol": asset.symbol, "reason": str(exc)})
            continue
        model = predict_probabilities(asset.symbol, prices, source)
        best_prediction = _best_positive_prediction(model.get("predictions", []))
        news_context = summarize_news_context(db, asset.symbol)

        for strategy_row in strategies:
            signal = get_strategy(strategy_row.strategy_type, strategy_row.parameters).generate_signal(asset.symbol, prices)
            backtest = run_backtest(asset.symbol, strategy_row.strategy_type, prices.tail(320), BacktestConfig(), strategy_row.parameters)
            severe_backtest_reasons = [
                reason for reason in backtest["rejection_reasons"] if reason != "Fewer than 30 historical trades."
            ]
            model_supported = best_prediction is not None
            strategy_supported = signal.action == "BUY" and signal.confidence >= 0.55
            backtest_supported = not severe_backtest_reasons and backtest["score"] > 0
            status = "positive_candidate" if model_supported and strategy_supported and backtest_supported else "watch"
            if model_supported and strategy_supported and not backtest_supported:
                status = "needs_more_evidence"

            probability = float(best_prediction.get("probability_up", 0)) if best_prediction else float(signal.probability_up)
            expected_return = float(best_prediction.get("expected_return", 0)) if best_prediction else 0.0
            base_score = (
                probability * 0.35
                + max(expected_return, -0.05) * 4
                + float(signal.confidence) * 0.20
                + float(backtest["score"]) * 0.25
                + (0.10 if status == "positive_candidate" else 0)
            )
            journal_feedback = _journal_feedback_adjustment(db, strategy_row, asset.symbol)
            score = base_score + float(journal_feedback["score_adjustment"])
            blockers = []
            if not model_supported:
                blockers.append("No positive expected-return model horizon.")
            if not strategy_supported:
                blockers.append("Technical strategy is not a confident BUY.")
            if not backtest_supported:
                blockers.extend(severe_backtest_reasons or ["Backtest quality below promotion threshold."])

            candidates.append(
                {
                    "symbol": asset.symbol,
                    "strategy": strategy_row.strategy_type,
                    "strategy_name": strategy_row.name,
                    "strategy_status": strategy_row.current_status,
                    "strategy_research": strategy_research_for(strategy_row.strategy_type),
                    "candidate_status": status,
                    "base_score": round(base_score, 4),
                    "score": round(score, 4),
                    "memory_score_adjustment": journal_feedback["score_adjustment"],
                    "review_threshold_adjustment": journal_feedback["review_threshold_adjustment"],
                    "journal_feedback": journal_feedback,
                    "action": signal.action,
                    "probability_up": round(probability, 4),
                    "expected_return": round(expected_return, 4),
                    "horizon_days": best_prediction.get("horizon_days") if best_prediction else None,
                    "confidence": round(float(signal.confidence), 4),
                    "backtest_score": backtest["score"],
                    "backtest_rejected": backtest["rejected"],
                    "blockers": blockers[:4],
                    "reason": signal.reason,
                    "news_summary": news_context["summary"],
                    "macro_summary": macro_context["summary"],
                    "market_regime": regime.get("market_regime", "unclassified"),
                    "data_source": source,
                }
            )

    ranked = sorted(candidates, key=lambda item: item["score"], reverse=True)[: max(1, min(limit, 50))]
    return {
        "generated_at": datetime.utcnow(),
        "cache_status": "fresh",
        "trusted_data_version": 1,
        "candidate_count": len(candidates),
        "blocked_assets": blocked_assets,
        "positive_count": sum(1 for item in candidates if item["candidate_status"] == "positive_candidate"),
        "candidates": ranked,
    }


def _hydrate_candidate_strategy_statuses(db: Session, candidates: list[dict]) -> list[dict]:
    strategy_types = {str(candidate.get("strategy")) for candidate in candidates if candidate.get("strategy")}
    if not strategy_types:
        return candidates

    strategies = (
        db.query(Strategy)
        .filter(Strategy.strategy_type.in_(strategy_types))
        .all()
    )
    strategy_by_type = {strategy.strategy_type: strategy for strategy in strategies}
    hydrated = []
    for candidate in candidates:
        row = dict(candidate)
        strategy = strategy_by_type.get(str(row.get("strategy")))
        if strategy:
            row["strategy_status"] = strategy.current_status
            journal_feedback = _journal_feedback_adjustment(db, strategy, str(row.get("symbol")))
            row["base_score"] = round(float(row.get("base_score", row.get("score", 0)) or 0), 4)
            row["memory_score_adjustment"] = journal_feedback["score_adjustment"]
            row["review_threshold_adjustment"] = journal_feedback["review_threshold_adjustment"]
            row["journal_feedback"] = journal_feedback
            row["score"] = round(float(row["base_score"]) + float(journal_feedback["score_adjustment"]), 4)
        row["strategy_research"] = strategy_research_for(str(row.get("strategy")))
        hydrated.append(row)
    return hydrated


def get_trade_candidate_snapshot(db: Session, limit: int = 12, refresh: bool = False, max_age_minutes: int = 30) -> dict:
    latest = db.query(TradeCandidateSnapshot).order_by(TradeCandidateSnapshot.created_at.desc()).first()
    if latest and not refresh and (latest.payload or {}).get("trusted_data_version") == 1 and latest.created_at >= datetime.utcnow() - timedelta(minutes=max_age_minutes):
        payload = dict(latest.payload or {})
        payload["cache_status"] = "cached"
        payload["cached_at"] = latest.created_at
        hydrated = _hydrate_candidate_strategy_statuses(
            db,
            payload.get("candidates") or [],
        )
        payload["candidates"] = sorted(hydrated, key=lambda item: item["score"], reverse=True)[: max(1, min(limit, 50))]
        return payload

    payload = scan_trade_candidates(db, max(limit, 12))
    row = TradeCandidateSnapshot(payload=jsonable_encoder(payload), source="scanner")
    db.add(row)
    db.flush()
    write_audit_log(
        db,
        event_type="trade_candidate_scan",
        entity_type="trade_candidate_snapshot",
        entity_id=row.id,
        action="refresh",
        status="complete",
        message=f"Refreshed trade candidate scanner with {payload['positive_count']} positive candidates.",
        payload={"candidate_count": payload["candidate_count"], "positive_count": payload["positive_count"]},
    )
    db.commit()
    payload["cached_at"] = row.created_at
    return payload
