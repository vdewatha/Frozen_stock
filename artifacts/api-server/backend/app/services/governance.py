from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from fastapi.encoders import jsonable_encoder

from app.models import AuditLog, Strategy, StrategyMemory
from app.services.audit import write_audit_log
from app.services.notifications import create_notification


MIN_PAPER_TRADES_FOR_ACTIVE = 20
MIN_PAPER_TRADES_FOR_RETIRE = 10
MIN_WIN_RATE = 0.45
MIN_PROFIT_FACTOR = 1.10
MAX_MEMORY_DRAWDOWN = -0.10
RETIRE_DRAWDOWN = -0.20
RETIRE_WIN_RATE = 0.35
RETIRE_PROFIT_FACTOR = 0.75
RETIRE_CONFIDENCE_SCORE = 0.25
MIN_CONFIDENCE_SCORE = 0.50


def governance_thresholds() -> dict:
    return {
        "promotion": {
            "min_sample_size": MIN_PAPER_TRADES_FOR_ACTIVE,
            "min_win_rate": 0.50,
            "min_profit_factor": MIN_PROFIT_FACTOR,
            "min_confidence_score": MIN_CONFIDENCE_SCORE,
        },
        "pause": {
            "min_sample_size": 5,
            "min_win_rate": MIN_WIN_RATE,
            "min_profit_factor": MIN_PROFIT_FACTOR,
            "max_drawdown": MAX_MEMORY_DRAWDOWN,
        },
        "retirement": {
            "min_sample_size": MIN_PAPER_TRADES_FOR_RETIRE,
            "retire_drawdown": RETIRE_DRAWDOWN,
            "max_win_rate": RETIRE_WIN_RATE,
            "max_profit_factor": RETIRE_PROFIT_FACTOR,
            "max_confidence_score": RETIRE_CONFIDENCE_SCORE,
        },
    }


def _memory_snapshot(memory: Optional[StrategyMemory]) -> dict:
    if not memory:
        return {
            "sample_size": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "avg_drawdown": 0.0,
            "confidence_score": 0.0,
        }
    return {
        "sample_size": int(memory.sample_size or 0),
        "win_rate": float(memory.win_rate or 0),
        "profit_factor": float(memory.profit_factor or 0),
        "avg_drawdown": float(memory.avg_drawdown or 0),
        "confidence_score": float(memory.confidence_score or 0),
        "symbol": memory.symbol,
        "market_regime": memory.market_regime,
        "notes": memory.notes,
    }


def _latest_memory_for_strategy(db: Session, strategy_id: int) -> Optional[StrategyMemory]:
    return (
        db.query(StrategyMemory)
        .filter(StrategyMemory.strategy_id == strategy_id)
        .order_by(StrategyMemory.last_updated.desc())
        .first()
    )


def _check(label: str, passed: bool, actual: float, threshold: float, comparator: str) -> dict:
    return {
        "label": label,
        "passed": passed,
        "actual": actual,
        "threshold": threshold,
        "comparator": comparator,
    }


def _governance_checks(snapshot: dict) -> dict:
    samples = int(snapshot["sample_size"])
    win_rate = float(snapshot["win_rate"])
    profit_factor = float(snapshot["profit_factor"])
    avg_drawdown = float(snapshot["avg_drawdown"])
    confidence = float(snapshot["confidence_score"])
    retirement = [
        _check("Enough retirement evidence", samples >= MIN_PAPER_TRADES_FOR_RETIRE, samples, MIN_PAPER_TRADES_FOR_RETIRE, ">="),
        _check("Severe drawdown breach", avg_drawdown <= RETIRE_DRAWDOWN, avg_drawdown, RETIRE_DRAWDOWN, "<="),
        _check("Retire weak win rate", win_rate < RETIRE_WIN_RATE, win_rate, RETIRE_WIN_RATE, "<"),
        _check("Retire weak profit factor", profit_factor < RETIRE_PROFIT_FACTOR, profit_factor, RETIRE_PROFIT_FACTOR, "<"),
        _check("Retire weak confidence", confidence < RETIRE_CONFIDENCE_SCORE, confidence, RETIRE_CONFIDENCE_SCORE, "<"),
    ]
    pause = [
        _check("Drawdown pause breach", avg_drawdown <= MAX_MEMORY_DRAWDOWN, avg_drawdown, MAX_MEMORY_DRAWDOWN, "<="),
        _check("Enough pause evidence", samples >= 5, samples, 5, ">="),
        _check("Pause weak win rate", win_rate < MIN_WIN_RATE, win_rate, MIN_WIN_RATE, "<"),
        _check("Pause weak profit factor", profit_factor < MIN_PROFIT_FACTOR, profit_factor, MIN_PROFIT_FACTOR, "<"),
    ]
    promotion = [
        _check("Enough promotion evidence", samples >= MIN_PAPER_TRADES_FOR_ACTIVE, samples, MIN_PAPER_TRADES_FOR_ACTIVE, ">="),
        _check("Promotion win rate", win_rate >= 0.50, win_rate, 0.50, ">="),
        _check("Promotion profit factor", profit_factor >= MIN_PROFIT_FACTOR, profit_factor, MIN_PROFIT_FACTOR, ">="),
        _check("Promotion confidence", confidence >= MIN_CONFIDENCE_SCORE, confidence, MIN_CONFIDENCE_SCORE, ">="),
    ]
    return {"retirement": retirement, "pause": pause, "promotion": promotion}


