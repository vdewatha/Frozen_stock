from __future__ import annotations

from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from app.models import RiskRule, Strategy
from app.services.audit import write_audit_log
from app.services.candidate_evidence import candidate_evidence_drilldown
from app.services.decision_journal import record_candidate_decision
from app.services.notifications import create_notification
from app.services.readiness import readiness_snapshot
from app.services.risk import DEFAULT_RISK_RULES
from app.services.trade_candidates import get_trade_candidate_snapshot


def _active_rules(db: Session) -> dict:
    rule = db.query(RiskRule).filter(RiskRule.is_active.is_(True)).order_by(RiskRule.id).first()
    return DEFAULT_RISK_RULES | (((rule.value if rule else {}) or {}).copy())


def _find_candidate(snapshot: dict, symbol: str, strategy_type: str) -> dict:
    symbol = symbol.upper()
    for candidate in snapshot.get("candidates", []):
        if candidate["symbol"] == symbol and candidate["strategy"] == strategy_type:
            return candidate
    return {}


def _bounded(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _review_context(candidate: dict, rules: dict, decision: str) -> dict:
    base_key = "activation_score_threshold" if decision == "activate" else "candidate_review_score_threshold"
    base_threshold = float(rules.get(base_key, 0.72 if decision == "activate" else 0.70) or 0)
    cap = max(float(rules.get("journal_feedback_review_threshold_cap", 0.03) or 0), 0)
    requested_adjustment = float(candidate.get("review_threshold_adjustment") or 0)
    applied_adjustment = _bounded(requested_adjustment, -cap, cap)
    adjusted_threshold = max(0.0, base_threshold + applied_adjustment)
    score = float(candidate.get("score") or 0)
    feedback = candidate.get("journal_feedback") or {}
    return {
        "base_threshold": round(base_threshold, 4),
        "applied_threshold_adjustment": round(applied_adjustment, 4),
        "adjusted_threshold": round(adjusted_threshold, 4),
        "candidate_score": round(score, 4),
        "eligible_by_score": score >= adjusted_threshold,
        "memory_status": feedback.get("status", "not_enough_feedback"),
        "memory_sample_size": int(feedback.get("sample_size") or 0),
        "memory_notes": feedback.get("notes", ""),
        "risk_profile": {
            "threshold_key": base_key,
            "journal_feedback_review_threshold_cap": cap,
        },
    }


def review_candidate_activation(db: Session, symbol: str, strategy_type: str, decision: str, reason: str) -> dict:
    decision = decision.lower()
    if decision not in {"activate", "candidate", "reject"}:
        raise ValueError("Decision must be activate, candidate, or reject.")

    strategy = db.query(Strategy).filter(Strategy.strategy_type == strategy_type).one_or_none()
    if not strategy:
        raise ValueError(f"Unknown strategy: {strategy_type}")
    return {
        "symbol": symbol.upper(),
        "strategy": strategy.strategy_type,
        "strategy_name": strategy.name,
        "decision": decision,
        "status": "quarantined",
        "message": "Legacy candidate activation cannot mutate stock-paper strategy state.",
        "old_status": strategy.current_status,
        "new_status": strategy.current_status,
        "eligible": False,
        "blockers": ["Legacy candidate activation is quarantined from stock-paper execution."],
        "candidate": {},
        "review_context": {"status": "quarantined"},
        "journal_entry_id": None,
    }

    snapshot = get_trade_candidate_snapshot(db, limit=50, refresh=False)
    candidate = _find_candidate(snapshot, symbol, strategy_type)
    if not candidate:
        raise ValueError("No cached scanner candidate matches that symbol and strategy.")

    blockers = list(candidate.get("blockers") or [])
    rules = _active_rules(db)
    kill_switch = bool(rules.get("kill_switch_enabled", False))
    review_context = _review_context(candidate, rules, decision)
    if kill_switch:
        blockers.append("Global kill switch is enabled.")
    if candidate.get("candidate_status") != "positive_candidate":
        blockers.append("Scanner row is not currently a positive candidate.")
    if candidate.get("action") != "BUY":
        blockers.append("Scanner row is not a BUY candidate.")
    if decision in {"activate", "candidate"} and not review_context["eligible_by_score"]:
        blockers.append(
            f"Candidate score {review_context['candidate_score']:.4f} is below adjusted {decision} threshold "
            f"{review_context['adjusted_threshold']:.4f}."
        )

    readiness = readiness_snapshot(db) if decision == "activate" else None
    if readiness:
        readiness_blockers = [check["name"] for check in readiness["checks"] if check["status"] == "blocked"]
        blockers.extend([f"Readiness blocked: {name}" for name in readiness_blockers])

    old_status = strategy.current_status
    new_status = old_status
    status = "complete"
    message = reason

    if decision == "activate":
        if blockers:
            status = "blocked"
            message = "Activation blocked by candidate review gates."
        else:
            new_status = "paper_trading_active"
            strategy.current_status = new_status
    elif decision == "candidate":
        if kill_switch:
            status = "blocked"
            message = "Candidate review blocked by global kill switch."
        else:
            new_status = "paper_trading_candidate"
            strategy.current_status = new_status
    else:
        new_status = "research"
        strategy.current_status = new_status

    payload = {
        "symbol": symbol.upper(),
        "strategy": strategy_type,
        "old_status": old_status,
        "new_status": new_status,
        "decision": decision,
        "blockers": blockers,
        "candidate": candidate,
        "review_context": review_context,
        "readiness": readiness,
        "reason": reason,
    }
    payload = jsonable_encoder(payload)
    evidence_snapshot = None
    try:
        evidence_snapshot = candidate_evidence_drilldown(db, symbol, strategy_type)
    except ValueError:
        evidence_snapshot = {"candidate": candidate, "readiness": readiness, "error": "Evidence drilldown unavailable during activation review."}
    write_audit_log(
        db,
        event_type="candidate_activation",
        entity_type="strategy",
        entity_id=strategy.id,
        action=decision,
        status=status,
        message=message,
        payload=payload,
    )
    journal = record_candidate_decision(
        db,
        symbol=symbol,
        strategy_type=strategy_type,
        decision=decision,
        status=status,
        reason=message,
        evidence_snapshot=evidence_snapshot,
    )
    create_notification(
        db,
        category="candidate_activation",
        severity="warning" if status == "blocked" else "info",
        source="candidate_activation",
        title=f"Candidate activation {status}",
        message=f"{strategy.name} {symbol.upper()}: {message}",
        entity_type="strategy",
        entity_id=strategy.id,
        payload=payload,
    )
    db.commit()
    db.refresh(strategy)
    return {
        "symbol": symbol.upper(),
        "strategy": strategy.strategy_type,
        "strategy_name": strategy.name,
        "decision": decision,
        "status": status,
        "message": message,
        "old_status": old_status,
        "new_status": strategy.current_status,
        "eligible": not blockers,
        "blockers": blockers,
        "candidate": candidate,
        "review_context": review_context,
        "journal_entry_id": journal["id"],
    }
