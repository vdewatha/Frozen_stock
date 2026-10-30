from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models import AuditLog, RiskRule, Strategy
from app.services.audit import write_audit_log
from app.services.notifications import create_notification
from app.services.portfolio_risk import portfolio_risk_snapshot
from app.services.risk import DEFAULT_RISK_RULES
from app.services.risk_settings import get_active_risk_rule


KILL_SWITCH_BREACH_LABELS = {"Drawdown", "Open positions"}


def _latest_risk_action_audit(db: Session) -> Optional[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(AuditLog.event_type == "risk_alert_action", AuditLog.action == "evaluate_portfolio_breaches")
        .order_by(AuditLog.created_at.desc())
        .first()
    )


def _set_kill_switch_without_commit(db: Session, enabled: bool, reason: str, payload: dict) -> dict:
    rule = get_active_risk_rule(db)
    old_value = DEFAULT_RISK_RULES | (rule.value or {})
    new_value = old_value | {"kill_switch_enabled": enabled, "paper_only": True}
    rule.value = new_value
    write_audit_log(
        db,
        event_type="safety_control",
        entity_type="risk_rule",
        entity_id=rule.id,
        action="set_kill_switch",
        status="enabled" if enabled else "disabled",
        message=reason,
        payload=payload | {"old_value": old_value, "new_value": new_value},
    )
    return {"id": rule.id, "name": rule.name, "value": rule.value, "is_active": rule.is_active}


def _pause_strategies_without_commit(db: Session, reason: str, payload: dict) -> list[int]:
    strategies = db.query(Strategy).order_by(Strategy.name).all()
    affected_ids: list[int] = []
    old_statuses: dict[int, str] = {}
    for strategy in strategies:
        old_statuses[strategy.id] = strategy.current_status
        if strategy.current_status != "paused":
            strategy.current_status = "paused"
            strategy.updated_at = datetime.utcnow()
            affected_ids.append(strategy.id)

    write_audit_log(
        db,
        event_type="safety_control",
        entity_type="strategy",
        entity_id=None,
        action="pause_all_strategies",
        status="complete",
        message=reason,
        payload=payload | {"affected_strategy_ids": affected_ids, "old_statuses": old_statuses},
    )
    return affected_ids


def _serializable_snapshot(snapshot: dict) -> dict:
    return snapshot | {"generated_at": snapshot["generated_at"].isoformat()}


def evaluate_portfolio_risk_actions(db: Session, *, require_persistence: bool = True, dry_run: bool = False) -> dict:
    snapshot = portfolio_risk_snapshot(db)
    breach_alerts = [alert for alert in snapshot["alerts"] if alert.get("severity") == "breach"]
    breach_labels = sorted({str(alert.get("label")) for alert in breach_alerts})
    latest_audit = _latest_risk_action_audit(db)
    previous_labels = sorted((latest_audit.payload or {}).get("breach_labels", [])) if latest_audit else []
    persistent_labels = sorted(set(breach_labels).intersection(previous_labels)) if require_persistence else breach_labels
    kill_switch_labels = [label for label in persistent_labels if label in KILL_SWITCH_BREACH_LABELS]
    pause_labels = [label for label in persistent_labels if label not in KILL_SWITCH_BREACH_LABELS and label != "Kill switch"]
    actions_taken: list[dict] = []
    risk_rule: Optional[dict] = None
    affected_strategy_ids: list[int] = []

    if not breach_labels:
        status = "clear"
        message = "No portfolio breach alerts detected."
    elif not persistent_labels:
        status = "observed"
        message = "Portfolio breach observed; waiting for persistence before automated safety action."
    elif dry_run:
        status = "dry_run"
        message = "Persistent portfolio breach detected; dry run did not change controls."
    else:
        status = "complete"
        message = "Persistent portfolio breach detected; automated paper-trading safety action applied."
        action_payload = {"breach_labels": breach_labels, "persistent_labels": persistent_labels, "source": "portfolio_risk_actions"}
        if kill_switch_labels and not bool(snapshot["risk_limits"].get("kill_switch_enabled", False)):
            risk_rule = _set_kill_switch_without_commit(
                db,
                True,
                f"Automated kill switch after persistent portfolio breach: {', '.join(kill_switch_labels)}.",
                action_payload,
            )
            actions_taken.append({"action": "set_kill_switch", "labels": kill_switch_labels})
        if pause_labels:
            affected_strategy_ids = _pause_strategies_without_commit(
                db,
                f"Automated strategy pause after persistent portfolio breach: {', '.join(pause_labels)}.",
                action_payload,
            )
            actions_taken.append({"action": "pause_all_strategies", "labels": pause_labels, "affected_strategy_ids": affected_strategy_ids})
        if not actions_taken:
            message = "Persistent breach detected; controls were already in the required safety state."

    write_audit_log(
        db,
        event_type="risk_alert_action",
        entity_type="portfolio",
        entity_id=None,
        action="evaluate_portfolio_breaches",
        status=status,
        message=message,
        payload={
            "breach_labels": breach_labels,
            "previous_breach_labels": previous_labels,
            "persistent_labels": persistent_labels,
            "actions_taken": actions_taken,
            "dry_run": dry_run,
            "require_persistence": require_persistence,
            "snapshot": {
                "generated_at": snapshot["generated_at"].isoformat(),
                "open_positions": snapshot["open_positions"],
                "gross_exposure": snapshot["gross_exposure"],
                "total_unrealized_pl_pct": snapshot["total_unrealized_pl_pct"],
                "risk_limits": snapshot["risk_limits"],
                "alerts": snapshot["alerts"],
            },
        },
    )
    if status in {"observed", "dry_run", "complete"} and breach_labels:
        create_notification(
            db,
            category="risk_alert",
            severity="critical" if persistent_labels else "warning",
            source="portfolio_risk_actions",
            title="Portfolio risk breach detected",
            message=message,
            entity_type="portfolio",
            payload={
                "breach_labels": breach_labels,
                "persistent_labels": persistent_labels,
                "actions_taken": actions_taken,
                "dry_run": dry_run,
            },
        )
    db.commit()

    return {
        "status": status,
        "message": message,
        "breach_labels": breach_labels,
        "previous_breach_labels": previous_labels,
        "persistent_labels": persistent_labels,
        "actions_taken": actions_taken,
        "kill_switch_enabled": bool((risk_rule["value"] if risk_rule else snapshot["risk_limits"]).get("kill_switch_enabled", False)),
        "affected_strategy_ids": affected_strategy_ids,
        "snapshot": _serializable_snapshot(snapshot),
    }