def evaluate_single_strategy(db: Session, strategy: Strategy) -> dict:
    memory = _latest_memory_for_strategy(db, strategy.id)
    snapshot = _memory_snapshot(memory)
    checks = _governance_checks(snapshot)
    old_status = strategy.current_status
    decision = "hold"
    reason = "Insufficient paper evidence for promotion or demotion."
    new_status = old_status

    if snapshot["sample_size"] > 0:
        if snapshot["sample_size"] >= MIN_PAPER_TRADES_FOR_RETIRE and (
            snapshot["avg_drawdown"] <= RETIRE_DRAWDOWN
            or (
                snapshot["win_rate"] < RETIRE_WIN_RATE
                and snapshot["profit_factor"] < RETIRE_PROFIT_FACTOR
                and snapshot["confidence_score"] < RETIRE_CONFIDENCE_SCORE
            )
        ):
            decision = "retire"
            reason = "Paper evidence is persistently poor; retire this strategy from paper allocation."
            new_status = "retired"
        elif snapshot["avg_drawdown"] <= MAX_MEMORY_DRAWDOWN:
            decision = "pause"
            reason = "Paper-trading drawdown exceeded strategy limit."
            new_status = "paused"
        elif snapshot["sample_size"] >= 5 and snapshot["win_rate"] < MIN_WIN_RATE:
            decision = "pause"
            reason = "Paper win rate is below minimum threshold."
            new_status = "paused"
        elif snapshot["sample_size"] >= 5 and snapshot["profit_factor"] < MIN_PROFIT_FACTOR:
            decision = "pause"
            reason = "Paper profit factor is below minimum threshold."
            new_status = "paused"
        elif (
            snapshot["sample_size"] >= MIN_PAPER_TRADES_FOR_ACTIVE
            and snapshot["win_rate"] >= 0.50
            and snapshot["profit_factor"] >= MIN_PROFIT_FACTOR
            and snapshot["confidence_score"] >= MIN_CONFIDENCE_SCORE
        ):
            decision = "promote"
            reason = "Paper-trading evidence meets active-strategy thresholds."
            new_status = "paper_trading_active"
        else:
            reason = "Paper evidence reviewed; no lifecycle change."

    if new_status != old_status:
        strategy.current_status = new_status

    write_audit_log(
        db,
        event_type="strategy_governance",
        entity_type="strategy",
        entity_id=strategy.id,
        action=decision,
        status="complete",
        message=reason,
        payload={"old_status": old_status, "new_status": new_status, "memory": snapshot},
    )
    return {
        "strategy_id": strategy.id,
        "strategy_name": strategy.name,
        "strategy_type": strategy.strategy_type,
        "old_status": old_status,
        "new_status": new_status,
        "decision": decision,
        "reason": reason,
        "memory": snapshot,
        "checks": checks,
        "thresholds": governance_thresholds(),
    }


def evaluate_strategy_governance(db: Session) -> dict:
    previous_rows = _governance_scorecard_rows(db, limit=1)
    previous_scorecard = (previous_rows[0].payload or {}) if previous_rows else None
    strategies = db.query(Strategy).order_by(Strategy.name).all()
    decisions = [evaluate_single_strategy(db, strategy) for strategy in strategies]
    result = {
        "evaluated": len(decisions),
        "changed": sum(1 for decision in decisions if decision["old_status"] != decision["new_status"]),
        "thresholds": governance_thresholds(),
        "decisions": decisions,
    }
    comparison = _scorecard_comparison(result, previous_scorecard)
    scorecard_log = write_audit_log(
        db,
        event_type="strategy_governance_scorecard",
        entity_type="strategy_governance",
        action="evaluate_strategy_governance",
        status="complete",
        message=f"Evaluated {result['evaluated']} strategy lifecycle decision(s); {result['changed']} changed.",
        payload=jsonable_encoder(result),
    )
    db.flush()
    notifications = _create_governance_notifications(db, comparison=comparison, audit_log_id=scorecard_log.id)
    result["comparison"] = comparison
    result["notifications"] = notifications
    db.commit()
    return result


