from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.models import RiskRule
from app.services.audit import write_audit_log
from app.services.risk import DEFAULT_RISK_RULES


UPDATABLE_RISK_FIELDS = {
    "min_confidence",
    "max_daily_drawdown",
    "max_strategy_drawdown",
    "max_open_positions",
    "max_open_positions_per_strategy",
    "max_symbol_exposure",
    "max_risk_per_trade",
    "stop_after_consecutive_losses",
    "candidate_review_score_threshold",
    "activation_score_threshold",
    "journal_feedback_review_threshold_cap",
    "journal_feedback_allocation_multiplier_cap",
    "memory_replay_min_complete_samples",
    "memory_replay_min_avg_return_delta",
    "memory_replay_min_hit_rate_delta",
}


def get_active_risk_rule(db: Session) -> RiskRule:
    rule = db.query(RiskRule).filter(RiskRule.is_active.is_(True)).order_by(RiskRule.id).first()
    if rule:
        return rule

    rule = RiskRule(name="default_paper_risk", value=DEFAULT_RISK_RULES.copy(), is_active=True)
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


def update_risk_settings(db: Session, updates: dict, reason: Optional[str] = None) -> RiskRule:
    rule = get_active_risk_rule(db)
    old_value = DEFAULT_RISK_RULES | ((rule.value or {}).copy())
    next_value = old_value.copy()

    for field, value in updates.items():
        if field in UPDATABLE_RISK_FIELDS and value is not None:
            next_value[field] = value

    next_value["paper_only"] = True
    next_value["kill_switch_enabled"] = bool(old_value.get("kill_switch_enabled", False))

    rule.value = next_value
    write_audit_log(
        db,
        event_type="risk_rule",
        entity_type="risk_rule",
        entity_id=rule.id,
        action="update_risk_settings",
        status="complete",
        message=reason or "Risk settings updated from control room.",
        payload={"old_value": old_value, "new_value": next_value, "updated_fields": sorted(updates.keys())},
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule
