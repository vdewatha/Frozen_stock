from __future__ import annotations

from decimal import Decimal
from typing import Optional

from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from app.models import CandidateDecisionJournal, PaperTrade, Strategy, StrategyMemory
from app.services.audit import write_audit_log
from app.services.candidate_evidence import candidate_evidence_drilldown
from app.services.market_data import get_price_history
from app.services.notifications import create_notification


def _journal_row_to_dict(row: CandidateDecisionJournal) -> dict:
    return {
        "id": row.id,
        "symbol": row.symbol,
        "strategy_id": row.strategy_id,
        "strategy_type": row.strategy_type,
        "decision": row.decision,
        "status": row.status,
        "reason": row.reason,
        "evidence_snapshot": row.evidence_snapshot,
        "paper_trade_id": row.paper_trade_id,
        "realized_return": float(row.realized_return) if row.realized_return is not None else None,
        "realized_status": row.realized_status,
        "created_at": row.created_at,
    }


def record_candidate_decision(
    db: Session,
    *,
    symbol: str,
    strategy_type: str,
    decision: str,
    status: str,
    reason: str,
    evidence_snapshot: Optional[dict] = None,
    paper_trade_id: Optional[int] = None,
) -> dict:
    symbol = symbol.strip().upper()
    strategy = db.query(Strategy).filter(Strategy.strategy_type == strategy_type).one_or_none()
    if not strategy:
        raise ValueError(f"Unknown strategy: {strategy_type}")
    evidence = evidence_snapshot or candidate_evidence_drilldown(db, symbol, strategy_type)
    row = CandidateDecisionJournal(
        symbol=symbol,
        strategy_id=strategy.id,
        strategy_type=strategy_type,
        decision=decision,
        status=status,
        reason=reason,
        evidence_snapshot=jsonable_encoder(evidence),
        paper_trade_id=paper_trade_id,
    )
    db.add(row)
    db.flush()
    write_audit_log(
        db,
        event_type="candidate_decision_journal",
        entity_type="candidate_decision",
        entity_id=row.id,
        action=decision,
        status=status,
        message=reason,
        payload={
            "symbol": symbol,
            "strategy": strategy_type,
            "paper_trade_id": paper_trade_id,
            "evidence_generated_at": (row.evidence_snapshot or {}).get("generated_at"),
        },
    )
    return jsonable_encoder(_journal_row_to_dict(row))


def list_candidate_decisions(
    db: Session,
    *,
    limit: int = 25,
    symbol: Optional[str] = None,
    strategy_type: Optional[str] = None,
) -> list[dict]:
    query = db.query(CandidateDecisionJournal)
    if symbol:
        query = query.filter(CandidateDecisionJournal.symbol == symbol.strip().upper())
    if strategy_type:
        query = query.filter(CandidateDecisionJournal.strategy_type == strategy_type)
    rows = query.order_by(CandidateDecisionJournal.created_at.desc()).limit(min(limit, 100)).all()
    return jsonable_encoder([_journal_row_to_dict(row) for row in rows])


def update_journal_realized_outcomes(db: Session) -> dict:
    rows = (
        db.query(CandidateDecisionJournal)
        .filter(CandidateDecisionJournal.paper_trade_id.isnot(None))
        .order_by(CandidateDecisionJournal.created_at.desc())
        .limit(200)
        .all()
    )
    updated = 0
    for row in rows:
        trade = db.query(PaperTrade).filter(PaperTrade.id == row.paper_trade_id).one_or_none()
        if not trade:
            continue
        realized_return = Decimal(str(round(float(trade.profit_loss_pct or 0), 6))) if trade.status == "closed" else None
        realized_status = "open" if trade.status == "open" else "won" if float(trade.profit_loss or 0) > 0 else "lost" if trade.status == "closed" else trade.status
        if row.realized_return != realized_return or row.realized_status != realized_status:
            row.realized_return = realized_return
            row.realized_status = realized_status
            updated += 1
    db.commit()
    return {"updated": updated, "checked": len(rows)}


