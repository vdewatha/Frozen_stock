from __future__ import annotations

import math
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


# Mirrors RiskSettingsUpdateRequest so service-level callers (jobs, scripts)
# cannot store values the API would reject, e.g. NaN, which silently disables
# every ``<`` / ``>`` comparison in approve_trade.
RISK_FIELD_BOUNDS: dict[str, tuple[type, float, float]] = {
    "min_confidence": (float, 0, 1),
    "max_daily_drawdown": (float, 0, 1),
    "max_strategy_drawdown": (float, 0, 1),
    "max_open_positions": (int, 1, 100),
    "max_open_positions_per_strategy": (int, 1, 100),
    "max_symbol_exposure": (float, 0, 1),
    "max_risk_per_trade": (float, 0, 1),
    "stop_after_consecutive_losses": (int, 1, 100),
    "candidate_review_score_threshold": (float, 0, 2),
    "activation_score_threshold": (float, 0, 2),
    "journal_feedback_review_threshold_cap": (float, 0, 0.25),
    "journal_feedback_allocation_multiplier_cap": (float, 0, 1),
    "memory_replay_min_complete_samples": (int, 0, 1000),
    "memory_replay_min_avg_return_delta": (float, -1, 1),
    "memory_replay_min_hit_rate_delta": (float, -1, 1),
}


def _validated_risk_value(field: str, value):
    kind, lower, upper = RISK_FIELD_BOUNDS[field]
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric, not boolean.")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric.") from exc
    if not math.isfinite(numeric) or numeric < lower or numeric > upper:
        raise ValueError(f"{field} must be a finite value between {lower} and {upper}.")
    if kind is int:
        if numeric != int(numeric):
            raise ValueError(f"{field} must be a whole number.")
        return int(numeric)
    return numeric


def get_active_risk_rule(db: Session) -> RiskRule:
    rule = db.query(RiskRule).filter(RiskRule.is_active.is_(True)).order_by(RiskRule.id).first()
    if rule:
        return rule

    rule = RiskRule(name="default_paper_risk", value=DEFAULT_RISK_RULES.copy(), is_active=True)
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


def update_risk_settings(
    db: Session,
    updates: dict,
    reason: Optional[str] = None,
    *,
    actor: str = "system",
    authorization: dict | None = None,
) -> RiskRule:
    validated = {
        field: _validated_risk_value(field, value)
        for field, value in updates.items()
        if field in UPDATABLE_RISK_FIELDS and value is not None
    }
    rule = get_active_risk_rule(db)
    old_value = DEFAULT_RISK_RULES | ((rule.value or {}).copy())
    next_value = old_value.copy()
    next_value.update(validated)

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
        payload={
            "old_value": old_value,
            "new_value": next_value,
            "updated_fields": sorted(updates.keys()),
            "actor": actor,
            "authorization": authorization or {},
        },
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule
