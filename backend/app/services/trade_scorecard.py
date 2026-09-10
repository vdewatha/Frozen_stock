from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models import PaperTrade, Strategy
from app.services.market_data import get_price_history


def _latest_price(db: Session, symbol: str) -> Optional[float]:
    prices, _source = get_price_history(db, symbol, 5, auto_seed=False)
    if prices.empty:
        return None
    return float(prices.iloc[-1]["close"])


def _entry_edge(trade: PaperTrade) -> dict:
    features = trade.features_at_entry or {}
    evidence = features.get("predictive_evidence") or {}
    best = evidence.get("best_horizon") or {}
    if not best and "best_horizon" in features:
        best = features.get("best_horizon") or {}
    return {
        "probability_up": float(best.get("probability_up", 0) or 0),
        "expected_return": float(best.get("expected_return", 0) or 0),
        "horizon_days": best.get("horizon_days"),
        "model_supported": bool(evidence.get("supports_long", best)),
    }


def predictive_trade_scorecard(db: Session, limit: int = 50) -> dict:
    strategies = {strategy.id: strategy for strategy in db.query(Strategy).all()}
    trades = db.query(PaperTrade).order_by(PaperTrade.created_at.desc()).limit(min(limit, 200)).all()
    rows: list[dict] = []
    closed_returns: list[float] = []
    open_returns: list[float] = []
    positive_open = 0

    for trade in trades:
        strategy = strategies.get(trade.strategy_id or 0)
        entry_price = float(trade.entry_price or 0)
        quantity = float(trade.quantity or 0)
        edge = _entry_edge(trade)
        current_price = float(trade.exit_price or 0) if trade.status == "closed" else (_latest_price(db, trade.symbol) or entry_price)
        pnl_pct = float(trade.profit_loss_pct or 0) if trade.status == "closed" else ((current_price / entry_price - 1) if entry_price else 0)
        pnl = float(trade.profit_loss or 0) if trade.status == "closed" else (current_price - entry_price) * quantity
        age_days = max((datetime.utcnow() - (trade.entry_time or datetime.utcnow())).days, 0)
        on_track = pnl_pct >= 0 or (edge["expected_return"] > 0 and age_days < int(edge["horizon_days"] or 1))
        if trade.status == "closed":
            closed_returns.append(pnl_pct)
        else:
            open_returns.append(pnl_pct)
            if pnl_pct >= 0:
                positive_open += 1

        rows.append(
            {
                "paper_trade_id": trade.id,
                "symbol": trade.symbol,
                "strategy_name": strategy.name if strategy else "Unknown",
                "strategy_type": strategy.strategy_type if strategy else "unknown",
                "status": trade.status or "unknown",
                "entry_time": trade.entry_time,
                "exit_time": trade.exit_time,
                "entry_price": entry_price,
                "current_price": current_price,
                "quantity": quantity,
                "profit_loss": round(pnl, 4),
                "profit_loss_pct": round(pnl_pct, 6),
                "age_days": age_days,
                "probability_up_at_entry": round(edge["probability_up"], 4),
                "expected_return_at_entry": round(edge["expected_return"], 4),
                "horizon_days": edge["horizon_days"],
                "predictive_model_supported": edge["model_supported"],
                "on_track": on_track,
                "reason_entered": trade.reason_entered,
                "reason_exited": trade.reason_exited,
            }
        )

    realized_wins = sum(1 for value in closed_returns if value > 0)
    return {
        "generated_at": datetime.utcnow(),
        "paper_trade_count": len(rows),
        "open_trades": len(open_returns),
        "closed_trades": len(closed_returns),
        "realized_win_rate": round(realized_wins / len(closed_returns), 6) if closed_returns else None,
        "avg_realized_return": round(sum(closed_returns) / len(closed_returns), 6) if closed_returns else None,
        "avg_open_return": round(sum(open_returns) / len(open_returns), 6) if open_returns else None,
        "positive_open_trades": positive_open,
        "rows": rows,
    }
