from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.models import RiskRule, Strategy, StrategyMemory
from app.services.audit import write_audit_log
from app.services.notifications import create_notification


REVIEWABLE_STATUSES = {"paused", "paper_trading_candidate"}
MIN_REACTIVATION_TRADES = 5
MIN_REACTIVATION_WIN_RATE = 0.45
MIN_REACTIVATION_PROFIT_FACTOR = 1.10
MIN_REACTIVATION_CONFIDENCE = 0.50
MAX_REACTIVATION_DRAWDOWN = -0.10


def _latest_memory_for_strategy(db: Session, strategy_id: int) -> Optional[StrategyMemory]:
    return (
        db.query(StrategyMemory)
        .filter(StrategyMemory.strategy_id == strategy_id)
        .order_by(StrategyMemory.last_updated.desc())
        .first()
    )


def _memory_snapshot(memory: Optional[StrategyMemory]) -> dict:
    if not memory:
        return {
            "sample_size": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "avg_drawdown": 0.0,
            "confidence_score": 0.0,
            "market_regime": "unclassified",
            "symbol": None,
        }
    return {
        "sample_size": int(memory.sample_size or 0),
        "win_rate": float(memory.win_rate or 0),
        "profit_factor": float(memory.profit_factor or 0),
        "avg_drawdown": float(memory.avg_drawdown or 0),
        "confidence_score": float(memory.confidence_score or 0),
        "market_regime": memory.market_regime or "unclassified",
        "symbol": memory.symbol,
    }


def _kill_switch_enabled(db: Session) -> bool:
    rule = db.query(RiskRule).filter(RiskRule.is_active.is_(True)).order_by(RiskRule.id).first()
    return bool(((rule.value if rule else {}) or {}).get("kill_switch_enabled", False))


def _blockers(strategy: Strategy, memory: dict, kill_switch_enabled: bool) -> list[str]:
    blockers: list[str] = []
    if strategy.current_status not in REVIEWABLE_STATUSES:
        blockers.append("Strategy is not paused or in candidate review.")
    if kill_switch_enabled:
        blockers.append("Global kill switch is enabled.")
    if memory["sample_size"] < MIN_REACTIVATION_TRADES:
        blockers.append("Insufficient closed paper-trade sample.")
    if memory["win_rate"] < MIN_REACTIVATION_WIN_RATE:
        blockers.append("Paper win rate below reactivation threshold.")
    if memory["profit_factor"] < MIN_REACTIVATION_PROFIT_FACTOR:
        blockers.append("Paper profit factor below reactivation threshold.")
    if memory["confidence_score"] < MIN_REACTIVATION_CONFIDENCE:
        blockers.append("Strategy confidence score below reactivation threshold.")
    if memory["avg_drawdown"] <= MAX_REACTIVATION_DRAWDOWN:
        blockers.append("Strategy drawdown remains beyond reactivation limit.")
    return blockers


def reactivation_queue(db: Session) -> dict:
    kill_switch = _kill_switch_enabled(db)
    strategies = db.query(Strategy).filter(Strategy.current_status.in_(REVIEWABLE_STATUSES)).order_by(Strategy.name).all()
    candidates = []
    for strategy in strategies:
        memory = _memory_snapshot(_latest_memory_for_strategy(db, strategy.id))
        blockers = _blockers(strategy, memory, kill_switch)
        candidates.append(
            {
                "strategy_id": strategy.id,
                "strategy_name": strategy.name,
                "strategy_type": strategy.strategy_type,
                "current_status": strategy.current_status,
                "eligible": not blockers,
                "blockers": blockers,
                "memory": memory,
            }
        )
    return {"kill_switch_enabled": kill_switch, "candidates": candidates}


def review_strategy_reactivation(db: Session, strategy_id: int, decision: str, reason: str) -> dict:
    strategy = db.query(Strategy).filter(Strategy.id == strategy_id).one_or_none()
    if not strategy:
        raise ValueError(f"Unknown strategy: {strategy_id}")

    decision = decision.lower()
    if decision not in {"approve", "reject", "hold"}:
        raise ValueError("Decision must be approve, reject, or hold.")

    kill_switch = _kill_switch_enabled(db)
    memory = _memory_snapshot(_latest_memory_for_strategy(db, strategy.id))
    blockers = _blockers(strategy, memory, kill_switch)
    old_status = strategy.current_status
    new_status = old_status
    status = "complete"
    message = reason

    if decision == "approve":
        if blockers:
            status = "blocked"
            message = "Reactivation approval blocked by review gates."
        else:
            new_status = "paper_trading_active"
            strategy.current_status = new_status
    elif decision == "reject":
        new_status = "paused"
        strategy.current_status = new_status
    else:
        new_status = "paper_trading_candidate" if old_status == "paused" else old_status
        strategy.current_status = new_status

    write_audit_log(
        db,
        event_type="strategy_reactivation",
        entity_type="strategy",
        entity_id=strategy.id,
        action=decision,
        status=status,
        message=message,
        payload={
            "old_status": old_status,
            "new_status": new_status,
            "blockers": blockers,
            "memory": memory,
            "kill_switch_enabled": kill_switch,
            "reason": reason,
        },
    )
    severity = "warning" if status == "blocked" else "info"
    create_notification(
        db,
        category="reactivation_review",
        severity=severity,
        source="strategy_reactivation",
        title=f"Strategy reactivation {status}",
        message=f"{strategy.name}: {message}",
        entity_type="strategy",
        entity_id=strategy.id,
        payload={
            "decision": decision,
            "old_status": old_status,
            "new_status": new_status,
            "blockers": blockers,
            "memory": memory,
        },
    )
    db.commit()
    db.refresh(strategy)
    return {
        "strategy_id": strategy.id,
        "strategy_name": strategy.name,
        "decision": decision,
        "status": status,
        "message": message,
        "old_status": old_status,
        "new_status": strategy.current_status,
        "eligible": not blockers,
        "blockers": blockers,
        "memory": memory,
    }
