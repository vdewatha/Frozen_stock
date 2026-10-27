from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from app.models import Asset, Strategy, StrategyMemory, TradeCandidateSnapshot
from app.core.config import settings
from app.services.audit import write_audit_log
from app.services.backtester import BacktestConfig, run_backtest
from app.services.economic_data import summarize_macro_context
from app.services.market_regime import latest_market_regime, regime_risk_multiplier
from app.services.news_sentiment import summarize_news_context
from app.services.probabilistic_model import predict_probabilities
from app.services.risk import DEFAULT_RISK_RULES, volatility_capped_exposure
from app.services.trusted_data import trusted_history, UntrustedMarketData
from app.services.strategy_research import strategy_research_for
from app.services.strategies.registry import get_strategy


MIN_MODEL_PROBABILITY_UP = 0.54
MIN_GROSS_EXPECTED_RETURN = 0.002
# Net-of-cost edge a horizon must still show after a modeled round trip.
MIN_NET_EXPECTED_EDGE = 0.001
# The scanner's "confident BUY" bar must not sit below the execution risk
# gate; otherwise rows are labelled positive that approve_trade always blocks.
MIN_STRATEGY_CONFIDENCE = max(0.55, float(DEFAULT_RISK_RULES["min_confidence"]))
ATR_WINDOW = 14


def round_trip_cost(config: Optional[BacktestConfig] = None) -> float:
    """Fees plus slippage paid on entry and exit, as a return fraction."""
    config = config or BacktestConfig()
    return 2 * (float(config.fees_bps) + float(config.slippage_bps)) / 10_000


