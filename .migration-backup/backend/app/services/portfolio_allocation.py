from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from app.models import CandidateDecisionJournal, Notification, PaperTrade, Strategy
from app.services.audit import write_audit_log
from app.services.memory_replay import memory_replay_evaluation
from app.services.paper_trading import reduce_paper_trade, run_paper_signal
from app.services.portfolio_risk import portfolio_risk_snapshot
from app.services.readiness import readiness_snapshot
from app.services.trade_candidates import get_trade_candidate_snapshot

MIN_ACTION_EXPOSURE = 0.005
TARGET_TOLERANCE = 0.003
MIN_TRIM_PCT = 0.05


def _bounded(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _candidate_weight(candidate: dict) -> float:
    score = max(float(candidate.get("score") or 0), 0)
    probability_edge = max(float(candidate.get("probability_up") or 0) - 0.5, 0)
    expected_return = max(float(candidate.get("expected_return") or 0), 0)
    backtest_score = max(float(candidate.get("backtest_score") or 0), 0)
    return max(score + probability_edge * 0.5 + expected_return * 8 + backtest_score * 0.2, 0.0001)


def _gate_lookup(groups: list[dict]) -> dict:
    return {str(group.get("key")): group for group in groups}


def _candidate_replay_gate(candidate: dict, replay: dict) -> dict:
    groups = replay.get("groups") or {}
    symbol = str(candidate.get("symbol"))
    strategy = str(candidate.get("strategy"))
    regime = str(candidate.get("market_regime") or "unknown")
    candidates = [
        ("symbol_strategy", f"{symbol}|{strategy}", _gate_lookup(groups.get("by_symbol_strategy") or [])),
        ("strategy", strategy, _gate_lookup(groups.get("by_strategy") or [])),
        ("symbol", symbol, _gate_lookup(groups.get("by_symbol") or [])),
        ("regime", regime, _gate_lookup(groups.get("by_regime") or [])),
    ]
    for scope, key, lookup in candidates:
        group = lookup.get(key)
        if group:
            gate = dict(group.get("replay_gate") or {})
            gate["scope"] = scope
            gate["scope_key"] = key
            gate["scope_label"] = group.get("label") or key
            return gate
    gate = dict(replay.get("replay_gate") or {})
    gate["scope"] = "global"
    gate["scope_key"] = "global"
    gate["scope_label"] = "Global replay"
    return gate


def _memory_allocation_context(candidate: dict, risk_limits: dict, replay_gate: dict) -> dict:
    cap = max(float(risk_limits.get("journal_feedback_allocation_multiplier_cap", 0.20) or 0), 0)
    score_adjustment = float(candidate.get("memory_score_adjustment") or 0)
    review_adjustment = float(candidate.get("review_threshold_adjustment") or 0)
    raw_multiplier = 1 + _bounded(score_adjustment * 2.5 - review_adjustment * 2.0, -cap, cap)
    requested_multiplier = _bounded(raw_multiplier, max(0.0, 1 - cap), 1 + cap)
    multiplier = requested_multiplier
    gate_limited = False
    if requested_multiplier > 1 and not bool(replay_gate.get("allows_memory_increase")):
        multiplier = 1.0
        gate_limited = True
    feedback = candidate.get("journal_feedback") or {}
    return {
        "multiplier": round(multiplier, 4),
        "requested_multiplier": round(requested_multiplier, 4),
        "gate_limited": gate_limited,
        "cap": cap,
        "score_adjustment": round(score_adjustment, 4),
        "review_threshold_adjustment": round(review_adjustment, 4),
        "replay_gate": replay_gate,
        "replay_gate_scope": replay_gate.get("scope", "global"),
        "replay_gate_scope_label": replay_gate.get("scope_label", "Global replay"),
        "status": feedback.get("status", "not_enough_feedback"),
        "sample_size": int(feedback.get("sample_size") or 0),
        "notes": feedback.get("notes", ""),
    }


def _recommendation(
    status: str,
    current_exposure: float,
    target_exposure: float,
    symbol_room: float,
    is_open: bool,
    memory_context: dict,
) -> tuple[str, str]:
    gap = target_exposure - current_exposure
    memory_note = ""
    if memory_context.get("sample_size"):
        multiplier = float(memory_context.get("multiplier") or 1)
        requested_multiplier = float(memory_context.get("requested_multiplier") or multiplier)
        if memory_context.get("gate_limited"):
            scope_label = memory_context.get("replay_gate_scope_label", "replay")
            memory_note = f" Journal feedback requested higher sizing, but {scope_label} replay gate is closed."
        elif multiplier > 1:
            memory_note = f" Journal feedback raises target sizing by {round((multiplier - 1) * 100, 1)}%."
        elif multiplier < 1:
            memory_note = f" Journal feedback reduces target sizing by {round((1 - multiplier) * 100, 1)}%."
        elif requested_multiplier > 1:
            memory_note = " Journal feedback sizing increase is held flat by replay policy."
    if status != "paper_trading_active":
        if symbol_room >= MIN_ACTION_EXPOSURE:
            return "activate_candidate", f"Positive scanner evidence has room under the symbol cap; review for paper activation.{memory_note}"
        return "wait_for_room", f"Positive scanner evidence exists, but the symbol cap is full.{memory_note}"
    if is_open and current_exposure > target_exposure + TARGET_TOLERANCE:
        return "trim", f"Open exposure is above the score-weighted target allocation.{memory_note}"
    if gap >= MIN_ACTION_EXPOSURE and symbol_room >= MIN_ACTION_EXPOSURE:
        return "add", f"Active positive candidate has room below its score-weighted target allocation.{memory_note}"
    if gap >= MIN_ACTION_EXPOSURE:
        return "wait_for_room", f"Active positive candidate ranks well, but the symbol cap is full.{memory_note}"
    if is_open:
        return "hold", f"Open exposure is close to its score-weighted target allocation.{memory_note}"
    return "watch", f"Positive candidate is active, but the target gap is too small for a new paper entry.{memory_note}"


def allocation_plan(db: Session, limit: int = 20) -> dict:
    risk = portfolio_risk_snapshot(db)
    replay = memory_replay_evaluation(db, limit=12, top_k=3)
    scanner = get_trade_candidate_snapshot(db, limit=max(limit, 20), refresh=False)
    strategies = {strategy.id: strategy for strategy in db.query(Strategy).all()}
    strategies_by_type = {strategy.strategy_type: strategy for strategy in strategies.values()}
    positions_by_id = {position["paper_trade_id"]: position for position in risk["positions"]}

    open_lookup: dict[tuple[str, str], dict] = {}
    for trade in db.query(PaperTrade).filter(PaperTrade.status == "open").all():
        strategy = strategies.get(trade.strategy_id or 0)
        if not strategy:
            continue
        position = positions_by_id.get(trade.id, {})
        open_lookup[(trade.symbol, strategy.strategy_type)] = {
            "paper_trade_id": trade.id,
            "quantity": float(trade.quantity or 0),
            "exposure_pct": float(position.get("exposure_pct") or 0),
            "unrealized_pl": float(position.get("unrealized_pl") or 0),
        }

    symbol_exposure = {row["key"]: float(row["exposure_pct"]) for row in risk["symbol_exposure"]}
    symbol_limit = float(risk["risk_limits"].get("max_symbol_exposure", 0))
    positives = [
        candidate
        for candidate in scanner.get("candidates", [])
        if candidate.get("candidate_status") == "positive_candidate" and candidate.get("action") == "BUY"
    ][: max(1, min(limit, 50))]
    weights_by_symbol: dict[str, float] = {}
    base_weights_by_symbol: dict[str, float] = {}
    candidate_weights: dict[tuple[str, str], dict] = {}
    for candidate in positives:
        symbol = candidate["symbol"]
        strategy_type = candidate["strategy"]
        base_weight = _candidate_weight(candidate)
        replay_gate = _candidate_replay_gate(candidate, replay)
        memory_context = _memory_allocation_context(candidate, risk["risk_limits"], replay_gate)
        adjusted_weight = base_weight * float(memory_context["multiplier"])
        candidate_weights[(symbol, strategy_type)] = {
            "base_weight": base_weight,
            "adjusted_weight": adjusted_weight,
            "memory_context": memory_context,
        }
        base_weights_by_symbol[symbol] = base_weights_by_symbol.get(symbol, 0.0) + base_weight
        weights_by_symbol[symbol] = weights_by_symbol.get(symbol, 0.0) + adjusted_weight

    rows: list[dict] = []
    for candidate in positives:
        symbol = candidate["symbol"]
        strategy_type = candidate["strategy"]
        strategy = strategies_by_type.get(strategy_type)
        open_position = open_lookup.get((symbol, strategy_type))
        current_exposure = float((open_position or {}).get("exposure_pct") or 0)
        symbol_current_exposure = symbol_exposure.get(symbol, 0.0)
        symbol_room = max(symbol_limit - symbol_current_exposure, 0.0)
        weight_context = candidate_weights[(symbol, strategy_type)]
        weight = float(weight_context["adjusted_weight"])
        base_weight = float(weight_context["base_weight"])
        memory_context = weight_context["memory_context"]
        raw_target_exposure = symbol_limit * (base_weight / max(base_weights_by_symbol.get(symbol, base_weight), 0.0001))
        target_exposure = symbol_limit * (weight / max(weights_by_symbol.get(symbol, weight), 0.0001))
        recommendation, reason = _recommendation(
            candidate.get("strategy_status") or (strategy.current_status if strategy else "unknown"),
            current_exposure,
            target_exposure,
            symbol_room,
            open_position is not None,
            memory_context,
        )
        rows.append(
            {
                "symbol": symbol,
                "strategy": strategy_type,
                "strategy_name": candidate.get("strategy_name") or (strategy.name if strategy else strategy_type),
                "strategy_status": candidate.get("strategy_status") or (strategy.current_status if strategy else "unknown"),
                "candidate_status": candidate.get("candidate_status"),
                "market_regime": candidate.get("market_regime", "unknown"),
                "paper_trade_id": (open_position or {}).get("paper_trade_id"),
                "rank_score": float(candidate.get("score") or 0),
                "allocation_score": round(weight, 6),
                "probability_up": float(candidate.get("probability_up") or 0),
                "expected_return": float(candidate.get("expected_return") or 0),
                "current_exposure_pct": round(current_exposure, 6),
                "base_target_exposure_pct": round(raw_target_exposure, 6),
                "target_exposure_pct": round(target_exposure, 6),
                "memory_allocation_multiplier": memory_context["multiplier"],
                "memory_allocation_context": memory_context,
                "symbol_exposure_pct": round(symbol_current_exposure, 6),
                "symbol_limit_pct": symbol_limit,
                "symbol_room_pct": round(symbol_room, 6),
                "recommendation": recommendation,
                "reason": reason,
            }
        )

    rows = sorted(
        rows,
        key=lambda row: (
            {"add": 0, "activate_candidate": 1, "trim": 2, "hold": 3, "wait_for_room": 4, "watch": 5}.get(row["recommendation"], 9),
            -row["allocation_score"],
        ),
    )
    return {
        "generated_at": datetime.utcnow(),
        "status": "ready" if positives else "empty",
        "message": (
            f"Built allocation plan from {len(positives)} positive scanner candidates."
            if positives
            else "No positive BUY candidates are available for allocation."
        ),
        "paper_equity": risk["paper_equity"],
        "risk_limits": risk["risk_limits"],
        "open_positions": risk["open_positions"],
        "gross_exposure": risk["gross_exposure"],
        "alerts": risk["alerts"],
        "memory_replay_gate": replay_gate,
        "positive_candidates": len(positives),
        "recommendations": rows,
    }


def _gate_matches_recommendation(gate_payload: dict, recommendation: dict) -> bool:
    scope = str(gate_payload.get("scope") or "")
    key = str(gate_payload.get("scope_key") or "")
    if scope == "global":
        return True
    if scope == "symbol_strategy":
        return key == f"{recommendation.get('symbol')}|{recommendation.get('strategy')}"
    if scope == "strategy":
        return key == str(recommendation.get("strategy"))
    if scope == "symbol":
        return key == str(recommendation.get("symbol"))
    if scope == "regime":
        return key == str(recommendation.get("market_regime") or "unknown")
    return False


def _gate_specificity(scope: str) -> int:
    return {
        "symbol_strategy": 5,
        "strategy": 4,
        "symbol": 3,
        "regime": 2,
        "global": 1,
    }.get(scope, 0)


def _queue_priority(recommendation: dict) -> int:
    action = recommendation.get("recommendation")
    if action == "add":
        return 0
    if action == "activate_candidate":
        return 1
    if action == "trim":
        return 2
    if action == "hold":
        return 3
    return 4


def allocation_review_queue(db: Session, limit: int = 20) -> dict:
    plan = allocation_plan(db, limit=max(limit, 20))
    gate_alerts = (
        db.query(Notification)
        .filter(Notification.category == "memory_replay")
        .filter(Notification.source == "memory_replay_gate_open")
        .filter(Notification.entity_type == "memory_replay_gate")
        .filter(Notification.status.in_(["open", "acknowledged"]))
        .order_by(Notification.created_at.desc())
        .limit(50)
        .all()
    )
    items: list[dict] = []
    seen: set[tuple[int, str, str]] = set()
    reviewed_journal_set = {
        (row.symbol, row.strategy_type)
        for row in (
            db.query(CandidateDecisionJournal)
            .filter(CandidateDecisionJournal.status == "allocation_reviewed")
            .filter(CandidateDecisionJournal.decision.in_(["approve", "skip"]))
            .order_by(CandidateDecisionJournal.created_at.desc())
            .limit(250)
            .all()
        )
    }
    globally_reviewed_set = reviewed_journal_set | {
        (str(review.get("symbol")), str(review.get("strategy")))
        for alert in gate_alerts
        for review in ((alert.payload or {}).get("allocation_reviews") or [])
    }
    for alert in gate_alerts:
        payload = alert.payload or {}
        gate = payload.get("replay_gate") or {}
        if not bool(gate.get("allows_memory_increase")):
            continue
        reviewed_set = {
            (str(review.get("symbol")), str(review.get("strategy")))
            for review in (payload.get("allocation_reviews") or [])
        }
        for row in plan["recommendations"]:
            if not _gate_matches_recommendation(payload, row):
                continue
            setup_key = (str(row.get("symbol")), str(row.get("strategy")))
            if setup_key in reviewed_set or setup_key in globally_reviewed_set:
                continue
            memory_context = row.get("memory_allocation_context") or {}
            queue_key = (0, str(row.get("symbol")), str(row.get("strategy")))
            existing_index = next((index for index, item in enumerate(items) if (0, item["symbol"], item["strategy"]) == queue_key), None)
            if existing_index is not None and _gate_specificity(str(items[existing_index].get("gate_scope") or "")) >= _gate_specificity(str(payload.get("scope") or "")):
                continue
            if existing_index is not None:
                items.pop(existing_index)
            seen.add(queue_key)
            requested = float(memory_context.get("requested_multiplier") or row.get("memory_allocation_multiplier") or 1)
            effective = float(row.get("memory_allocation_multiplier") or 1)
            paper_action = row.get("recommendation")
            items.append(
                {
                    "notification_id": alert.id,
                    "gate_scope": payload.get("scope"),
                    "gate_key": payload.get("scope_key"),
                    "gate_label": payload.get("scope_label"),
                    "gate_status": gate.get("status"),
                    "gate_complete_samples": gate.get("complete_samples"),
                    "gate_min_complete_samples": gate.get("min_complete_samples"),
                    "symbol": row.get("symbol"),
                    "strategy": row.get("strategy"),
                    "strategy_name": row.get("strategy_name"),
                    "market_regime": row.get("market_regime"),
                    "recommendation": paper_action,
                    "review_status": "ready_for_dry_run" if paper_action in {"add", "trim"} else "needs_human_review",
                    "paper_action": paper_action if paper_action in {"add", "trim"} else None,
                    "dry_run_available": paper_action in {"add", "trim"},
                    "rank_score": row.get("rank_score"),
                    "probability_up": row.get("probability_up"),
                    "expected_return": row.get("expected_return"),
                    "current_exposure_pct": row.get("current_exposure_pct"),
                    "target_exposure_pct": row.get("target_exposure_pct"),
                    "base_target_exposure_pct": row.get("base_target_exposure_pct"),
                    "memory_allocation_multiplier": effective,
                    "requested_memory_multiplier": requested,
                    "memory_status": memory_context.get("status"),
                    "reason": row.get("reason"),
                    "notification_created_at": alert.created_at,
                    "paper_only": True,
                }
            )

    items = sorted(
        items,
        key=lambda item: (
            _queue_priority(item),
            -float(item.get("memory_allocation_multiplier") or 0),
            -float(item.get("rank_score") or 0),
            str(item.get("gate_label") or ""),
            str(item.get("symbol") or ""),
        ),
    )[: max(1, min(limit, 50))]
    return jsonable_encoder(
        {
            "generated_at": datetime.utcnow(),
            "status": "ready" if items else "empty",
            "message": (
                f"{len(items)} replay-approved allocation review item(s) are ready."
                if items
                else "No replay-approved allocation review items are currently open."
            ),
            "open_gate_alerts": len(gate_alerts),
            "actionable_items": sum(1 for item in items if item["dry_run_available"]),
            "allocation_plan_generated_at": plan["generated_at"],
            "items": items,
        }
    )


def review_allocation_queue_item(
    db: Session,
    *,
    notification_id: int,
    symbol: str,
    strategy: str,
    decision: str,
    reason: str,
) -> dict:
    if decision not in {"approve", "skip"}:
        raise ValueError("Allocation queue decision must be approve or skip.")
    symbol = symbol.strip().upper()
    queue = allocation_review_queue(db, limit=50)
    item = next(
        (
            row
            for row in queue["items"]
            if int(row.get("notification_id") or 0) == notification_id
            and str(row.get("symbol")) == symbol
            and str(row.get("strategy")) == strategy
        ),
        None,
    )
    if not item:
        raise ValueError("Allocation review queue item is no longer open.")

    from app.services.candidate_evidence import candidate_evidence_drilldown
    from app.services.decision_journal import record_candidate_decision

    evidence = candidate_evidence_drilldown(db, symbol, strategy)
    cached_candidate = dict((evidence.get("cached_candidate") or evidence.get("candidate") or {}))
    cached_candidate.update(
        {
            "symbol": symbol,
            "strategy": strategy,
            "strategy_name": item.get("strategy_name"),
            "market_regime": item.get("market_regime"),
            "score": item.get("rank_score"),
            "base_score": cached_candidate.get("base_score", item.get("rank_score")),
            "probability_up": item.get("probability_up"),
            "expected_return": item.get("expected_return"),
            "memory_score_adjustment": cached_candidate.get("memory_score_adjustment", 0),
            "review_threshold_adjustment": cached_candidate.get("review_threshold_adjustment", 0),
        }
    )
    evidence_snapshot = {
        **evidence,
        "cached_candidate": cached_candidate,
        "allocation_review": {
            "decision": decision,
            "reason": reason,
            "reviewed_at": datetime.utcnow(),
            "queue_item": item,
            "paper_only": True,
        },
    }
    journal = record_candidate_decision(
        db,
        symbol=symbol,
        strategy_type=strategy,
        decision=decision,
        status="allocation_reviewed",
        reason=reason,
        evidence_snapshot=evidence_snapshot,
    )
    notification = db.query(Notification).filter(Notification.id == notification_id).one_or_none()
    if notification:
        payload = notification.payload or {}
        reviews = list(payload.get("allocation_reviews") or [])
        reviews.append(
            {
                "journal_entry_id": journal["id"],
                "symbol": symbol,
                "strategy": strategy,
                "decision": decision,
                "reason": reason,
                "reviewed_at": datetime.utcnow().isoformat(),
            }
        )
        notification.payload = {**payload, "allocation_reviews": reviews[-10:]}
        notification.updated_at = datetime.utcnow()
    db.commit()
    return jsonable_encoder(
        {
            "status": "recorded",
            "message": f"Recorded {decision} review for {symbol} {strategy}.",
            "decision": decision,
            "queue_item": item,
            "journal_entry": journal,
            "paper_only": True,
        }
    )


def _allocation_executor_action(
    db: Session,
    row: dict,
    *,
    dry_run: bool,
    add_allowed: bool,
    blocked_checks: list[str],
) -> tuple[Optional[dict], Optional[dict]]:
    recommendation = row["recommendation"]
    if recommendation == "trim":
        current = float(row["current_exposure_pct"] or 0)
        target = float(row["target_exposure_pct"] or 0)
        paper_trade_id = row.get("paper_trade_id")
        reduce_pct = (current - target) / current if current else 0
        if not paper_trade_id or reduce_pct < MIN_TRIM_PCT:
            return None, {**row, "skip_reason": "Trim amount is below the executor minimum."}
        action = {
            "action": "trim",
            "symbol": row["symbol"],
            "strategy": row["strategy"],
            "paper_trade_id": paper_trade_id,
            "reduce_pct": round(min(max(reduce_pct, 0), 1), 6),
            "dry_run": dry_run,
            "reason": row["reason"],
        }
        if not dry_run:
            action["result"] = reduce_paper_trade(
                db,
                int(paper_trade_id),
                float(action["reduce_pct"]),
                "Allocation executor trimmed paper exposure toward score-weighted target.",
            )
        return action, None

    if recommendation == "add":
        if not add_allowed:
            return None, {**row, "skip_reason": f"Readiness blocked: {', '.join(blocked_checks)}"}
        action = {
            "action": "add",
            "symbol": row["symbol"],
            "strategy": row["strategy"],
            "dry_run": dry_run,
            "reason": row["reason"],
        }
        if not dry_run:
            action["result"] = run_paper_signal(db, row["symbol"], row["strategy"])
        return action, None

    if recommendation == "activate_candidate":
        return None, {**row, "skip_reason": "Candidate activation still requires explicit review."}

    return None, {**row, "skip_reason": f"No executor action for recommendation: {recommendation}."}


def dry_run_approved_allocation_review(
    db: Session,
    *,
    symbol: str,
    strategy: str,
    journal_entry_id: Optional[int] = None,
    limit: int = 20,
) -> dict:
    symbol = symbol.strip().upper()
    query = (
        db.query(CandidateDecisionJournal)
        .filter(CandidateDecisionJournal.symbol == symbol)
        .filter(CandidateDecisionJournal.strategy_type == strategy)
        .filter(CandidateDecisionJournal.status == "allocation_reviewed")
        .filter(CandidateDecisionJournal.decision == "approve")
    )
    if journal_entry_id is not None:
        query = query.filter(CandidateDecisionJournal.id == journal_entry_id)
    journal = query.order_by(CandidateDecisionJournal.created_at.desc()).first()
    if not journal:
        raise ValueError("No approved allocation review was found for this setup.")

    plan = allocation_plan(db, limit=max(limit, 20))
    row = next(
        (
            recommendation
            for recommendation in plan["recommendations"]
            if str(recommendation.get("symbol")) == symbol and str(recommendation.get("strategy")) == strategy
        ),
        None,
    )
    if not row:
        raise ValueError("Approved setup is not in the current allocation plan.")

    review = (journal.evidence_snapshot or {}).get("allocation_review") or {}
    queue_item = review.get("queue_item") or {}
    if queue_item.get("paper_action") not in {"add", "trim"} and row.get("recommendation") not in {"add", "trim"}:
        raise ValueError("Approved setup does not have an executor-supported add or trim action.")

    readiness = readiness_snapshot(db)
    blocked_checks = [check["name"] for check in readiness["checks"] if check["status"] == "blocked"]
    action, skipped = _allocation_executor_action(
        db,
        row,
        dry_run=True,
        add_allowed=not blocked_checks,
        blocked_checks=blocked_checks,
    )
    status = "dry_run" if action else "skipped"
    message = (
        f"Prepared approved allocation dry run for {symbol} {strategy}."
        if action
        else f"Approved allocation dry run skipped for {symbol} {strategy}."
    )
    if blocked_checks:
        message += f" Readiness blocked: {', '.join(blocked_checks)}."

    result = {
        "generated_at": datetime.utcnow(),
        "status": status,
        "dry_run": True,
        "message": message,
        "actions": [action] if action else [],
        "skipped": [skipped] if skipped else [],
        "readiness": readiness,
        "plan": plan,
        "journal_entry_id": journal.id,
        "approved_review": jsonable_encoder(
            {
                "id": journal.id,
                "symbol": journal.symbol,
                "strategy_type": journal.strategy_type,
                "decision": journal.decision,
                "status": journal.status,
                "reason": journal.reason,
                "created_at": journal.created_at,
            }
        ),
        "queue_item": queue_item,
        "paper_only": True,
    }
    write_audit_log(
        db,
        event_type="portfolio_allocation",
        entity_type="candidate_decision",
        entity_id=journal.id,
        action="dry_run_approved_allocation_review",
        status=status,
        message=message,
        payload=jsonable_encoder(
            {
                "symbol": symbol,
                "strategy": strategy,
                "journal_entry_id": journal.id,
                "action_count": len(result["actions"]),
                "skipped_count": len(result["skipped"]),
                "blocked_checks": blocked_checks,
                "actions": result["actions"],
            }
        ),
    )
    db.commit()
    return jsonable_encoder(result)


def execute_allocation_plan(db: Session, *, dry_run: bool = True, max_actions: int = 3, limit: int = 20) -> dict:
    plan = allocation_plan(db, limit=limit)
    readiness = readiness_snapshot(db)
    blocked_checks = [check["name"] for check in readiness["checks"] if check["status"] == "blocked"]
    add_allowed = not blocked_checks
    actions: list[dict] = []
    skipped: list[dict] = []

    for row in plan["recommendations"]:
        if len(actions) >= max_actions:
            skipped.append({**row, "skip_reason": "Maximum allocation actions reached."})
            continue

        action, skipped_row = _allocation_executor_action(
            db,
            row,
            dry_run=dry_run,
            add_allowed=add_allowed,
            blocked_checks=blocked_checks,
        )
        if action:
            actions.append(action)
        if skipped_row:
            skipped.append(skipped_row)

    status = "dry_run" if dry_run else "complete"
    if not actions:
        status = "skipped"
    if blocked_checks and not any(action["action"] == "trim" for action in actions):
        status = "blocked" if not dry_run else status

    message = (
        f"Prepared {len(actions)} allocation action(s)."
        if dry_run
        else f"Executed {len(actions)} allocation action(s)."
    )
    if blocked_checks and not add_allowed:
        message += f" Add actions blocked by readiness: {', '.join(blocked_checks)}."

    result = {
        "generated_at": datetime.utcnow(),
        "status": status,
        "dry_run": dry_run,
        "message": message,
        "actions": actions,
        "skipped": skipped[:10],
        "readiness": readiness,
        "plan": allocation_plan(db, limit=limit) if not dry_run and actions else plan,
    }
    write_audit_log(
        db,
        event_type="portfolio_allocation",
        entity_type="portfolio",
        action="execute_allocation_plan",
        status=status,
        message=message,
        payload=jsonable_encoder(
            {
                "dry_run": dry_run,
                "action_count": len(actions),
                "skipped_count": len(skipped),
                "blocked_checks": blocked_checks,
                "actions": actions,
            }
        ),
    )
    db.commit()
    return result
