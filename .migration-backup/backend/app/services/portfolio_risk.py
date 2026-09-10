from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models import PaperTrade, Strategy, RiskRule
from app.services.risk import DEFAULT_RISK_RULES
from app.services.trusted_data import trusted_history, UntrustedMarketData

DEFAULT_PAPER_EQUITY = 100_000.0
WARN_UTILIZATION = 0.85


def _latest_price(db: Session, symbol: str, fallback: float) -> tuple[float, str]:
    try:
        prices, source = trusted_history(db, symbol, 90, minimum=1, require_active=False)
    except UntrustedMarketData:
        return fallback, "valuation_unavailable"
    return float(prices.iloc[-1]["close"]), source


def _alert(severity: str, label: str, message: str, utilization: Optional[float] = None) -> dict:
    return {"severity": severity, "label": label, "message": message, "utilization": utilization}


def portfolio_risk_snapshot(db: Session) -> dict:
    risk_rule = db.query(RiskRule).filter(RiskRule.is_active.is_(True)).order_by(RiskRule.id).first()
    rules = DEFAULT_RISK_RULES | ((risk_rule.value if risk_rule else {}) or {})
    open_trades = (
        db.query(PaperTrade)
        .filter(PaperTrade.status == "open")
        .order_by(PaperTrade.entry_time.desc())
        .all()
    )
    strategies = {strategy.id: strategy for strategy in db.query(Strategy).all()}

    positions: list[dict] = []
    symbol_exposure: dict[str, float] = {}
    strategy_exposure: dict[str, float] = {}
    strategy_counts: dict[str, int] = {}
    sources: set[str] = set()
    total_notional = 0.0
    total_unrealized_pl = 0.0

    for trade in open_trades:
        entry_price = float(trade.entry_price or 0)
        quantity = float(trade.quantity or 0)
        latest_price, source = _latest_price(db, trade.symbol, entry_price)
        sources.add(source)
        notional = latest_price * quantity
        entry_notional = entry_price * quantity
        unrealized_pl = (latest_price - entry_price) * quantity if trade.side == "BUY" else (entry_price - latest_price) * quantity
        unrealized_pl_pct = unrealized_pl / entry_notional if entry_notional else 0.0
        strategy = strategies.get(trade.strategy_id or 0)
        strategy_name = strategy.name if strategy else "Unassigned"
        symbol_exposure[trade.symbol] = symbol_exposure.get(trade.symbol, 0.0) + notional / DEFAULT_PAPER_EQUITY
        strategy_exposure[strategy_name] = strategy_exposure.get(strategy_name, 0.0) + notional / DEFAULT_PAPER_EQUITY
        strategy_counts[strategy_name] = strategy_counts.get(strategy_name, 0) + 1
        total_notional += notional
        total_unrealized_pl += unrealized_pl
        positions.append(
            {
                "paper_trade_id": trade.id,
                "symbol": trade.symbol,
                "strategy": strategy_name,
                "side": trade.side,
                "quantity": round(quantity, 6),
                "entry_price": round(entry_price, 4),
                "latest_price": round(latest_price, 4),
                "valuation_valid": source != "valuation_unavailable",
                "data_source": source,
                "notional": round(notional, 2),
                "exposure_pct": round(notional / DEFAULT_PAPER_EQUITY, 6),
                "unrealized_pl": round(unrealized_pl, 2),
                "unrealized_pl_pct": round(unrealized_pl_pct, 6),
                "entry_time": trade.entry_time.isoformat() if trade.entry_time else None,
            }
        )

    alerts: list[dict] = []
    if "valuation_unavailable" in sources:
        alerts.append(_alert("breach", "Valuation unavailable", "A position lacks trusted current prices; displayed entry-cost estimates are not current valuations."))
    gross_exposure = total_notional / DEFAULT_PAPER_EQUITY
    position_utilization = len(open_trades) / max(int(rules.get("max_open_positions", 1)), 1)
    symbol_limit = float(rules.get("max_symbol_exposure", 1))
    max_daily_drawdown = float(rules.get("max_daily_drawdown", 1))
    current_drawdown = abs(min(total_unrealized_pl / DEFAULT_PAPER_EQUITY, 0.0))

    if bool(rules.get("kill_switch_enabled", False)):
        alerts.append(_alert("breach", "Kill switch", "Global kill switch is enabled. New paper trades are blocked.", 1.0))
    if position_utilization >= 1:
        alerts.append(_alert("breach", "Open positions", "Portfolio open-position limit has been reached.", position_utilization))
    elif position_utilization >= WARN_UTILIZATION:
        alerts.append(_alert("warning", "Open positions", "Portfolio is close to the open-position limit.", position_utilization))
    if current_drawdown >= max_daily_drawdown and max_daily_drawdown > 0:
        alerts.append(_alert("breach", "Drawdown", "Unrealized paper drawdown is beyond the daily drawdown limit.", current_drawdown / max_daily_drawdown))
    elif max_daily_drawdown > 0 and current_drawdown / max_daily_drawdown >= WARN_UTILIZATION:
        alerts.append(_alert("warning", "Drawdown", "Unrealized paper drawdown is close to the daily drawdown limit.", current_drawdown / max_daily_drawdown))

    for symbol, exposure in symbol_exposure.items():
        utilization = exposure / symbol_limit if symbol_limit else 0.0
        if utilization >= 1:
            alerts.append(_alert("breach", symbol, "Symbol exposure is at or above its configured cap.", utilization))
        elif utilization >= WARN_UTILIZATION:
            alerts.append(_alert("warning", symbol, "Symbol exposure is close to its configured cap.", utilization))

    max_per_strategy = int(rules.get("max_open_positions_per_strategy", 1))
    for strategy_name, count in strategy_counts.items():
        utilization = count / max(max_per_strategy, 1)
        if utilization >= 1:
            alerts.append(_alert("breach", strategy_name, "Strategy open-position count is at its configured cap.", utilization))
        elif utilization >= WARN_UTILIZATION:
            alerts.append(_alert("warning", strategy_name, "Strategy open-position count is close to its configured cap.", utilization))

    if not alerts:
        alerts.append(_alert("clear", "Risk monitor", "No portfolio exposure alerts.", 0.0))

    return {
        "generated_at": datetime.utcnow(),
        "paper_equity": DEFAULT_PAPER_EQUITY,
        "open_positions": len(open_trades),
        "gross_exposure": round(gross_exposure, 6),
        "total_notional": round(total_notional, 2),
        "valuation_valid": "valuation_unavailable" not in sources,
        "total_unrealized_pl": round(total_unrealized_pl, 2),
        "total_unrealized_pl_pct": round(total_unrealized_pl / DEFAULT_PAPER_EQUITY, 6),
        "risk_limits": {
            "max_open_positions": int(rules.get("max_open_positions", 0)),
            "max_open_positions_per_strategy": max_per_strategy,
            "max_symbol_exposure": symbol_limit,
            "max_daily_drawdown": max_daily_drawdown,
            "candidate_review_score_threshold": float(rules.get("candidate_review_score_threshold", 0.70)),
            "activation_score_threshold": float(rules.get("activation_score_threshold", 0.72)),
            "journal_feedback_review_threshold_cap": float(rules.get("journal_feedback_review_threshold_cap", 0.03)),
            "journal_feedback_allocation_multiplier_cap": float(rules.get("journal_feedback_allocation_multiplier_cap", 0.20)),
            "memory_replay_min_complete_samples": int(rules.get("memory_replay_min_complete_samples", 10)),
            "memory_replay_min_avg_return_delta": float(rules.get("memory_replay_min_avg_return_delta", 0.0)),
            "memory_replay_min_hit_rate_delta": float(rules.get("memory_replay_min_hit_rate_delta", 0.0)),
            "kill_switch_enabled": bool(rules.get("kill_switch_enabled", False)),
            "paper_only": bool(rules.get("paper_only", True)),
        },
        "positions": positions,
        "symbol_exposure": [
            {"key": symbol, "exposure_pct": round(exposure, 6), "limit_pct": symbol_limit, "utilization": round(exposure / symbol_limit, 6) if symbol_limit else 0.0}
            for symbol, exposure in sorted(symbol_exposure.items(), key=lambda item: item[1], reverse=True)
        ],
        "strategy_exposure": [
            {
                "key": strategy_name,
                "exposure_pct": round(exposure, 6),
                "open_positions": strategy_counts.get(strategy_name, 0),
                "position_limit": max_per_strategy,
                "utilization": round(strategy_counts.get(strategy_name, 0) / max(max_per_strategy, 1), 6),
            }
            for strategy_name, exposure in sorted(strategy_exposure.items(), key=lambda item: item[1], reverse=True)
        ],
        "alerts": alerts,
        "data_sources": sorted(sources) or ["none"],
    }