RISK_RESET_SOURCE = "risk_action_drawdown_reset"


def _as_utc(value: datetime) -> datetime:
    from datetime import timezone

    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def reset_drawdown_kill_switch_after_window(db: Session, *, now: Optional[datetime] = None) -> dict:
    """Clear a risk-action kill switch set only for drawdown, once its window resets.

    Applies only when every condition holds; otherwise an operator is needed:

    * the newest kill-switch change is the automated portfolio-risk enable,
      and its persistent labels were exactly ``Drawdown`` (open-position
      breaches never auto-reset);
    * the newest portfolio risk evaluation after it is ``clear``;
    * the current New York date is later than the enable's date (the daily
      drawdown window has reset); and
    * stock paper recovery is not halted (a recovery halt owns the switch).
    """
    from datetime import timezone

    from app.models import StockPaperRecoveryState
    from app.services.intraday_data import NY

    now = now or datetime.now(timezone.utc)
    rule = get_active_risk_rule(db)
    if not bool((rule.value or {}).get("kill_switch_enabled")):
        return {"status": "not_engaged"}
    latest_switch = (
        db.query(AuditLog)
        .filter(AuditLog.event_type == "safety_control", AuditLog.action == "set_kill_switch")
        .order_by(AuditLog.id.desc())
        .first()
    )
    payload = (latest_switch.payload or {}) if latest_switch else {}
    if (
        latest_switch is None
        or latest_switch.status != "enabled"
        or payload.get("source") != "portfolio_risk_actions"
        or sorted(payload.get("persistent_labels") or []) != ["Drawdown"]
    ):
        return {"status": "needs_operator", "reason": "Kill switch was not engaged solely by an automated drawdown action"}
    enabled_at = _as_utc(latest_switch.created_at)
    if now.astimezone(NY).date() <= enabled_at.astimezone(NY).date():
        return {"status": "waiting", "reason": "Daily drawdown window has not reset"}
    evaluation = _latest_risk_action_audit(db)
    if evaluation is None or evaluation.status != "clear" or _as_utc(evaluation.created_at) <= enabled_at:
        return {"status": "waiting", "reason": "No clear portfolio risk evaluation since the drawdown action"}
    recovery = db.get(StockPaperRecoveryState, 1)
    if recovery is not None and recovery.status not in {"armed", "resumable"}:
        return {"status": "waiting", "reason": f"Stock paper recovery is {recovery.status}"}
    reason = "Drawdown kill switch reset automatically after the daily window reset with a clear risk evaluation."
    _set_kill_switch_without_commit(db, False, reason, {
        "source": RISK_RESET_SOURCE, "enabled_audit_id": latest_switch.id, "evaluation_audit_id": evaluation.id,
    })
    create_notification(db, category="risk_alert", severity="info", source=RISK_RESET_SOURCE,
                        title="Drawdown kill switch reset", message=reason, entity_type="portfolio",
                        payload={"enabled_audit_id": latest_switch.id})
    db.commit()
    return {"status": "reset", "reason": reason, "enabled_audit_id": latest_switch.id}
