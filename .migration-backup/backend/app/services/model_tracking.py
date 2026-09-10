from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

import pandas as pd

from sqlalchemy.orm import Session

from app.models import ModelPrediction, ModelValidationFold
from app.services.audit import write_audit_log
from app.services.economic_data import summarize_macro_context
from app.services.probabilistic_model import predict_probabilities
from app.services.trusted_data import trusted_history, UntrustedMarketData, TRUSTED_SOURCES


def _decimal(value: float) -> Decimal:
    return Decimal(str(round(float(value), 6)))


def run_and_persist_model_predictions(db: Session, symbol: str) -> dict:
    symbol = symbol.upper()
    prices, source = trusted_history(db, symbol, 420, minimum=140)
    result = predict_probabilities(symbol, prices, source)
    macro_context = summarize_macro_context(db)
    result["macro_context"] = macro_context
    prediction_date = date.fromisoformat(result.get("prediction_date")) if result.get("prediction_date") else None
    if not prediction_date:
        db.commit()
        return result | {"saved_prediction_ids": [], "saved_validation_fold_ids": []}

    prediction_ids: list[int] = []
    for prediction in result["predictions"]:
        row = ModelPrediction(
            symbol=symbol,
            prediction_date=prediction_date,
            horizon_days=prediction["horizon_days"],
            probability_up=_decimal(prediction["probability_up"]),
            probability_down=_decimal(prediction["probability_down"]),
            expected_return=_decimal(prediction["expected_return"]),
            source=source,
            features=result["latest_features"] | {"macro_context": macro_context},
            probabilities_by_model=prediction["probabilities_by_model"],
            is_realized=False,
        )
        db.add(row)
        db.flush()
        prediction_ids.append(row.id)

    fold_ids: list[int] = []
    for fold in result["walk_forward"]:
        row = ModelValidationFold(
            symbol=symbol,
            horizon_days=fold["horizon_days"],
            fold=fold["fold"],
            train_start=date.fromisoformat(fold["train_start"]),
            train_end=date.fromisoformat(fold["train_end"]),
            test_start=date.fromisoformat(fold["test_start"]),
            test_end=date.fromisoformat(fold["test_end"]),
            sample_size=fold["sample_size"],
            accuracy=_decimal(fold["accuracy"]),
            brier_score=_decimal(fold["brier_score"]),
        )
        db.add(row)
        db.flush()
        fold_ids.append(row.id)

    write_audit_log(
        db,
        event_type="model_prediction",
        entity_type="model_prediction",
        entity_id=prediction_ids[0] if prediction_ids else None,
        action="persist_model_run",
        status="complete",
        message=f"Persisted {len(prediction_ids)} horizon predictions for {symbol}.",
        payload={"symbol": symbol, "prediction_ids": prediction_ids, "fold_ids": fold_ids, "source": source},
    )
    db.commit()
    return result | {"saved_prediction_ids": prediction_ids, "saved_validation_fold_ids": fold_ids}


def _price_on_or_after(prices, target_date: date) -> Optional[float]:
    frame = prices.copy()
    if frame.empty:
        return None
    frame["date"] = frame["date"].astype(str)
    eligible = frame[frame["date"] >= str(target_date)]
    if eligible.empty:
        return None
    return float(eligible.iloc[0]["close"])


def score_realized_predictions(db: Session, symbol: Optional[str] = None) -> dict:
    query = db.query(ModelPrediction).filter(ModelPrediction.is_realized.is_(False))
    if symbol:
        query = query.filter(ModelPrediction.symbol == symbol.upper())
    predictions = query.order_by(ModelPrediction.prediction_date.asc()).all()
    scored_ids: list[int] = []
    checked = 0
    prices_by_symbol: dict[str, object] = {}
    for prediction in predictions:
        checked += 1
        if prediction.source not in TRUSTED_SOURCES and prediction.source not in {f"database:{s}" for s in TRUSTED_SOURCES}:
            continue
        if prediction.symbol not in prices_by_symbol:
            try:
                prices_by_symbol[prediction.symbol] = trusted_history(db, prediction.symbol, 800, minimum=1, require_active=False)[0]
            except UntrustedMarketData:
                prices_by_symbol[prediction.symbol] = None
        prices = prices_by_symbol[prediction.symbol]
        if prices is None:
            continue
        start_price, end_price = realization_prices(prices, prediction.prediction_date, prediction.horizon_days)
        if start_price is None or end_price is None:
            continue
        realized_return = end_price / start_price - 1
        realized_up = realized_return > 0
        probability = float(prediction.probability_up)
        brier = (probability - (1.0 if realized_up else 0.0)) ** 2
        prediction.realized_return = _decimal(realized_return)
        prediction.realized_up = realized_up
        prediction.brier_score = _decimal(brier)
        prediction.is_realized = True
        scored_ids.append(prediction.id)

    if scored_ids:
        write_audit_log(
            db,
            event_type="model_prediction",
            action="score_realized_predictions",
            status="complete",
            message=f"Scored {len(scored_ids)} realized model predictions.",
            payload={"symbol": symbol, "scored_ids": scored_ids},
        )
    db.commit()
    return {"checked": checked, "scored": len(scored_ids), "scored_prediction_ids": scored_ids}


def realization_prices(prices, prediction_date: date, horizon: int):
    """Match the training target's observed-bar horizon, including weekends."""
    eligible = prices[pd.to_datetime(prices["date"]).dt.date >= prediction_date]
    if eligible.empty or pd.Timestamp(eligible.iloc[0]["date"]).date() != prediction_date or len(eligible) <= horizon:
        return None, None
    return float(eligible.iloc[0]["close"]), float(eligible.iloc[horizon]["close"])


def model_performance_summary(db: Session, symbol: Optional[str] = None, limit: int = 50) -> dict:
    query = db.query(ModelPrediction)
    if symbol:
        query = query.filter(ModelPrediction.symbol == symbol.upper())
    rows = query.order_by(ModelPrediction.created_at.desc()).limit(limit).all()
    realized = [row for row in rows if row.is_realized]
    avg_brier = sum(float(row.brier_score or 0) for row in realized) / len(realized) if realized else None
    hit_rate = (
        sum(1 for row in realized if bool(row.realized_up) == (float(row.probability_up) >= 0.5)) / len(realized)
        if realized
        else None
    )
    return {
        "total_predictions": len(rows),
        "realized_predictions": len(realized),
        "avg_brier_score": round(avg_brier, 6) if avg_brier is not None else None,
        "hit_rate": round(hit_rate, 6) if hit_rate is not None else None,
        "predictions": rows,
    }