def refresh_decision_journal_outcomes(db: Session, *, source: str = "manual", notify: bool = False) -> dict:
    rows = db.query(CandidateDecisionJournal).order_by(CandidateDecisionJournal.created_at.desc()).limit(250).all()
    checked = 0
    updated = 0
    newly_scored: list[dict] = []
    for row in rows:
        checked += 1
        previous_return = row.realized_return
        previous_status = row.realized_status
        next_return = previous_return
        next_status = previous_status
        trade_source = "journal"

        if row.paper_trade_id:
            trade = db.query(PaperTrade).filter(PaperTrade.id == row.paper_trade_id).one_or_none()
            if trade:
                next_return = Decimal(str(round(float(trade.profit_loss_pct or 0), 6))) if trade.status == "closed" else None
                next_status = "open" if trade.status == "open" else "won" if float(trade.profit_loss or 0) > 0 else "lost" if trade.status == "closed" else trade.status
                trade_source = "paper_trade"
        else:
            follow_through = _market_follow_through(db, row)
            if follow_through.get("return") is not None:
                next_return = Decimal(str(round(float(follow_through["return"]), 6)))
                next_status = f"market_{follow_through.get('status')}"
                trade_source = "market_follow_through"

        if previous_return != next_return or previous_status != next_status:
            row.realized_return = next_return
            row.realized_status = next_status
            updated += 1

        if previous_return is None and next_return is not None:
            payload = {
                "journal_entry_id": row.id,
                "symbol": row.symbol,
                "strategy": row.strategy_type,
                "decision": row.decision,
                "realized_return": float(next_return),
                "realized_status": next_status,
                "source": trade_source,
                "refresh_source": source,
            }
            newly_scored.append(payload)
            if notify:
                create_notification(
                    db,
                    category="decision_journal",
                    severity="info",
                    source="decision_journal_outcome_refresh",
                    title="Journal decision scored",
                    message=f"{row.symbol} {row.strategy_type} {row.decision} now has outcome {float(next_return):.2%}.",
                    entity_type="candidate_decision",
                    entity_id=row.id,
                    payload=payload,
                )

    if updated or newly_scored:
        write_audit_log(
            db,
            event_type="candidate_decision_journal",
            entity_type="candidate_decision",
            action="refresh_outcomes",
            status="complete",
            message=f"Refreshed {updated} journal outcome row(s); {len(newly_scored)} newly scored.",
            payload={"source": source, "checked": checked, "updated": updated, "newly_scored": newly_scored[:20]},
        )
        memory = update_strategy_memory_from_journal(db)
    else:
        memory = {"updated": 0, "groups": 0, "scored_rows": 0}
    db.commit()
    return {"checked": checked, "updated": updated, "newly_scored": len(newly_scored), "newly_scored_rows": newly_scored[:20], "strategy_memory": memory}


def _market_follow_through(db: Session, row: CandidateDecisionJournal) -> dict:
    evidence = row.evidence_snapshot or {}
    market_data = evidence.get("market_data") or {}
    entry_date = market_data.get("latest_date")
    entry_price = float(market_data.get("latest_close") or 0)
    horizon = int((evidence.get("cached_candidate") or {}).get("horizon_days") or 20)
    prices, source = get_price_history(db, row.symbol, 260)
    if prices.empty or not entry_date or not entry_price:
        return {"source": source, "status": "missing", "horizon_days": horizon, "return": None}

    prices = prices.sort_values("date").reset_index(drop=True)
    entry_index = None
    for index, item in enumerate(prices["date"].astype(str).tolist()):
        if item >= str(entry_date):
            entry_index = index
            break
    if entry_index is None:
        return {"source": source, "status": "missing_entry_date", "horizon_days": horizon, "return": None}

    target_index = min(entry_index + horizon, len(prices) - 1)
    if target_index <= entry_index:
        return {"source": source, "status": "pending", "horizon_days": horizon, "return": None}
    exit_price = float(prices.iloc[target_index]["close"])
    return {
        "source": source,
        "status": "complete" if target_index - entry_index >= horizon else "partial",
        "horizon_days": horizon,
        "entry_date": str(prices.iloc[entry_index]["date"]),
        "exit_date": str(prices.iloc[target_index]["date"]),
        "entry_price": round(entry_price, 6),
        "exit_price": round(exit_price, 6),
        "return": round(exit_price / entry_price - 1, 6) if entry_price else None,
    }