def _governance_scorecard_rows(db: Session, limit: int = 2) -> list[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(AuditLog.event_type == "strategy_governance_scorecard")
        .filter(AuditLog.action == "evaluate_strategy_governance")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
        .all()
    )


def _threshold_changes(current: dict, previous: dict) -> list[dict]:
    changes: list[dict] = []
    current_thresholds = current.get("thresholds") or {}
    previous_thresholds = previous.get("thresholds") or {}
    for group, values in current_thresholds.items():
        previous_values = previous_thresholds.get(group) or {}
        for key, value in (values or {}).items():
            previous_value = previous_values.get(key)
            if previous_value != value:
                changes.append(
                    {
                        "group": group,
                        "metric": key,
                        "previous": previous_value,
                        "current": value,
                    }
                )
    return changes


def _memory_delta(current: dict, previous: dict, field: str) -> dict:
    current_value = (current.get("memory") or {}).get(field)
    previous_value = (previous.get("memory") or {}).get(field)
    delta = None
    if isinstance(current_value, (int, float)) and isinstance(previous_value, (int, float)):
        delta = round(float(current_value) - float(previous_value), 6)
    return {"previous": previous_value, "current": current_value, "delta": delta}


def _scorecard_comparison(current: dict, previous: Optional[dict]) -> dict:
    if not previous:
        return {
            "status": "pending",
            "message": "No previous governance scorecard is available for comparison.",
            "decision_changes": [],
            "memory_changes": [],
            "threshold_changes": [],
        }

    previous_by_strategy = {int(row.get("strategy_id")): row for row in previous.get("decisions") or []}
    decision_changes: list[dict] = []
    memory_changes: list[dict] = []
    for row in current.get("decisions") or []:
        strategy_id = int(row.get("strategy_id"))
        previous_row = previous_by_strategy.get(strategy_id)
        if not previous_row:
            decision_changes.append(
                {
                    "strategy_id": strategy_id,
                    "strategy_name": row.get("strategy_name"),
                    "change_type": "new_strategy",
                    "previous_decision": None,
                    "current_decision": row.get("decision"),
                    "previous_status": None,
                    "current_status": row.get("new_status"),
                }
            )
            continue

        decision_changed = previous_row.get("decision") != row.get("decision")
        status_changed = previous_row.get("new_status") != row.get("new_status")
        if decision_changed or status_changed:
            decision_changes.append(
                {
                    "strategy_id": strategy_id,
                    "strategy_name": row.get("strategy_name"),
                    "change_type": "decision_or_status",
                    "previous_decision": previous_row.get("decision"),
                    "current_decision": row.get("decision"),
                    "previous_status": previous_row.get("new_status"),
                    "current_status": row.get("new_status"),
                }
            )

        metric_deltas = {
            field: _memory_delta(row, previous_row, field)
            for field in ["sample_size", "win_rate", "profit_factor", "avg_drawdown", "confidence_score"]
        }
        if any(value["delta"] not in {None, 0} for value in metric_deltas.values()):
            memory_changes.append(
                {
                    "strategy_id": strategy_id,
                    "strategy_name": row.get("strategy_name"),
                    "metrics": metric_deltas,
                }
            )

    current_ids = {int(row.get("strategy_id")) for row in current.get("decisions") or []}
    for strategy_id, previous_row in previous_by_strategy.items():
        if strategy_id not in current_ids:
            decision_changes.append(
                {
                    "strategy_id": strategy_id,
                    "strategy_name": previous_row.get("strategy_name"),
                    "change_type": "removed_strategy",
                    "previous_decision": previous_row.get("decision"),
                    "current_decision": None,
                    "previous_status": previous_row.get("new_status"),
                    "current_status": None,
                }
            )

    threshold_changes = _threshold_changes(current, previous)
    return {
        "status": "ready",
        "message": (
            f"{len(decision_changes)} lifecycle change(s), "
            f"{len(memory_changes)} memory movement(s), "
            f"{len(threshold_changes)} threshold change(s)."
        ),
        "decision_changes": decision_changes,
        "memory_changes": memory_changes,
        "threshold_changes": threshold_changes,
    }


