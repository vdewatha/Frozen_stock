"""Continuous, fail-closed monitoring for the Alpaca paper stock path."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from statistics import mean, pstdev
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Asset,
    ModelPrediction,
    Notification,
    RiskRule,
    StockModelLifecycleState,
    StockMonitoringBreach,
    StockMonitoringSnapshot,
    StockPaperBindingState,
)
from app.models.stock_paper import (
    StockPaperAccount,
    StockPaperEquitySnapshot,
    StockPaperFill,
    StockPaperLedgerEvent,
    StockPaperOrder,
    StockPaperPosition,
)
from app.services.audit import write_audit_log
from app.services.intraday_data import feed_status
from app.services.notifications import create_notification
from app.services.risk import DEFAULT_RISK_RULES
from app.services.stock_recovery import enter_stock_recovery, record_stock_monitor_heartbeat
from app.services.stock_training_jobs import StockTrainingError, transition_stock_model_lifecycle
from app.services.trusted_data import TRUSTED_SOURCES, UntrustedMarketData, trusted_history

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
MONITOR_KEY = "stock_continuous_monitor"
PERSISTENCE_WINDOW = timedelta(minutes=20)
MIN_PREDICTIONS = 10


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)) and float(value) == float(value):
        return float(value)
    return None


def _flatten_numeric(value: object, prefix: str = "") -> dict[str, float]:
    if isinstance(value, dict):
        flattened: dict[str, float] = {}
        for key, nested in value.items():
            if key == "macro_context":
                continue
            flattened.update(_flatten_numeric(nested, f"{prefix}.{key}" if prefix else str(key)))
        return flattened
    numeric = _number(value)
    return {prefix: numeric} if numeric is not None and prefix else {}


def _check(
    key: str,
    category: str,
    metric: str,
    status: str,
    message: str,
    *,
    value: dict | None = None,
    threshold: dict | None = None,
    details: dict | None = None,
    action_scope: str = "none",
) -> dict:
    return {
        "key": key,
        "category": category,
        "metric": metric,
        "status": status,
        "message": message,
        "value": value or {},
        "threshold": threshold or {},
        "details": details or {},
        "action_scope": action_scope,
    }


def _distribution_drift(db: Session) -> list[dict]:
    rows = db.query(ModelPrediction).order_by(ModelPrediction.created_at.desc()).limit(180).all()
    recent = rows[:30]
    baseline = rows[30:120]
    results: list[dict] = []
    if len(recent) < MIN_PREDICTIONS or len(baseline) < MIN_PREDICTIONS:
        results.append(_check(
            "model.prediction_distribution", "model", "prediction_distribution_drift", "unknown",
            "Prediction-distribution drift is unavailable until recent and baseline prediction windows exist.",
            value={"recent_count": len(recent), "baseline_count": len(baseline)},
            threshold={"minimum_samples_per_window": MIN_PREDICTIONS, "warning_mean_shift": 0.10, "breach_mean_shift": 0.20},
            details={"source": "persisted trusted model predictions"},
            action_scope="model",
        ))
    else:
        recent_mean = mean(float(row.probability_up) for row in recent)
        baseline_mean = mean(float(row.probability_up) for row in baseline)
        shift = abs(recent_mean - baseline_mean)
        status = "breach" if shift >= 0.20 else "warning" if shift >= 0.10 else "clear"
        results.append(_check(
            "model.prediction_distribution", "model", "prediction_distribution_drift", status,
            "Prediction probability distribution is outside the configured drift threshold."
            if status != "clear" else "Prediction probability distribution is within the configured baseline range.",
            value={"recent_mean_probability_up": round(recent_mean, 6), "baseline_mean_probability_up": round(baseline_mean, 6), "absolute_mean_shift": round(shift, 6)},
            threshold={"warning_mean_shift": 0.10, "breach_mean_shift": 0.20},
            details={"recent_count": len(recent), "baseline_count": len(baseline)},
            action_scope="model",
        ))
    feature_rows = [row for row in rows if row.features]
    recent_features = [_flatten_numeric(row.features or {}) for row in feature_rows[:30]]
    baseline_features = [_flatten_numeric(row.features or {}) for row in feature_rows[30:120]]
    feature_names = sorted(set().union(*(row.keys() for row in recent_features), *(row.keys() for row in baseline_features)))
    if len(recent_features) < MIN_PREDICTIONS or len(baseline_features) < MIN_PREDICTIONS or not feature_names:
        results.append(_check(
            "model.input_features", "model", "input_feature_drift", "unknown",
            "Input-feature drift is unavailable until persisted feature baselines contain enough numeric observations.",
            value={"recent_count": len(recent_features), "baseline_count": len(baseline_features), "feature_count": len(feature_names)},
            threshold={"minimum_samples_per_window": MIN_PREDICTIONS, "warning_max_standardized_shift": 2.0, "breach_max_standardized_shift": 3.0},
            details={"source": "prediction feature payloads", "excluded": ["macro_context"]},
            action_scope="model",
        ))
    else:
        shifts: dict[str, float] = {}
        for name in feature_names:
            recent_values = [row[name] for row in recent_features if name in row]
            baseline_values = [row[name] for row in baseline_features if name in row]
            if len(recent_values) < MIN_PREDICTIONS or len(baseline_values) < MIN_PREDICTIONS:
                continue
            deviation = pstdev(baseline_values)
            shifts[name] = abs(mean(recent_values) - mean(baseline_values)) / max(deviation, 1e-9)
        if not shifts:
            status = "unknown"
            max_shift = None
        else:
            max_shift = max(shifts.values())
            status = "breach" if max_shift >= 3 else "warning" if max_shift >= 2 else "clear"
        results.append(_check(
            "model.input_features", "model", "input_feature_drift", status,
            "At least one input feature has materially shifted from its persisted baseline."
            if status != "clear" and status != "unknown" else
            "Input features remain within their persisted baseline range." if status == "clear"
            else "Input-feature drift is unavailable for the current feature payloads.",
            value={"max_standardized_shift": round(max_shift, 6) if max_shift is not None else None, "features": {key: round(value, 6) for key, value in sorted(shifts.items(), key=lambda item: item[1], reverse=True)[:10]}},
            threshold={"warning_max_standardized_shift": 2.0, "breach_max_standardized_shift": 3.0},
            details={"recent_count": len(recent_features), "baseline_count": len(baseline_features), "feature_count": len(shifts)},
            action_scope="model",
        ))
    return results


def _performance_drift(db: Session) -> dict:
    rows = db.query(ModelPrediction).filter(ModelPrediction.is_realized.is_(True)).order_by(ModelPrediction.prediction_date.desc()).limit(180).all()
    recent, baseline = rows[:30], rows[30:120]
    if len(recent) < MIN_PREDICTIONS or len(baseline) < MIN_PREDICTIONS:
        return _check(
            "model.realized_performance", "model", "calibration_and_realized_performance", "unknown",
            "Calibration and realized-performance degradation is unavailable until two realized windows exist.",
            value={"recent_count": len(recent), "baseline_count": len(baseline)},
            threshold={"minimum_samples_per_window": MIN_PREDICTIONS, "warning_brier_degradation": 0.05, "breach_brier_degradation": 0.10, "warning_hit_rate_drop": 0.10, "breach_hit_rate_drop": 0.15},
            details={"costs": "not used; this is prediction calibration evidence"},
            action_scope="model",
        )
    recent_brier = mean(float(row.brier_score) for row in recent if row.brier_score is not None)
    base_brier = mean(float(row.brier_score) for row in baseline if row.brier_score is not None)
    recent_hit = mean(bool(row.realized_up) == (float(row.probability_up) >= 0.5) for row in recent)
    base_hit = mean(bool(row.realized_up) == (float(row.probability_up) >= 0.5) for row in baseline)
    brier_delta, hit_delta = recent_brier - base_brier, recent_hit - base_hit
    status = "breach" if brier_delta >= 0.10 or hit_delta <= -0.15 else "warning" if brier_delta >= 0.05 or hit_delta <= -0.10 else "clear"
    return _check(
        "model.realized_performance", "model", "calibration_and_realized_performance", status,
        "Calibration or realized hit-rate degradation is persistent enough to review."
        if status != "clear" else "Calibration and realized performance remain within baseline bounds.",
        value={"recent_brier": round(recent_brier, 6), "baseline_brier": round(base_brier, 6), "brier_degradation": round(brier_delta, 6), "recent_hit_rate": round(recent_hit, 6), "baseline_hit_rate": round(base_hit, 6), "hit_rate_delta": round(hit_delta, 6)},
        threshold={"warning_brier_degradation": 0.05, "breach_brier_degradation": 0.10, "warning_hit_rate_drop": -0.10, "breach_hit_rate_drop": -0.15},
        details={"recent_count": len(recent), "baseline_count": len(baseline)},
        action_scope="model",
    )


def _freshness_and_provenance(db: Session) -> dict:
    assets = db.query(Asset).filter(Asset.is_active.is_(True), Asset.asset_type == "stock").order_by(Asset.symbol).all()
    if not assets:
        return _check("data.freshness_provenance", "data", "data_freshness_and_provenance", "unknown", "No active stock universe is configured.", action_scope="safety")
    results, failures = {}, {}
    for asset in assets:
        try:
            status = feed_status(db, asset.symbol)
            results[asset.symbol] = {
                "status": status.get("status"),
                "provider": status.get("provider"),
                "feed_class": status.get("feed_class"),
                "exchange_timestamp": status.get("exchange_timestamp").isoformat() if status.get("exchange_timestamp") else None,
                "ingestion_timestamp": status.get("ingestion_timestamp").isoformat() if status.get("ingestion_timestamp") else None,
                "missing_intervals": status.get("missing_intervals", []),
                "unavailable_reason": status.get("unavailable_reason"),
            }
            if status.get("status") != "ready":
                failures[asset.symbol] = status.get("status", "unavailable")
        except Exception as exc:
            failures[asset.symbol] = exc.__class__.__name__
    status = "breach" if failures else "clear"
    return _check(
        "data.freshness_provenance", "data", "data_freshness_and_provenance", status,
        "Fresh trusted Alpaca SIP data is not available for every active stock."
        if failures else "Freshness and provider provenance are valid for the active stock universe.",
        value={"failed_symbols": sorted(failures), "ready_symbols": sorted(set(results) - set(failures))},
        threshold={"provider": "alpaca", "feed_class": "sip", "timeframe": "1m", "session": "regular"},
        details={"symbols": results, "failures": failures},
        action_scope="safety",
    )


def _execution_divergence(db: Session, now: datetime) -> dict:
    since = now - timedelta(days=7)
    rows = db.query(StockPaperFill, StockPaperOrder).join(
        StockPaperOrder, StockPaperOrder.id == StockPaperFill.order_id
    ).filter(StockPaperFill.filled_at >= since, StockPaperOrder.limit_price.is_not(None)).all()
    if not rows:
        return _check("execution.fill_divergence", "execution", "slippage_and_fill_divergence", "unknown", "Fill divergence is unavailable because no broker fills have a matched paper reference price.", threshold={"warning_p95_slippage": 0.005, "breach_p95_slippage": 0.01}, action_scope="safety")
    slippages = []
    for fill, order in rows:
        reference = float(order.limit_price)
        actual = float(fill.price)
        slippages.append(max(0.0, (actual - reference) / reference) if fill.side == "buy" else max(0.0, (reference - actual) / reference))
    p95 = sorted(slippages)[max(0, int(len(slippages) * 0.95) - 1)]
    average = mean(slippages)
    status = "breach" if p95 >= 0.01 else "warning" if p95 >= 0.005 else "clear"
    return _check(
        "execution.fill_divergence", "execution", "slippage_and_fill_divergence", status,
        "Broker fills diverge materially from persisted reference prices."
        if status != "clear" else "Broker fills remain within the reference-price divergence threshold.",
        value={"matched_fills": len(rows), "average_adverse_slippage": round(average, 8), "p95_adverse_slippage": round(p95, 8)},
        threshold={"warning_p95_slippage": 0.005, "breach_p95_slippage": 0.01},
        details={"window_days": 7, "reference": "paper order limit_price"},
        action_scope="safety",
    )


def _stock_risk_metrics(db: Session, now: datetime) -> list[dict]:
    account = db.query(StockPaperAccount).filter_by(broker="alpaca_paper").one_or_none()
    if not account:
        return [_check("risk.exposure", "risk", "exposure_concentration_turnover_volatility_daily_loss", "unknown", "Stock-paper risk metrics are unavailable because the broker account is not initialized.", action_scope="safety")]
    equity = float(account.equity or 0)
    positions = db.query(StockPaperPosition).filter_by(account_id=account.id).all()
    values = {row.symbol: float(row.market_value or 0) for row in positions if float(row.quantity or 0) > 0 and row.market_value is not None}
    gross = sum(abs(value) for value in values.values()) / equity if equity > 0 else None
    concentration = max((value / equity for value in values.values()), default=None) if equity > 0 else None
    fills = db.query(StockPaperFill).filter(StockPaperFill.account_id == account.id, StockPaperFill.filled_at >= now - timedelta(days=1)).all()
    turnover = sum(float(fill.quantity * fill.price) for fill in fills) / equity if equity > 0 else None
    start_local = now.astimezone(NY).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    snapshots = db.query(StockPaperEquitySnapshot).filter(
        StockPaperEquitySnapshot.account_id == account.id,
        StockPaperEquitySnapshot.observed_at >= start_local,
    ).order_by(StockPaperEquitySnapshot.observed_at.asc()).all()
    start_equity = float(snapshots[0].equity) if snapshots else None
    daily_loss = (start_equity - equity) / start_equity if start_equity and start_equity > 0 else None
    returns = []
    for before, after in zip(snapshots, snapshots[1:]):
        if float(before.equity) > 0:
            returns.append(float(after.equity) / float(before.equity) - 1)
    volatility = pstdev(returns) if len(returns) >= 5 else None
    rule = db.query(RiskRule).filter(RiskRule.is_active.is_(True)).order_by(RiskRule.id).first()
    rules = DEFAULT_RISK_RULES | ((rule.value if rule else {}) or {})
    limits = {
        "max_total_exposure": float(rules.get("max_total_exposure", 0.10)),
        "max_symbol_exposure": float(rules.get("max_symbol_exposure", 0.10)),
        "max_daily_loss": float(rules.get("max_daily_drawdown", 0.03)),
        "warning_turnover": 0.25,
        "breach_turnover": 0.50,
        "warning_volatility": 0.05,
        "breach_volatility": 0.10,
    }
    metrics = [
        ("risk.exposure", "exposure", gross, limits["max_total_exposure"], "Total paper exposure exceeds the configured allocation cap." if gross is not None else "Total paper exposure is unavailable.", "safety"),
        ("risk.concentration", "concentration", concentration, limits["max_symbol_exposure"], "A single symbol exceeds the configured concentration cap." if concentration is not None else "Symbol concentration is unavailable.", "safety"),
        ("risk.turnover", "turnover", turnover, {"warning": limits["warning_turnover"], "breach": limits["breach_turnover"]}, "Paper turnover is unusually high for the monitoring window." if turnover is not None else "Turnover is unavailable.", "safety"),
        ("risk.volatility", "volatility", volatility, {"warning": limits["warning_volatility"], "breach": limits["breach_volatility"]}, "Observed paper equity volatility is unusually high." if volatility is not None else "Volatility is unavailable until enough equity snapshots exist.", "safety"),
        ("risk.daily_loss", "daily_loss", daily_loss, limits["max_daily_loss"], "Paper equity has breached the configured daily loss limit." if daily_loss is not None else "Daily loss is unavailable until a session baseline exists.", "safety"),
    ]
    results = []
    for key, metric, value, threshold, message, scope in metrics:
        if value is None:
            status = "unknown"
        elif isinstance(threshold, dict):
            status = "breach" if value >= threshold["breach"] else "warning" if value >= threshold["warning"] else "clear"
        else:
            status = "breach" if value >= threshold else "warning" if value >= threshold * 0.85 else "clear"
        results.append(_check(key, "risk", metric, status, message if status != "clear" else f"{metric.replace('_', ' ').title()} is within its configured range.", value={metric: round(value, 8) if value is not None else None}, threshold=threshold if isinstance(threshold, dict) else {"breach": threshold, "warning": threshold * 0.85}, details={"equity": equity, "position_count": len(values), "fill_count_24h": len(fills), "equity_snapshot_count_today": len(snapshots)}, action_scope=scope))
    return results


def _broker_reconciliation_health(db: Session, now: datetime) -> list[dict]:
    account = db.query(StockPaperAccount).filter_by(broker="alpaca_paper").one_or_none()
    if not account:
        return [_check("health.reconciliation", "health", "broker_reconciliation", "unknown", "Broker reconciliation health is unavailable because the paper account is not initialized.", action_scope="safety")]
    last = _aware(account.last_reconciled_at)
    stale = not last or now - last > timedelta(minutes=10)
    status = "breach" if account.status != "reconciled" or account.reconciliation_required or stale else "clear"
    return [
        _check("health.broker", "health", "broker_account_health", "clear" if account.status == "reconciled" else "breach", "Alpaca paper account is reconciled." if account.status == "reconciled" else "Alpaca paper account is halted or unavailable.", value={"account_status": account.status}, threshold={"required_status": "reconciled"}, details={"halt_reason": account.halt_reason}, action_scope="safety"),
        _check("health.reconciliation", "health", "broker_reconciliation", status, "Broker reconciliation is stale, required, or halted." if status != "clear" else "Broker reconciliation is recent and verified.", value={"last_reconciled_at": last.isoformat() if last else None, "reconciliation_required": account.reconciliation_required}, threshold={"max_age_minutes": 10, "required": False}, details={"account_status": account.status, "unexplained_residual": account.unexplained_residual}, action_scope="safety"),
    ]


def _worker_scheduler_health(db: Session) -> list[dict]:
    from app.services.readiness import _scheduler_health
    details = _scheduler_health()
    status = "clear" if details["healthy"] and not details.get("recent_unresolved_failures") else "breach"
    return [_check("health.worker_scheduler", "health", "worker_scheduler_health", status, "Workers and exactly one scheduler are evidenced." if status == "clear" else "Worker, scheduler, or recent scheduled-job health is not confirmed.", value={"worker_count": details["worker_count"], "beat_count": details["beat_count"]}, threshold={"worker_count": 1, "beat_count": 1}, details=details, action_scope="safety")]


def _persist_breach(db: Session, check: dict, now: datetime) -> dict:
    row = db.query(StockMonitoringBreach).filter_by(breach_key=check["key"]).one_or_none()
    if row is None:
        row = StockMonitoringBreach(
            breach_key=check["key"], category=check["category"], metric=check["metric"],
            status="unknown", severity="monitor", last_observed_at=now, details={},
        )
        db.add(row)
        db.flush()
    if check["status"] == "breach":
        prior_recent = row.status in {"observed", "persistent"} and _aware(row.last_observed_at) and now - _aware(row.last_observed_at) <= PERSISTENCE_WINDOW
        row.consecutive_count = row.consecutive_count + 1 if prior_recent else 1
        row.status = "persistent" if row.consecutive_count >= 2 else "observed"
        row.severity = "critical" if row.status == "persistent" else "warning"
        row.first_observed_at = row.first_observed_at if prior_recent else now
        row.resolved_at = None
    elif check["status"] == "unknown":
        row.status, row.consecutive_count, row.severity = "unknown", 0, "unknown"
    else:
        row.status, row.consecutive_count, row.severity, row.resolved_at = "cleared", 0, "clear", now
    row.last_observed_at = now
    row.observed_value, row.threshold, row.details, row.updated_at = check["value"], check["threshold"], check["details"], now
    return {"key": check["key"], "status": row.status, "consecutive_count": row.consecutive_count}


def _pause_stock_path(db: Session, reasons: list[str], now: datetime) -> dict:
    reason = "Automatic stock-paper pause after persistent monitoring breach: " + ", ".join(reasons)
    enter_stock_recovery(db, reason=reason, actor="stock_monitor", flatten_policy="none")
    write_audit_log(db, event_type="stock_monitoring_action", action="pause_stock_path", status="complete", message=reason, entity_type="stock_paper", payload={"reasons": reasons, "at": now.isoformat()})
    notification = db.query(Notification).filter(
        Notification.category == "stock_monitoring",
        Notification.source == "stock_monitoring",
        Notification.entity_type == "stock_paper",
        Notification.status != "resolved",
    ).order_by(Notification.created_at.desc()).first()
    payload = {"reasons": reasons}
    if notification:
        notification.severity = "critical"
        notification.title = "Stock paper trading paused"
        notification.message = reason
        notification.payload = payload
        notification.updated_at = now
    else:
        create_notification(
            db, category="stock_monitoring", severity="critical",
            source="stock_monitoring", title="Stock paper trading paused",
            message=reason, entity_type="stock_paper", payload=payload,
        )
    return {"action": "pause_stock_path", "reasons": reasons}


def _demote_active_model(db: Session, reasons: list[str], now: datetime) -> dict | None:
    pointer = db.get(StockPaperBindingState, 1)
    binding = None
    if pointer:
        from app.models import StockPaperModelBinding
        binding = db.get(StockPaperModelBinding, pointer.active_binding_id)
    if not binding:
        return None
    state = db.get(StockModelLifecycleState, binding.model_run_id)
    if not state or state.lifecycle_state not in {"champion", "paper_canary"}:
        return None
    try:
        transition_stock_model_lifecycle(
            db, model_run_id=binding.model_run_id, action="demote", actor="stock_monitor",
            reason="Automatic demotion after persistent monitoring breach: " + ", ".join(reasons),
        )
    except StockTrainingError:
        return None
    write_audit_log(db, event_type="stock_monitoring_action", action="demote_model", status="complete", message="Active paper model demoted automatically.", entity_type="stock_model", payload={"model_run_id": binding.model_run_id, "reasons": reasons, "at": now.isoformat()})
    return {"action": "demote_model", "model_run_id": binding.model_run_id, "reasons": reasons}


def run_stock_monitoring(db: Session, *, source: str = "stock_monitoring_job") -> dict:
    now = _now()
    checks = _distribution_drift(db) + [_performance_drift(db), _freshness_and_provenance(db), _execution_divergence(db, now)]
    checks += _stock_risk_metrics(db, now)
    checks += _broker_reconciliation_health(db, now) + _worker_scheduler_health(db)
    persisted = [_persist_breach(db, check, now) for check in checks]
    persistent_safety = [check["key"] for check, state in zip(checks, persisted) if state["status"] == "persistent" and check["action_scope"] == "safety"]
    persistent_model = [check["key"] for check, state in zip(checks, persisted) if state["status"] == "persistent" and check["action_scope"] == "model"]
    actions: list[dict] = []
    if persistent_safety:
        actions.append(_pause_stock_path(db, persistent_safety, now))
    if persistent_model:
        action = _demote_active_model(db, persistent_model, now)
        if action:
            actions.append(action)
    statuses = [check["status"] for check in checks]
    overall = "breach" if any(status == "breach" for status in statuses) else "warning" if any(status in {"warning", "unknown"} for status in statuses) else "clear"
    snapshot = StockMonitoringSnapshot(
        monitor_key=MONITOR_KEY, status=overall, generated_at=now,
        checks=checks, actions=actions, source=source,
    )
    record_stock_monitor_heartbeat(db, observed_at=now)
    db.add(snapshot)
    write_audit_log(db, event_type="stock_monitoring", action="evaluate", status=overall, message=f"Stock monitoring completed with status {overall}.", entity_type="stock_monitoring", payload={"checks": checks, "actions": actions, "source": source})
    db.commit()
    return {"status": overall, "generated_at": now, "checks": checks, "breaches": persisted, "actions": actions, "snapshot_id": snapshot.id}


def latest_stock_monitoring(db: Session) -> dict:
    row = db.query(StockMonitoringSnapshot).filter_by(monitor_key=MONITOR_KEY).order_by(StockMonitoringSnapshot.generated_at.desc()).first()
    if not row:
        return {
            "status": "unknown",
            "generated_at": None,
            "checks": [],
            "actions": [],
            "snapshot_id": None,
            "breaches": [],
            "message": "No monitoring snapshot has been recorded yet; the scheduled monitor must run before this read is conclusive.",
        }
    return {"status": row.status, "generated_at": row.generated_at, "checks": row.checks, "actions": row.actions, "snapshot_id": row.id, "breaches": [
        {"key": breach.breach_key, "status": breach.status, "consecutive_count": breach.consecutive_count, "severity": breach.severity, "last_observed_at": breach.last_observed_at}
        for breach in db.query(StockMonitoringBreach).order_by(StockMonitoringBreach.updated_at.desc()).all()
    ]}