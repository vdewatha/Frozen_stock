from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models import RiskRule, Strategy
from app.services.audit import write_audit_log
from app.services.risk import DEFAULT_RISK_RULES


def _active_risk_rule(db: Session) -> RiskRule:
    rule = db.query(RiskRule).filter(RiskRule.is_active.is_(True)).order_by(RiskRule.id).first()
    if not rule:
        rule = RiskRule(name="default_paper_risk", value=DEFAULT_RISK_RULES, is_active=True)
        db.add(rule)
        db.flush()
    return rule


def _set_kill_switch(db: Session, enabled: bool, reason: str) -> dict:
    rule = _active_risk_rule(db)
    old_value = rule.value or {}
    new_value = DEFAULT_RISK_RULES | old_value | {"kill_switch_enabled": enabled, "paper_only": True}
    rule.value = new_value
    status = "enabled" if enabled else "disabled"
    write_audit_log(
        db,
        event_type="safety_control",
        entity_type="risk_rule",
        entity_id=rule.id,
        action="set_kill_switch",
        status=status,
        message=reason,
        payload={"old_value": old_value, "new_value": new_value},
    )
    db.commit()
    return {
        "action": "set_kill_switch",
        "status": status,
        "message": reason,
        "kill_switch_enabled": enabled,
        "paused_strategies": 0,
        "affected_strategy_ids": [],
        "risk_rule": {"id": rule.id, "name": rule.name, "value": rule.value, "is_active": rule.is_active},
    }


def enable_kill_switch(db: Session, reason: str = "Manual kill switch from control room.") -> dict:
    return _set_kill_switch(db, True, reason)


def disable_kill_switch(db: Session, reason: str = "Manual kill switch reset from control room.") -> dict:
    return _set_kill_switch(db, False, reason)


def pause_all_strategies(db: Session, reason: str = "Manual pause from control room.") -> dict:
    strategies = db.query(Strategy).order_by(Strategy.name).all()
    affected_ids: list[int] = []
    old_statuses = {}
    for strategy in strategies:
        old_statuses[strategy.id] = strategy.current_status
        if strategy.current_status != "paused":
            affected_ids.append(strategy.id)
            strategy.current_status = "paused"
            strategy.updated_at = datetime.utcnow()

    write_audit_log(
        db,
        event_type="safety_control",
        entity_type="strategy",
        entity_id=None,
        action="pause_all_strategies",
        status="complete",
        message=reason,
        payload={"affected_strategy_ids": affected_ids, "old_statuses": old_statuses},
    )
    db.commit()
    return {
        "action": "pause_all_strategies",
        "status": "complete",
        "message": reason,
        "kill_switch_enabled": bool((_active_risk_rule(db).value or {}).get("kill_switch_enabled", False)),
        "paused_strategies": len(affected_ids),
        "affected_strategy_ids": affected_ids,
        "risk_rule": None,
    }


def resume_candidate_strategies(db: Session, reason: str = "Manual resume from control room.") -> dict:
    strategies = db.query(Strategy).filter(Strategy.current_status == "paused").order_by(Strategy.name).all()
    affected_ids: list[int] = []
    for strategy in strategies:
        strategy.current_status = "paper_trading_candidate"
        strategy.updated_at = datetime.utcnow()
        affected_ids.append(strategy.id)
    write_audit_log(
        db,
        event_type="safety_control",
        entity_type="strategy",
        entity_id=None,
        action="resume_candidate_strategies",
        status="complete",
        message=reason,
        payload={"affected_strategy_ids": affected_ids, "new_status": "paper_trading_candidate"},
    )
    db.commit()
    return {
        "action": "resume_candidate_strategies",
        "status": "complete",
        "message": reason,
        "kill_switch_enabled": bool((_active_risk_rule(db).value or {}).get("kill_switch_enabled", False)),
        "paused_strategies": len(affected_ids),
        "affected_strategy_ids": affected_ids,
        "risk_rule": None,
    }