def decision_journal_scorecard(db: Session, *, limit: int = 100, refresh_outcomes: bool = True) -> dict:
    if refresh_outcomes:
        refresh_decision_journal_outcomes(db, source="scorecard", notify=False)
    rows = (
        db.query(CandidateDecisionJournal)
        .order_by(CandidateDecisionJournal.created_at.desc())
        .limit(min(limit, 250))
        .all()
    )
    scored_rows: list[dict] = []
    by_decision: dict[str, dict] = {}
    for row in rows:
        evidence = row.evidence_snapshot or {}
        cached = evidence.get("cached_candidate") or {}
        model_predictions = (evidence.get("model_evidence") or {}).get("predictions") or []
        best_prediction = max(
            model_predictions,
            key=lambda item: (float(item.get("expected_return") or 0), float(item.get("probability_up") or 0)),
            default={},
        )
        follow_through = _market_follow_through(db, row)
        outcome_return = row.realized_return if row.realized_return is not None else follow_through.get("return")
        outcome_status = row.realized_status or follow_through.get("status")
        expected_return = float(best_prediction.get("expected_return") or cached.get("expected_return") or 0)
        probability_up = float(best_prediction.get("probability_up") or cached.get("probability_up") or 0)
        was_positive = outcome_return is not None and float(outcome_return) > 0
        decision_type = row.decision
        should_have_taken = outcome_return is not None and float(outcome_return) > 0.002
        avoided_loss = decision_type in {"reject", "skip"} and outcome_return is not None and float(outcome_return) <= 0
        missed_gain = decision_type in {"reject", "skip"} and should_have_taken
        good_approval = decision_type in {"approve", "activate", "candidate", "review"} and was_positive
        bad_approval = decision_type in {"approve", "activate", "candidate", "review"} and outcome_return is not None and float(outcome_return) <= 0
        quality = (
            "good_approval"
            if good_approval
            else "bad_approval"
            if bad_approval
            else "avoided_loss"
            if avoided_loss
            else "missed_gain"
            if missed_gain
            else "pending"
        )
        score = None
        if outcome_return is not None:
            score = round((1 if was_positive else -1) * (abs(float(outcome_return)) + max(probability_up - 0.5, 0) + max(expected_return, 0)), 6)
            if decision_type in {"reject", "skip"}:
                score = round(-score, 6)
        row_payload = {
            "id": row.id,
            "symbol": row.symbol,
            "strategy_type": row.strategy_type,
            "decision": decision_type,
            "status": row.status,
            "created_at": row.created_at,
            "probability_up": round(probability_up, 6),
            "expected_return": round(expected_return, 6),
            "outcome_return": float(outcome_return) if outcome_return is not None else None,
            "outcome_status": outcome_status,
            "quality": quality,
            "score": score,
            "market_follow_through": follow_through,
        }
        scored_rows.append(row_payload)
        bucket = by_decision.setdefault(
            decision_type,
            {"decision": decision_type, "count": 0, "scored": 0, "positive": 0, "avg_return": 0.0, "avg_score": 0.0},
        )
        bucket["count"] += 1
        if outcome_return is not None:
            bucket["scored"] += 1
            bucket["positive"] += 1 if float(outcome_return) > 0 else 0
            bucket["avg_return"] += float(outcome_return)
            bucket["avg_score"] += float(score or 0)

    for bucket in by_decision.values():
        scored = max(int(bucket["scored"]), 1)
        bucket["hit_rate"] = round(bucket["positive"] / scored, 6) if bucket["scored"] else None
        bucket["avg_return"] = round(bucket["avg_return"] / scored, 6) if bucket["scored"] else None
        bucket["avg_score"] = round(bucket["avg_score"] / scored, 6) if bucket["scored"] else None

    scored = [row for row in scored_rows if row["outcome_return"] is not None]
    positive = [row for row in scored if float(row["outcome_return"]) > 0]
    return jsonable_encoder(
        {
            "journal_count": len(rows),
            "scored_count": len(scored),
            "positive_outcomes": len(positive),
            "hit_rate": round(len(positive) / len(scored), 6) if scored else None,
            "avg_outcome_return": round(sum(float(row["outcome_return"]) for row in scored) / len(scored), 6) if scored else None,
            "by_decision": sorted(by_decision.values(), key=lambda item: item["decision"]),
            "rows": scored_rows[: min(limit, 50)],
        }
    )