def _finite_or(value, default: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return numeric if math.isfinite(numeric) else default


def _best_positive_prediction(predictions: list[dict], cost: Optional[float] = None) -> Optional[dict]:
    """Best horizon whose expected return clears costs with a margin.

    Non-finite probabilities or returns never qualify.  Ranking uses the
    net-of-cost edge so a horizon is never chosen for gross return alone.
    """
    cost = round_trip_cost() if cost is None else float(cost)
    candidates = []
    for prediction in predictions:
        probability = _finite_or(prediction.get("probability_up"), float("nan"))
        expected = _finite_or(prediction.get("expected_return"), float("nan"))
        if not (math.isfinite(probability) and math.isfinite(expected)):
            continue
        if probability < MIN_MODEL_PROBABILITY_UP or expected <= MIN_GROSS_EXPECTED_RETURN:
            continue
        if expected - cost < MIN_NET_EXPECTED_EDGE:
            continue
        candidates.append(prediction)
    return max(
        candidates,
        key=lambda item: (float(item.get("expected_return", 0)) - cost, float(item.get("probability_up", 0))),
        default=None,
    )


def average_true_range_pct(prices: pd.DataFrame, window: int = ATR_WINDOW) -> Optional[float]:
    """Latest simple-average true range divided by the latest close.

    Uses high/low/previous-close when available and falls back to absolute
    close-to-close moves.  Returns ``None`` when it cannot be computed.
    """
    if prices is None or len(prices) < window + 1 or "close" not in prices:
        return None
    close = pd.to_numeric(prices["close"], errors="coerce").astype(float)
    previous = close.shift(1)
    if {"high", "low"}.issubset(prices.columns):
        high = pd.to_numeric(prices["high"], errors="coerce").astype(float)
        low = pd.to_numeric(prices["low"], errors="coerce").astype(float)
        true_range = pd.concat([high - low, (high - previous).abs(), (low - previous).abs()], axis=1).max(axis=1, skipna=False)
    else:
        true_range = (close - previous).abs()
    atr = true_range.rolling(window).mean().iloc[-1]
    latest_close = close.iloc[-1]
    if not (math.isfinite(atr) and math.isfinite(latest_close) and latest_close > 0):
        return None
    return float(atr / latest_close)


def _decision_trace(
    *,
    signal,
    best_prediction: Optional[dict],
    backtest: dict,
    cost: float,
    regime_risk: dict,
    atr_pct: Optional[float],
    gates: dict,
    status: str,
) -> dict:
    """Structured, JSON-safe "why" for a scanner decision."""
    expected = float(best_prediction.get("expected_return", 0)) if best_prediction else None
    return {
        "version": 1,
        "status": status,
        "gates": gates,
        "thresholds": {
            "min_model_probability_up": MIN_MODEL_PROBABILITY_UP,
            "min_gross_expected_return": MIN_GROSS_EXPECTED_RETURN,
            "min_net_expected_edge": MIN_NET_EXPECTED_EDGE,
            "min_strategy_confidence": MIN_STRATEGY_CONFIDENCE,
            "backtest_requires_unrejected_positive_score": True,
        },
        "inputs": {
            "strategy_action": signal.action,
            "strategy_confidence": round(_finite_or(signal.confidence, 0.0), 4),
            "strategy_probability_up": round(_finite_or(signal.probability_up, 0.5), 4),
            "model_best_horizon_days": best_prediction.get("horizon_days") if best_prediction else None,
            "model_probability_up": float(best_prediction.get("probability_up", 0)) if best_prediction else None,
            "backtest_score": backtest.get("score"),
            "backtest_rejected": bool(backtest.get("rejected")),
            "backtest_rejection_reasons": list(backtest.get("rejection_reasons") or []),
        },
        "expected_edge": {
            "gross_expected_return": expected,
            "round_trip_cost": round(cost, 6),
            "net_expected_return": round(expected - cost, 6) if expected is not None else None,
        },
        "risk": {
            "atr_pct": round(atr_pct, 6) if atr_pct is not None else None,
            "regime_risk_multiplier": regime_risk.get("multiplier", 1.0),
            "regime_risk_reasons": regime_risk.get("reasons", []),
            "market_regime": regime_risk.get("market_regime"),
        },
    }


def _backtest_supported(backtest: dict) -> bool:
    """Require an unrejected, positive-score backtest for candidate promotion."""
    return not bool(backtest.get("rejected")) and float(backtest.get("score", 0)) > 0


def _journal_feedback_adjustment(db: Session, strategy_row: Strategy | dict, symbol: str) -> dict:
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
    paper_execution_symbols = set(settings.paper_execution_symbols)
    symbols = [
        symbol
        for (symbol,) in db.query(Asset.symbol)
        .filter(Asset.is_active.is_(True))
        .order_by(Asset.symbol)
        .all()
    ]
    # Keep only scalar metadata while the scanner performs model fitting and
    # backtests. ORM instances expire after commits, so touching one later can
    # reopen a PostgreSQL transaction after the connection has been idle for
    # the duration of the CPU-bound work.
    strategies = [
        {
            "id": strategy.id,
            "name": strategy.name,
            "strategy_type": strategy.strategy_type,
            "parameters": strategy.parameters,
            "current_status": strategy.current_status,
        }
        for strategy in db.query(Strategy).order_by(Strategy.name).all()
    ]
    macro_context = summarize_macro_context(db)
    regime = latest_market_regime(db, auto_detect=False) or {}
    regime_risk = regime_risk_multiplier(regime)
    cost = round_trip_cost()
    db.commit()
    candidates: list[dict] = []
    blocked_assets: list[dict] = []

    for symbol in symbols:
        try:
            prices, source = trusted_history(db, symbol, 420, minimum=140)
        except UntrustedMarketData as exc:
            blocked_assets.append({"symbol": symbol, "reason": str(exc)})
            continue
        # History loading opens a transaction; close it before model fitting
        # so PostgreSQL does not terminate the session during CPU work.
        db.commit()
        model = predict_probabilities(
            symbol,
            prices,
            source,
            include_walk_forward=False,
            forest_estimators=40,
        )
        best_prediction = _best_positive_prediction(model.get("predictions", []), cost)
        atr_pct = average_true_range_pct(prices)
        volatility_cap = volatility_capped_exposure(atr_pct)
        news_context = summarize_news_context(db, symbol)
        db.commit()

        for strategy_row in strategies:
            signal = get_strategy(strategy_row["strategy_type"], strategy_row["parameters"]).generate_signal(symbol, prices)
            # Keep the full trusted window so the 30-trade quality gate is
            # measured against all available evidence, not an arbitrary tail.
            backtest = run_backtest(symbol, strategy_row["strategy_type"], prices, BacktestConfig(), strategy_row["parameters"])
            model_supported = best_prediction is not None
            signal_confidence = _finite_or(signal.confidence, 0.0)
            strategy_supported = signal.action == "BUY" and signal_confidence >= MIN_STRATEGY_CONFIDENCE
            # A positive score is not enough to make a candidate actionable:
            # every backtest rejection, including an insufficient sample, must
            # be resolved before a strategy can enter the paper-trade lane.
            backtest_supported = _backtest_supported(backtest)
            status = "positive_candidate" if model_supported and strategy_supported and backtest_supported else "watch"
            if model_supported and strategy_supported and not backtest_supported:
                status = "needs_more_evidence"

            probability = float(best_prediction.get("probability_up", 0)) if best_prediction else _finite_or(signal.probability_up, 0.5)
            expected_return = float(best_prediction.get("expected_return", 0)) if best_prediction else 0.0
            base_score = (
                probability * 0.35
                + max(expected_return, -0.05) * 4
                + signal_confidence * 0.20
                + float(backtest["score"]) * 0.25
                + (0.10 if status == "positive_candidate" else 0)
            )
            decision_trace = _decision_trace(
                signal=signal,
                best_prediction=best_prediction,
                backtest=backtest,
                cost=cost,
                regime_risk=regime_risk,
                atr_pct=atr_pct,
                gates={
                    "model_supported": model_supported,
                    "strategy_supported": strategy_supported,
                    "backtest_supported": backtest_supported,
                },
                status=status,
            )
            journal_feedback = _journal_feedback_adjustment(db, strategy_row, symbol)
            score = base_score + float(journal_feedback["score_adjustment"])
            blockers = []
            execution_eligible = symbol in paper_execution_symbols
            execution_blocker = None
            if not execution_eligible:
                execution_blocker = "research_only_symbol"
                blockers.append("Symbol remains research-only until its execution feed is complete.")
            if not model_supported:
                blockers.append("No positive expected-return model horizon.")
            if not strategy_supported:
                blockers.append("Technical strategy is not a confident BUY.")
            if not backtest_supported:
                blockers.extend(backtest["rejection_reasons"] or ["Backtest quality below promotion threshold."])

            candidates.append(
                {
                    "symbol": symbol,
                    "strategy": strategy_row["strategy_type"],
                    "strategy_name": strategy_row["name"],
                    "strategy_status": strategy_row["current_status"],
                    "strategy_research": strategy_research_for(strategy_row["strategy_type"]),
                    "candidate_status": status,
                    "execution_eligible": execution_eligible,
                    "execution_blocker": execution_blocker,
                    "base_score": round(base_score, 4),
                    "score": round(score, 4),
                    "memory_score_adjustment": journal_feedback["score_adjustment"],
                    "review_threshold_adjustment": journal_feedback["review_threshold_adjustment"],
                    "journal_feedback": journal_feedback,
                    "action": signal.action,
                    "probability_up": round(probability, 4),
                    "expected_return": round(expected_return, 4),
                    "net_expected_return": round(expected_return - cost, 4) if best_prediction else None,
                    "round_trip_cost": round(cost, 6),
                    "atr_pct": round(atr_pct, 6) if atr_pct is not None else None,
                    "volatility_exposure_cap": round(volatility_cap, 6) if volatility_cap is not None else None,
                    "regime_risk_multiplier": regime_risk["multiplier"],
                    "decision_trace": decision_trace,
                    "horizon_days": best_prediction.get("horizon_days") if best_prediction else None,
                    "confidence": round(signal_confidence, 4),
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
        # Keep research candidates visible, but only count paper-execution
        # eligible rows as actionable positives in the summary.
        "positive_count": sum(
            1
            for item in candidates
            if item["candidate_status"] == "positive_candidate" and item["execution_eligible"]
        ),
        "research_positive_count": sum(
            1 for item in candidates if item["candidate_status"] == "positive_candidate"
        ),
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
    paper_execution_symbols = set(settings.paper_execution_symbols)
    for candidate in candidates:
        row = dict(candidate)
        symbol = str(row.get("symbol") or "").upper()
        row["execution_eligible"] = symbol in paper_execution_symbols
        row["execution_blocker"] = (
            None if row["execution_eligible"] else "research_only_symbol"
        )
        blockers = list(row.get("blockers") or [])
        if not row["execution_eligible"] and not any("research-only" in str(item).lower() for item in blockers):
            blockers.insert(0, "Symbol remains research-only until its execution feed is complete.")
        row["blockers"] = blockers[:4]
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
        # Recompute summary fields so older cached snapshots receive the same
        # research-only labeling as freshly generated candidates.
        payload["positive_count"] = sum(
            1
            for item in hydrated
            if item.get("candidate_status") == "positive_candidate" and item.get("execution_eligible")
        )
        payload["research_positive_count"] = sum(
            1 for item in hydrated if item.get("candidate_status") == "positive_candidate"
        )
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
