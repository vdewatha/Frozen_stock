"""Bounded, fail-closed live operations projections.

This module is deliberately read-only.  It joins durable broker, safety,
monitoring, scheduler, and audit evidence without returning provider payloads
or credentials.  A missing observation is represented as ``unknown`` rather
than being treated as healthy.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from statistics import mean
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import (
    Asset,
    AuditLog,
    LiveBrokerAccount,
    LiveBrokerActivity,
    LiveBrokerFill,
    LiveBrokerLedgerEvent,
    LiveBrokerOrder,
    LiveBrokerPosition,
    LiveSafetyState,
    Notification,
    RiskRule,
    StockMonitoringSnapshot,
)
from app.services.intraday_data import feed_status
from app.services.live_safety import evaluate_live_safety
from app.services.operational_hardening import operational_hardening_snapshot
from app.services.readiness import _scheduler_health

UTC = timezone.utc
MAX_ITEMS = 50
ORDER_TERMINAL = frozenset({"filled", "canceled", "cancelled", "expired", "rejected", "done_for_day"})
SENSITIVE_KEY_PARTS = ("secret", "password", "token", "credential", "private", "authorization")


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return _aware(value).isoformat() if value else None


def _component(status: str, reason: str, *, observed_at: datetime | None = None, **details: Any) -> dict:
    return {
        "status": status,
        "reason": reason,
        "observed_at": _iso(observed_at),
        "details": details,
    }


def _safe_payload_keys(payload: dict | None) -> list[str]:
    return sorted(
        str(key)
        for key in (payload or {}).keys()
        if not any(part in str(key).lower() for part in SENSITIVE_KEY_PARTS)
    )


def _status_from_feed(value: dict) -> str:
    if value.get("status") == "ready":
        return "healthy"
    if value.get("status") in {"stale", "incomplete", "degraded"}:
        return "degraded"
    if value.get("status") in {"blocked", "unavailable"}:
        return "blocked"
    return "unknown"


def _live_mode(db: Session, safety: dict) -> dict:
    mode = safety.get("mode")
    if mode in {"canary-live", "approved-live"} and safety.get("live_authorized"):
        status = "healthy"
        reason = "Live safety is authorized by the current evidence gates."
    elif mode == "emergency-stop":
        status = "blocked"
        reason = "Emergency stop is active; live orders are blocked."
    elif mode:
        status = "blocked"
        reason = f"Live mode is {mode}; live orders are not authorized."
    else:
        status = "unknown"
        reason = "Live safety mode has not been observed."
    return _component(
        status,
        reason,
        observed_at=safety.get("updated_at"),
        mode=mode,
        live_orders_allowed=bool(safety.get("live_orders_allowed")),
        live_authorized=bool(safety.get("live_authorized")),
        last_reason=safety.get("last_reason"),
    )


def _risk(db: Session, safety: dict, now: datetime) -> dict:
    rule = db.query(RiskRule).filter(RiskRule.is_active.is_(True)).order_by(RiskRule.id).first()
    account = db.query(LiveBrokerAccount).filter_by(broker="alpaca_live").one_or_none()
    if rule is None or account is None:
        return _component("unknown", "No active risk rule is available for live operation.", observed_at=now)
    values = rule.value or {}
    daily_drawdown = (account.raw_payload or {}).get("daily_drawdown")
    if values.get("kill_switch_enabled"):
        status, reason = "blocked", "The risk kill switch is active."
    elif daily_drawdown is not None and float(daily_drawdown) >= float(values.get("max_daily_drawdown", 0.03)):
        status, reason = "blocked", "Live daily drawdown has reached the configured risk limit."
    else:
        status, reason = "healthy", "An active risk rule is present and the kill switch is off."
    return _component(
        status,
        reason,
        observed_at=now,
        rule_id=rule.id,
        kill_switch_enabled=bool(values.get("kill_switch_enabled", False)),
        max_total_exposure=values.get("max_total_exposure"),
        max_symbol_exposure=values.get("max_symbol_exposure"),
        max_daily_drawdown=values.get("max_daily_drawdown"),
        daily_drawdown=daily_drawdown,
        safety_status=safety.get("status"),
    )


def _broker(db: Session, now: datetime) -> tuple[dict, dict]:
    account = db.query(LiveBrokerAccount).filter_by(broker="alpaca_live").one_or_none()
    if account is None:
        return (
            _component(
                "unknown",
                "No live broker reconciliation has produced durable account evidence.",
                broker="alpaca_live",
            ),
            {"status": "unknown", "account": None, "positions": [], "orders": [], "fills": []},
        )

    last = _aware(account.last_reconciled_at)
    age_seconds = (now - last).total_seconds() if last else None
    if account.status == "halted" or account.reconciliation_required or account.unexplained_residual:
        status = "blocked"
        reason = account.halt_reason or "Live broker reconciliation requires operator review."
    elif last is None:
        status = "unknown"
        reason = "Live broker account has no reconciliation timestamp."
    elif age_seconds is not None and age_seconds > 300:
        status = "degraded"
        reason = "Live broker reconciliation evidence is older than five minutes."
    elif account.status == "reconciled":
        status = "healthy"
        reason = "Live broker account is reconciled and has no known residual."
    else:
        status = "unknown"
        reason = f"Live broker account status is {account.status}."

    open_orders = (
        db.query(LiveBrokerOrder)
        .filter(LiveBrokerOrder.account_id == account.id, ~LiveBrokerOrder.status.in_(ORDER_TERMINAL))
        .order_by(LiveBrokerOrder.created_at.desc())
        .limit(MAX_ITEMS)
        .all()
    )
    positions = (
        db.query(LiveBrokerPosition)
        .filter_by(account_id=account.id)
        .order_by(LiveBrokerPosition.symbol)
        .limit(MAX_ITEMS)
        .all()
    )
    fills = (
        db.query(LiveBrokerFill)
        .filter_by(account_id=account.id)
        .order_by(LiveBrokerFill.filled_at.desc())
        .limit(MAX_ITEMS)
        .all()
    )
    account_view = {
        "broker": account.broker,
        "account_id": account.broker_account_id,
        "environment": account.environment,
        "currency": account.currency,
        "cash": str(account.cash),
        "buying_power": str(account.buying_power),
        "equity": str(account.equity),
        "status": account.status,
        "reconciliation_required": account.reconciliation_required,
        "unexplained_residual": account.unexplained_residual,
        "accounting_review_required": account.accounting_review_required,
        "last_reconciled_at": _iso(account.last_reconciled_at),
    }
    order_view = [
        {
            "id": row.id,
            "client_order_id": row.client_order_id,
            "broker_order_id": row.broker_order_id,
            "symbol": row.symbol,
            "side": row.side,
            "quantity": str(row.quantity),
            "status": row.status,
            "model_run_id": row.model_run_id,
            "signal_id": row.signal_id,
            "risk_decision_id": row.risk_decision_id,
            "actor": row.actor,
            "request_id": row.request_id,
            "uncertain_submission": row.uncertain_submission,
            "created_at": _iso(row.created_at),
            "submitted_at": _iso(row.submitted_at),
        }
        for row in open_orders
    ]
    position_view = [
        {
            "symbol": row.symbol,
            "quantity": str(row.quantity),
            "current_price": str(row.current_price) if row.current_price is not None else None,
            "market_value": str(row.market_value) if row.market_value is not None else None,
            "observed_at": _iso(row.observed_at),
        }
        for row in positions
    ]
    fill_view = [
        {
            "broker_activity_id": row.broker_activity_id,
            "broker_order_id": row.broker_order_id,
            "symbol": row.symbol,
            "side": row.side,
            "quantity": str(row.quantity),
            "price": str(row.price),
            "fee_known": row.fee is not None,
            "filled_at": _iso(row.filled_at),
        }
        for row in fills
    ]
    return (
        _component(
            status,
            reason,
            observed_at=last,
            broker=account.broker,
            account_status=account.status,
            reconciliation_age_seconds=round(age_seconds, 1) if age_seconds is not None else None,
            reconciliation_required=account.reconciliation_required,
        ),
        {
            "status": status,
            "account": account_view,
            "positions": position_view,
            "orders": order_view,
            "fills": fill_view,
        },
    )


def _data_feed(db: Session, now: datetime) -> dict:
    assets = (
        db.query(Asset)
        .filter(Asset.is_active.is_(True), Asset.asset_type == "stock")
        .order_by(Asset.symbol)
        .limit(MAX_ITEMS)
        .all()
    )
    if not assets:
        return _component("unknown", "No active stock data universe is configured.", symbols={})
    symbols: dict[str, dict] = {}
    statuses: list[str] = []
    for asset in assets:
        try:
            value = feed_status(db, asset.symbol)
            status = _status_from_feed(value)
            statuses.append(status)
            symbols[asset.symbol] = {
                "status": status,
                "provider": value.get("provider"),
                "feed_class": value.get("feed_class"),
                "exchange_timestamp": _iso(value.get("exchange_timestamp")),
                "ingestion_timestamp": _iso(value.get("ingestion_timestamp")),
                "latency_seconds": value.get("latency_seconds"),
                "missing_intervals": list(value.get("missing_intervals") or [])[:20],
                "deferred_window": value.get("deferred_window"),
                "oldest_unresolved_interval": value.get("oldest_unresolved_interval"),
                "reason": value.get("unavailable_reason"),
            }
        except Exception as exc:
            statuses.append("unknown")
            symbols[asset.symbol] = {"status": "unknown", "reason": f"Feed check failed: {exc.__class__.__name__}"}
    if "blocked" in statuses:
        overall, reason = "blocked", "At least one active stock feed is unavailable."
    elif "degraded" in statuses:
        overall, reason = "degraded", "At least one active stock feed is stale or incomplete."
    elif "unknown" in statuses:
        overall, reason = "unknown", "At least one active stock feed has no conclusive observation."
    else:
        overall, reason = "healthy", "All active stock feeds are fresh and complete."
    return _component(overall, reason, observed_at=now, symbols=symbols)


def _workers() -> dict:
    try:
        scheduler = _scheduler_health()
    except Exception as exc:
        return _component("unknown", "Worker and scheduler health could not be checked.", error_type=exc.__class__.__name__)
    if scheduler.get("healthy") and not scheduler.get("recent_unresolved_failures"):
        status = "healthy"
        reason = "Workers and exactly one beat scheduler are evidenced."
    elif scheduler.get("worker_count") or scheduler.get("beat_count"):
        status = "degraded"
        reason = "Some worker or scheduler evidence exists, but the complete runtime is not healthy."
    else:
        status = "blocked"
        reason = "No worker and beat evidence is available."
    return _component(
        status,
        reason,
        worker_count=scheduler.get("worker_count"),
        beat_count=scheduler.get("beat_count"),
        recent_unresolved_failures=scheduler.get("recent_unresolved_failures"),
        worker_error=scheduler.get("worker_error"),
        beat_error=scheduler.get("beat_error"),
    )


def _monitoring(db: Session, now: datetime) -> dict:
    snapshot = (
        db.query(StockMonitoringSnapshot)
        .filter_by(monitor_key="stock_continuous_monitor")
        .order_by(StockMonitoringSnapshot.generated_at.desc())
        .first()
    )
    if snapshot is None:
        return _component("unknown", "No monitoring snapshot has been recorded.", snapshot_id=None)
    generated = _aware(snapshot.generated_at)
    age = (now - generated).total_seconds() if generated else None
    if snapshot.status == "breach":
        status, reason = "blocked", "Continuous monitoring has an active breach."
    elif snapshot.status in {"warning", "unknown"}:
        status, reason = "degraded", "Continuous monitoring is not clear."
    elif age is None or age > 600:
        status, reason = "degraded", "The latest monitoring snapshot is older than ten minutes."
    else:
        status, reason = "healthy", "Continuous monitoring is clear and current."
    return _component(
        status,
        reason,
        observed_at=generated,
        snapshot_id=snapshot.id,
        snapshot_status=snapshot.status,
        age_seconds=round(age, 1) if age is not None else None,
        check_count=len(snapshot.checks or []),
        action_count=len(snapshot.actions or []),
    )


def _model_and_recovery(db: Session, safety: dict) -> tuple[dict, dict]:
    model_gate = safety.get("gates", {}).get("immutable_model_lineage", {})
    recovery_gate = safety.get("gates", {}).get("recovery_control", {})
    model_status = "healthy" if model_gate.get("status") == "pass" else (
        "unknown" if model_gate.get("status") == "unknown" else "blocked"
    )
    model = _component(
        model_status,
        model_gate.get("reason") or "Immutable live model lineage is verified.",
        observed_at=safety.get("updated_at"),
        gate_status=model_gate.get("status"),
        evidence=model_gate.get("evidence", {}),
    )
    recovery_status = "healthy" if recovery_gate.get("status") == "pass" else (
        "unknown" if recovery_gate.get("status") == "unknown" else "blocked"
    )
    recovery = _component(
        recovery_status,
        recovery_gate.get("reason") or "Live recovery controls are clear.",
        observed_at=safety.get("updated_at"),
        gate_status=recovery_gate.get("status"),
        evidence=recovery_gate.get("evidence", {}),
    )
    return model, recovery


def _audit_and_hardening(db: Session) -> tuple[dict, dict]:
    latest = db.query(AuditLog).order_by(AuditLog.created_at.desc()).first()
    if latest is None:
        audit = _component("unknown", "No attributable audit evidence has been recorded.", event_count=0)
    else:
        event_count = db.query(func.count(AuditLog.id)).scalar() or 0
        audit = _component(
            "healthy",
            "Attributable audit evidence is available.",
            observed_at=latest.created_at,
            event_count=event_count,
            latest_event_type=latest.event_type,
            latest_action=latest.action,
            latest_event_digest=latest.event_sha256,
        )
    try:
        report = operational_hardening_snapshot(db)
    except Exception as exc:
        hardening = _component(
            "unknown",
            "Operational hardening checks could not be completed.",
            error_type=exc.__class__.__name__,
        )
    else:
        status = "healthy" if report["status"] == "clear" else "blocked" if report["status"] == "breach" else "unknown"
        hardening = _component(
            status,
            "Backup, restore, lease, and configuration checks are clear."
            if status == "healthy"
            else "Backup, restore, lease, or configuration checks need review.",
            observed_at=report.get("generated_at"),
            hardening_status=report["status"],
            checks=[
                {"key": check["key"], "status": check["status"], "message": check["message"]}
                for check in report.get("checks", [])
            ],
            configuration_sha256=(report.get("configuration_digest") or {}).get("sha256"),
            incident_procedure=report.get("incident_procedure"),
        )
    return audit, hardening


def _metrics(db: Session, now: datetime, broker_view: dict) -> dict:
    since = now - timedelta(hours=24)
    account = db.query(LiveBrokerAccount).filter_by(broker="alpaca_live").one_or_none()
    if account is None:
        return {
            "status": "unknown",
            "reason": "Live order metrics are unavailable before account reconciliation.",
            "observed_at": None,
            "orders_last_24h": 0,
            "rejected_orders_last_24h": 0,
            "uncertain_orders": 0,
            "open_orders": 0,
            "average_submission_latency_seconds": None,
            "max_submission_latency_seconds": None,
            "repeated_retry_events_last_24h": 0,
            "exposure": {
                "gross_market_value": None,
                "gross_exposure_ratio": None,
                "equity": None,
            },
        }
    orders = db.query(LiveBrokerOrder).filter(
        LiveBrokerOrder.account_id == account.id,
        LiveBrokerOrder.created_at >= since,
    ).all()
    latencies = [
        (_aware(row.submitted_at) - _aware(row.submission_attempted_at)).total_seconds()
        for row in orders
        if row.submitted_at and row.submission_attempted_at and _aware(row.submitted_at) >= _aware(row.submission_attempted_at)
    ]
    rejected = sum(row.status == "rejected" for row in orders)
    uncertain = sum(bool(row.uncertain_submission) or row.status == "unknown" for row in orders)
    retries = db.query(LiveBrokerLedgerEvent).filter(
        LiveBrokerLedgerEvent.account_id == account.id,
        LiveBrokerLedgerEvent.created_at >= since,
        LiveBrokerLedgerEvent.event_type.in_(["order_retry", "order_submit"]),
    ).count()
    equity = Decimal(str(account.equity)) if account.equity is not None else Decimal("0")
    gross_market_value = sum(
        (abs(Decimal(str(item.get("market_value") or "0")) for item in broker_view.get("positions", []))),
        Decimal("0"),
    )
    exposure_ratio = gross_market_value / equity if equity > 0 else None
    status = "blocked" if rejected or uncertain else "healthy"
    reason = "Rejected or uncertain live orders require review." if status == "blocked" else "No rejected or uncertain live orders were observed."
    return {
        "status": status,
        "reason": reason,
        "observed_at": _iso(now),
        "orders_last_24h": len(orders),
        "rejected_orders_last_24h": rejected,
        "uncertain_orders": uncertain,
        "open_orders": len(broker_view.get("orders", [])),
        "average_submission_latency_seconds": round(mean(latencies), 3) if latencies else None,
        "max_submission_latency_seconds": round(max(latencies), 3) if latencies else None,
        "repeated_retry_events_last_24h": retries,
        "exposure": {
            "gross_market_value": str(gross_market_value),
            "gross_exposure_ratio": str(exposure_ratio) if exposure_ratio is not None else None,
            "equity": str(account.equity) if account.equity is not None else None,
        },
    }


def _alerts(db: Session, components: dict, now: datetime) -> list[dict]:
    alerts: list[dict] = []
    notifications = (
        db.query(Notification)
        .filter(Notification.status != "resolved")
        .order_by(Notification.created_at.desc())
        .limit(MAX_ITEMS)
        .all()
    )
    for item in notifications:
        alerts.append(
            {
                "id": item.id,
                "state": "blocked" if item.severity == "critical" else "degraded",
                "severity": item.severity,
                "title": item.title,
                "reason": item.message or "Operator review is required.",
                "observed_at": _iso(item.created_at),
                "acknowledged": item.acknowledged_at is not None,
                "source": item.source,
            }
        )
    for name, component in components.items():
        if component["status"] == "healthy":
            continue
        alerts.append(
            {
                "id": None,
                "state": "blocked" if component["status"] == "blocked" else component["status"],
                "severity": "critical" if component["status"] == "blocked" else "warning",
                "title": f"{name.replace('_', ' ').title()} is {component['status']}",
                "reason": component["reason"],
                "observed_at": component.get("observed_at") or _iso(now),
                "acknowledged": False,
                "source": "live_operations_projection",
            }
        )
    return alerts[:MAX_ITEMS]


def live_operations_snapshot(db: Session) -> dict:
    now = _now()
    safety = evaluate_live_safety(db, now=now)
    broker_component, broker_view = _broker(db, now)
    model, recovery = _model_and_recovery(db, safety)
    audit, hardening = _audit_and_hardening(db)
    components = {
        "live_mode": _live_mode(db, safety),
        "broker": broker_component,
        "data_freshness": _data_feed(db, now),
        "workers": _workers(),
        "account_reconciliation": broker_component,
        "risk": _risk(db, safety, now),
        "model": model,
        "monitoring": _monitoring(db, now),
        "recovery": recovery,
        "audit": audit,
        "configuration_and_restore": hardening,
    }
    metrics = _metrics(db, now, broker_view)
    if metrics["status"] != "healthy":
        components["execution_metrics"] = _component(
            metrics["status"],
            metrics["reason"],
            observed_at=now,
            **{key: value for key, value in metrics.items() if key not in {"status", "reason", "observed_at"}},
        )
    statuses = [item["status"] for item in components.values()]
    overall = (
        "blocked" if "blocked" in statuses
        else "degraded" if "degraded" in statuses
        else "uncertain" if "unknown" in statuses
        else "healthy"
    )
    return {
        "generated_at": now,
        "status": overall,
        "mode": safety.get("mode"),
        "live_orders_allowed": bool(safety.get("live_orders_allowed")),
        "components": components,
        "metrics": metrics,
        "account": broker_view["account"],
        "open_orders": broker_view["orders"],
        "positions": broker_view["positions"],
        "recent_fills": broker_view["fills"],
        "alerts": _alerts(db, components, now),
        "safety": {
            "status": safety.get("status"),
            "last_reason": safety.get("last_reason"),
            "updated_at": _iso(safety.get("updated_at")),
            "gates": {
                key: {"status": value.get("status"), "reason": value.get("reason")}
                for key, value in (safety.get("gates") or {}).items()
            },
        },
        "limits": {"max_components": len(components), "max_alerts": MAX_ITEMS, "max_orders": MAX_ITEMS, "max_positions": MAX_ITEMS, "max_fills": MAX_ITEMS},
    }


def live_operations_evidence(db: Session, *, limit: int = MAX_ITEMS) -> dict:
    """Export attributable proof summaries without raw broker payloads."""
    bounded = max(1, min(limit, 200))
    audit_rows = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(bounded).all()
    ledger_rows = db.query(LiveBrokerLedgerEvent).order_by(LiveBrokerLedgerEvent.created_at.desc()).limit(bounded).all()
    events = [
        {
            "source": "audit",
            "id": row.id,
            "event_type": row.event_type,
            "entity_type": row.entity_type,
            "entity_id": row.entity_id,
            "action": row.action,
            "status": row.status,
            "message": row.message,
            "created_at": _iso(row.created_at),
            "actor": (row.payload or {}).get("actor"),
            "correlation_id": (row.payload or {}).get("request_id") or (row.payload or {}).get("correlation_id"),
            "proof_digest": row.event_sha256,
            "payload_keys": _safe_payload_keys(row.payload),
        }
        for row in audit_rows
    ]
    events.extend(
        {
            "source": "live_broker_ledger",
            "id": row.id,
            "event_type": row.event_type,
            "entity_type": "live_broker",
            "entity_id": row.account_id,
            "action": row.event_type,
            "status": row.status,
            "message": row.reason,
            "created_at": _iso(row.created_at),
            "actor": row.actor,
            "correlation_id": (row.payload or {}).get("request_id"),
            "proof_digest": None,
            "payload_keys": _safe_payload_keys(row.payload),
        }
        for row in ledger_rows
    )
    events.sort(key=lambda item: item["created_at"] or "", reverse=True)
    return {
        "generated_at": _now(),
        "retention": "append-only source records; export is bounded and redacted",
        "events": events[:bounded],
        "redaction": ["credentials", "raw broker payloads", "account credential fields", "unnecessary financial payload fields"],
    }