def update_strategy_memory_from_journal(db: Session) -> dict:
    scorecard = decision_journal_scorecard(db, limit=250, refresh_outcomes=False)
    rows = [row for row in scorecard["rows"] if row.get("outcome_return") is not None]
    strategies = {strategy.strategy_type: strategy for strategy in db.query(Strategy).all()}
    grouped: dict[tuple[int, str, str], list[dict]] = {}
    for row in rows:
        strategy = strategies.get(row["strategy_type"])
        if not strategy:
            continue
        key = (strategy.id, row["symbol"], "journal_feedback")
        grouped.setdefault(key, []).append(row)

    updated = 0
    for (strategy_id, symbol, regime), group in grouped.items():
        returns = [float(row["outcome_return"] or 0) for row in group]
        positives = [value for value in returns if value > 0]
        negatives = [value for value in returns if value <= 0]
        missed_gains = [row for row in group if row["quality"] == "missed_gain"]
        avoided_losses = [row for row in group if row["quality"] == "avoided_loss"]
        bad_approvals = [row for row in group if row["quality"] == "bad_approval"]
        good_approvals = [row for row in group if row["quality"] == "good_approval"]
        avg_return = sum(returns) / len(returns)
        win_rate = len(positives) / len(group)
        profit_factor = (sum(positives) / abs(sum(negatives))) if negatives and sum(negatives) else (3.0 if positives else 0.0)
        penalty = len(bad_approvals) * 0.08 + len(missed_gains) * 0.04
        bonus = len(good_approvals) * 0.06 + len(avoided_losses) * 0.04
        confidence_score = max(0.0, min(1.0, 0.50 + avg_return * 2 + win_rate * 0.20 + bonus - penalty))
        if missed_gains and not bad_approvals:
            guidance = "Journal feedback: skipped/rejected candidates later gained; consider lowering review friction for similar setups."
        elif bad_approvals:
            guidance = "Journal feedback: approved/reviewed candidates later lost; consider stricter thresholds for similar setups."
        elif avoided_losses:
            guidance = "Journal feedback: skipped/rejected candidates avoided losses; current caution helped."
        else:
            guidance = "Journal feedback: scored decisions are broadly aligned with outcomes."

        memory = (
            db.query(StrategyMemory)
            .filter(StrategyMemory.strategy_id == strategy_id, StrategyMemory.symbol == symbol, StrategyMemory.market_regime == regime)
            .one_or_none()
        )
        if not memory:
            memory = StrategyMemory(strategy_id=strategy_id, symbol=symbol, market_regime=regime)
            db.add(memory)
        memory.sample_size = len(group)
        memory.avg_return = Decimal(str(round(avg_return, 6)))
        memory.avg_drawdown = Decimal(str(round(min(returns), 6)))
        memory.win_rate = Decimal(str(round(win_rate, 6)))
        memory.profit_factor = Decimal(str(round(profit_factor, 6)))
        memory.confidence_score = Decimal(str(round(confidence_score, 6)))
        memory.notes = (
            f"{guidance} "
            f"good approvals={len(good_approvals)}, bad approvals={len(bad_approvals)}, "
            f"avoided losses={len(avoided_losses)}, missed gains={len(missed_gains)}."
        )
        updated += 1
    db.commit()
    return {"updated": updated, "groups": len(grouped), "scored_rows": len(rows)}