def _is_memory_deterioration(change: dict) -> bool:
    metrics = change.get("metrics") or {}
    win_delta = (metrics.get("win_rate") or {}).get("delta")
    profit_delta = (metrics.get("profit_factor") or {}).get("delta")
    confidence_delta = (metrics.get("confidence_score") or {}).get("delta")
    drawdown_delta = (metrics.get("avg_drawdown") or {}).get("delta")
    return any(
        isinstance(delta, (int, float)) and delta < 0
        for delta in [win_delta, profit_delta, confidence_delta, drawdown_delta]
    )


def _create_governance_notifications(db: Session, *, comparison: dict, audit_log_id: Optional[int]) -> dict:
    created = []
    if comparison.get("status") != "ready":
        return {"created": 0, "notifications": created}

    for change in comparison.get("decision_changes") or []:
        current_status = change.get("current_status")
        current_decision = change.get("current_decision")
        if current_status not in {"paused", "retired"} and current_decision not in {"pause", "retire"}:
            continue
        severity = "critical" if current_status == "retired" or current_decision == "retire" else "warning"
        notification = create_notification(
            db,
            category="strategy_governance",
            severity=severity,
            source="strategy_governance_lifecycle",
            title=f"Strategy governance {current_decision or current_status}",
            message=(
                f"{change.get('strategy_name')}: "
                f"{change.get('previous_decision') or 'none'} / {change.get('previous_status') or 'none'} "
                f"-> {current_decision or 'none'} / {current_status or 'none'}."
            ),
            entity_type="strategy",
            entity_id=change.get("strategy_id"),
            payload={"audit_log_id": audit_log_id, "change": change},
        )
        db.flush()
        created.append({"id": notification.id, "severity": severity, "source": notification.source, "strategy_id": change.get("strategy_id")})

    for change in comparison.get("memory_changes") or []:
        if not _is_memory_deterioration(change):
            continue
        notification = create_notification(
            db,
            category="strategy_governance",
            severity="warning",
            source="strategy_governance_memory",
            title="Strategy memory deteriorated",
            message=f"{change.get('strategy_name')}: paper evidence deteriorated since the previous governance scorecard.",
            entity_type="strategy",
            entity_id=change.get("strategy_id"),
            payload={"audit_log_id": audit_log_id, "change": change},
        )
        db.flush()
        created.append({"id": notification.id, "severity": "warning", "source": notification.source, "strategy_id": change.get("strategy_id")})

    if comparison.get("threshold_changes"):
        notification = create_notification(
            db,
            category="strategy_governance",
            severity="info",
            source="strategy_governance_thresholds",
            title="Strategy governance thresholds changed",
            message=f"{len(comparison.get('threshold_changes') or [])} governance threshold value(s) changed since the previous scorecard.",
            entity_type="strategy_governance",
            entity_id=audit_log_id,
            payload={"audit_log_id": audit_log_id, "changes": comparison.get("threshold_changes") or []},
        )
        db.flush()
        created.append({"id": notification.id, "severity": "info", "source": notification.source, "strategy_id": None})

    return {"created": len(created), "notifications": created}


def latest_strategy_governance_scorecard(db: Session) -> dict:
    rows = _governance_scorecard_rows(db, limit=2)
    row = rows[0] if rows else None
    previous_row = rows[1] if len(rows) > 1 else None
    if not row:
        return {
            "status": "empty",
            "message": "No persisted governance scorecard is available yet.",
            "scorecard": None,
            "audit_log_id": None,
            "created_at": None,
            "previous_audit_log_id": None,
            "previous_created_at": None,
            "comparison": {
                "status": "pending",
                "message": "No governance scorecard is available for comparison.",
                "decision_changes": [],
                "memory_changes": [],
                "threshold_changes": [],
            },
        }
    scorecard = row.payload or {}
    previous_scorecard = (previous_row.payload or {}) if previous_row else None
    return {
        "status": "ready",
        "message": row.message,
        "scorecard": scorecard,
        "audit_log_id": row.id,
        "created_at": row.created_at,
        "previous_audit_log_id": previous_row.id if previous_row else None,
        "previous_created_at": previous_row.created_at if previous_row else None,
        "comparison": _scorecard_comparison(scorecard, previous_scorecard),
    }
