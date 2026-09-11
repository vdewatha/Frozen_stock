from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

import pandas as pd
from sqlalchemy.orm import Session

from app.models import MarketRegime
from app.services.audit import write_audit_log
from app.services.trusted_data import trusted_history


def _decimal(value: float) -> Decimal:
    return Decimal(str(round(float(value), 6)))


def _clean_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    cleaned = frame.copy()
    cleaned["date"] = pd.to_datetime(cleaned["date"]).dt.date
    for column in ["open", "high", "low", "close", "adjusted_close", "volume"]:
        if column in cleaned:
            cleaned[column] = pd.to_numeric(cleaned[column], errors="coerce")
    return cleaned.dropna(subset=["date", "close"]).sort_values("date")


def _trend_label(close: pd.Series) -> tuple[str, float, float]:
    ma_50 = float(close.rolling(50).mean().iloc[-1])
    ma_200 = float(close.rolling(200).mean().iloc[-1])
    trend_strength = ma_50 / ma_200 - 1 if ma_200 else 0.0
    if trend_strength >= 0.03:
        return "uptrend", ma_50, ma_200
    if trend_strength <= -0.03:
        return "downtrend", ma_50, ma_200
    return "sideways", ma_50, ma_200


def _volatility_label(close: pd.Series) -> tuple[str, float]:
    realized_vol = float(close.pct_change().rolling(20).std().iloc[-1] * (252**0.5))
    if realized_vol >= 0.28:
        return "high_volatility", realized_vol
    if realized_vol <= 0.14:
        return "low_volatility", realized_vol
    return "normal_volatility", realized_vol


def _rate_proxy_label(close: pd.Series) -> tuple[str, float]:
    returns = close.pct_change(60)
    sixty_day_return = float(returns.iloc[-1])
    if sixty_day_return >= 0.08:
        return "risk_on", sixty_day_return
    if sixty_day_return <= -0.08:
        return "risk_off", sixty_day_return
    return "neutral", sixty_day_return


def classify_market_regime(prices: pd.DataFrame, macro_context: Optional[dict] = None) -> dict:
    frame = _clean_frame(prices)
    if len(frame) < 220:
        raise ValueError("At least 220 price rows are required for regime classification.")
    close = frame["close"]
    spy_trend, ma_50, ma_200 = _trend_label(close)
    volatility_regime, realized_volatility = _volatility_label(close)
    rate_regime, sixty_day_return = _rate_proxy_label(close)
    # Macro data is display-only until a trusted entitled source is configured.
    # Retain the argument for API compatibility, but never use it as evidence.

    if spy_trend == "uptrend" and volatility_regime != "high_volatility" and rate_regime != "restrictive":
        market_regime = "bull_trend"
    elif spy_trend == "downtrend" and volatility_regime == "high_volatility":
        market_regime = "bear_stress"
    elif rate_regime == "restrictive" and volatility_regime == "high_volatility":
        market_regime = "policy_stress"
    elif volatility_regime == "high_volatility":
        market_regime = "volatile_range"
    elif spy_trend == "sideways":
        market_regime = "sideways_range"
    else:
        market_regime = "transition"

    latest = frame.iloc[-1]
    return {
        "regime_date": latest["date"],
        "spy_trend": spy_trend,
        "volatility_regime": volatility_regime,
        "rate_regime": rate_regime,
        "market_regime": market_regime,
        "features": {
            "latest_close": round(float(latest["close"]), 4),
            "ma_50": round(ma_50, 4),
            "ma_200": round(ma_200, 4),
            "ma_50_vs_200": round(ma_50 / ma_200 - 1 if ma_200 else 0.0, 6),
            "realized_volatility_20d": round(realized_volatility, 6),
            "return_60d": round(sixty_day_return, 6),
            "macro_context": {"status": "excluded", "reason": "No trusted macro feed configured."},
        },
    }


def detect_and_store_market_regime(db: Session, symbol: str = "SPY") -> dict:
    symbol = symbol.strip().upper()
    prices, source = trusted_history(db, symbol, 320, minimum=220)
    result = classify_market_regime(prices)
    regime_date: date = result["regime_date"]
    row = db.query(MarketRegime).filter(MarketRegime.regime_date == regime_date).one_or_none()
    if not row:
        row = MarketRegime(regime_date=regime_date)
        db.add(row)
    row.spy_trend = result["spy_trend"]
    row.volatility_regime = result["volatility_regime"]
    row.rate_regime = result["rate_regime"]
    row.market_regime = result["market_regime"]
    row.features = result["features"] | {"symbol": symbol, "source": source}
    db.flush()
    write_audit_log(
        db,
        event_type="market_regime",
        entity_type="market_regime",
        entity_id=row.id,
        action="detect_regime",
        status=row.market_regime or "unknown",
        message=f"Detected {row.market_regime} from {symbol} price regime features.",
        payload=row.features,
    )
    db.commit()
    db.refresh(row)
    return market_regime_to_dict(row)


def latest_market_regime(db: Session, auto_detect: bool = True) -> Optional[dict]:
    row = db.query(MarketRegime).order_by(MarketRegime.regime_date.desc(), MarketRegime.id.desc()).first()
    if not row and auto_detect:
        return detect_and_store_market_regime(db)
    return market_regime_to_dict(row) if row else None


def list_market_regimes(db: Session, limit: int = 30) -> list[dict]:
    rows = db.query(MarketRegime).order_by(MarketRegime.regime_date.desc(), MarketRegime.id.desc()).limit(min(limit, 200)).all()
    return [market_regime_to_dict(row) for row in rows]


def market_regime_to_dict(row: MarketRegime) -> dict:
    return {
        "id": row.id,
        "regime_date": row.regime_date,
        "spy_trend": row.spy_trend,
        "volatility_regime": row.volatility_regime,
        "rate_regime": row.rate_regime,
        "market_regime": row.market_regime,
        "features": row.features or {},
    }
