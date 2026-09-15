"""Controlled forward paper evaluation; deliberately independent of the legacy simulator."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from decimal import ROUND_DOWN
from uuid import uuid4
import hashlib
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import (
    StockDatasetSnapshot, StockModelRegistry, StockPaperModelBinding,
    StockPaperTrial, StockPaperTrialDecision, StockPaperTrialMetric, IntradayBar, MarketPrice,
    Strategy, StrategySignal,
)
from app.models.stock_paper import StockPaperOrder, StockPaperPosition, StockPaperStrategyEvidence, StockPaperAccount, StockPaperTrialLot, StockPaperFill
from app.services.feature_pipeline import feature_config_id, generate_features
from app.services.stock_accuracy import market_price_vintage, persist_accuracy_evidence
from app.services.stock_training import FEATURES, _calibrated_probability
from app.services.stock_training_jobs import StockTrainingError, _dataset_from_record, validate_registered_stock_model
from app.services.stock_paper_ledger import reserve_stock_paper_order, dispatch_reserved_order, StockPaperError
from app.services.audit import write_audit_log
from app.services.intraday_data import (
    ALLOWED_SYMBOLS,
    NY,
    feed_status,
    next_regular_session_open,
    preflight_intraday,
    session_bounds,
)
from app.services.risk import DEFAULT_RISK_RULES

POLICY = {
    "regular_sessions": 20, "minimum_closed_trades": 30, "minimum_decision_coverage": "0.90",
    "max_allocated_notional": "10000", "max_risk_per_trade": "0.0025",
    "auto_pause_drawdown": "0.02", "paper_only": True, "live_authorized": False,
}

def _json_safe(value):
    """Keep JSON audit and lineage columns free of database Decimal values."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value

def _trial_equity_curve_max_drawdown(db: Session, trial: StockPaperTrial, as_of: datetime) -> Decimal | None:
    """Gross trial-only curve, marked at verified regular-session closes."""
    if trial.baseline_equity is None or trial.baseline_at is None:
        return None
    symbols = trial.lineage.get("universe", [])
    cursor, sessions = trial.baseline_at.date(), []
    while cursor <= as_of.date() and len(sessions) < int(trial.policy["regular_sessions"]):
        bounds = session_bounds(cursor)
        if bounds and bounds[1] <= as_of and bounds[0] >= trial.baseline_at:
            sessions.append((cursor, bounds))
        cursor += timedelta(days=1)
    if not sessions or not symbols:
        return None
    lots = db.scalars(select(StockPaperTrialLot).where(StockPaperTrialLot.trial_id == trial.id)).all()
    fills = []
    for lot in lots:
        ids = [lot.entry_order_id] if lot.entry_order_id else []
        ids += [o.id for o in db.scalars(select(StockPaperOrder).where(
            StockPaperOrder.trial_lot_id == lot.id,
            StockPaperOrder.side == "sell")).all()]
        fills += db.scalars(select(StockPaperFill).where(
            StockPaperFill.order_id.in_(ids))).all() if ids else []
    peak = trial.baseline_equity
    max_dd = Decimal("0")
    for day, bounds in sessions:
        marks = {}
        for symbol in symbols:
            bar = db.scalar(select(IntradayBar).where(
                IntradayBar.symbol == symbol, IntradayBar.timeframe == "1m",
                IntradayBar.provider == "alpaca", IntradayBar.exchange_timestamp.is_not(None),
                IntradayBar.opened_at >= bounds[0], IntradayBar.opened_at < bounds[1],
                IntradayBar.opened_at <= as_of).order_by(IntradayBar.opened_at.desc()))
            if not bar:
                return None
            marks[symbol] = bar.close
        cashflow = Decimal("0")
        quantities = {symbol: Decimal("0") for symbol in symbols}
        for fill in sorted(fills, key=lambda x: x.filled_at):
            filled_at = fill.filled_at.replace(tzinfo=timezone.utc) if fill.filled_at.tzinfo is None else fill.filled_at
            if filled_at > bounds[1]:
                continue
            sign = Decimal("1") if fill.side == "buy" else Decimal("-1")
            quantities[fill.symbol] = quantities.get(fill.symbol, Decimal("0")) + sign * fill.quantity
            cashflow += (-sign * fill.quantity * fill.price)
        value = trial.baseline_equity + cashflow + sum(
            (qty * marks[symbol] for symbol, qty in quantities.items()), Decimal("0"))
        peak = max(peak, value)
        max_dd = max(max_dd, (peak - value) / peak if peak > 0 else Decimal("0"))
    return max_dd

def _evidence_allows_trade(db: Session, trial: StockPaperTrial, manifest: dict, now: datetime) -> tuple[bool, str | None]:
    """Persist factual forward evidence; manifest holdout is lineage only."""
    account = db.query(StockPaperAccount).filter_by(broker="alpaca_paper").one_or_none()
    if not account:
        return False, "missing_account_baseline"
    lots = db.scalars(select(StockPaperTrialLot).where(StockPaperTrialLot.trial_id == trial.id)).all()
    outcomes = sorted(_lot_outcomes(db, lots), key=lambda x: x["close_at"])
    losses = 0
    for outcome in reversed(outcomes):
        if outcome["outcome"] < 0: losses += 1
        else: break
    drawdown = ((trial.peak_equity - account.equity) / trial.peak_equity
                if trial.peak_equity and trial.peak_equity > 0 else None)
    if drawdown is None or drawdown > Decimal(str(trial.policy["auto_pause_drawdown"])):
        return False, "forward_drawdown_limit_exceeded"
    evidence = db.scalar(select(StockPaperStrategyEvidence).where(
        StockPaperStrategyEvidence.strategy_id == trial.strategy_id)) if trial.strategy_id else None
    if not trial.strategy_id:
        strategy = Strategy(name=f"forward_trial:{trial.id}", strategy_type="stock_forward_trial",
            description="Task12 controlled forward paper trial", parameters={"trial_id": trial.id},
            is_active=True, current_status="paper_trading_active")
        db.add(strategy)
        db.flush()
        trial.strategy_id = strategy.id
        db.add(StockPaperStrategyEvidence(
            strategy_id=strategy.id, evidence_id=f"trial-{trial.id}", status="approved",
            verified_drawdown=drawdown, consecutive_losses=losses, observed_at=now,
            expires_at=now + timedelta(days=1),
            provenance={"trial_id": trial.id, "model_hash": trial.lineage.get("model_hash"),
                        "snapshot_id": trial.lineage.get("snapshot_id"),
                        "phase": "forward_pretrade", "evidence_count": len(
                            db.scalars(select(StockPaperTrialDecision).where(
                                StockPaperTrialDecision.trial_id == trial.id)).all()),
                        "source": "alpaca_account_and_trial_owned_lots"},
        ))
        db.flush()
    else:
        evidence.verified_drawdown = drawdown
        evidence.consecutive_losses = losses
        evidence.observed_at = now
        evidence.expires_at = now + timedelta(days=1)
        evidence.provenance = {**(evidence.provenance or {}), "phase": "forward_pretrade",
                               "evidence_count": len(db.scalars(select(StockPaperTrialDecision).where(
                                   StockPaperTrialDecision.trial_id == trial.id)).all()),
                               "source": "alpaca_account_and_trial_owned_lots"}
    return True, None

def _lot_outcomes(db: Session, lots: list[StockPaperTrialLot]) -> list[dict]:
    """Aggregate every exact exit attempt, preserving FIFO ownership and close time."""
    result = []
    for lot in lots:
        entries = db.scalars(select(StockPaperFill).where(
            StockPaperFill.order_id == lot.entry_order_id, StockPaperFill.side == "buy")).all()
        exit_ids = [o.id for o in db.scalars(select(StockPaperOrder).where(
            StockPaperOrder.trial_lot_id == lot.id,
            StockPaperOrder.side == "sell")).all()]
        exits = db.scalars(select(StockPaperFill).where(
            StockPaperFill.order_id.in_(exit_ids), StockPaperFill.side == "sell")).all() if exit_ids else []
        entry_qty = sum((f.quantity for f in entries), Decimal("0"))
        exit_qty = sum((f.quantity for f in exits), Decimal("0"))
        if entry_qty <= 0 or exit_qty < entry_qty:
            continue
        remaining = entry_qty
        matched_proceeds = Decimal("0")
        matched_exit_fees = Decimal("0")
        costs_known = all(f.cost_known and f.fee is not None for f in entries)
        for fill in sorted(exits, key=lambda x: x.filled_at):
            qty = min(remaining, fill.quantity)
            matched_proceeds += qty * fill.price
            if not fill.cost_known or fill.fee is None:
                costs_known = False
            elif fill.quantity:
                matched_exit_fees += fill.fee * qty / fill.quantity
            remaining -= qty
            if remaining <= 0:
                break
        remaining = exit_qty
        matched_cost = Decimal("0")
        for fill in sorted(entries, key=lambda x: x.filled_at):
            qty = min(remaining, fill.quantity)
            matched_cost += qty * fill.price
            remaining -= qty
            if remaining <= 0:
                break
        gross_outcome = matched_proceeds - matched_cost
        entry_fees = sum((f.fee or Decimal("0") for f in entries), Decimal("0"))
        result.append({
            "closed": True,
            "outcome": gross_outcome,
            "net_outcome": gross_outcome - entry_fees - matched_exit_fees if costs_known else None,
            "costs_known": costs_known,
            "close_at": max(f.filled_at for f in exits),
        })
    return result

def _trial_allocated_notional(db: Session, trial: StockPaperTrial) -> Decimal:
    """Conservative open allocation, counting each owned entry exactly once."""
    lots = db.scalars(select(StockPaperTrialLot).where(
        StockPaperTrialLot.trial_id == trial.id)).all()
    represented = {lot.entry_order_id for lot in lots if lot.entry_order_id}
    allocated = Decimal("0")
    for lot in lots:
        entries = db.scalars(select(StockPaperFill).where(
            StockPaperFill.order_id == lot.entry_order_id, StockPaperFill.side == "buy")).all()
        entry_qty = sum((f.quantity for f in entries), Decimal("0"))
        if entry_qty <= 0:
            continue
        entry_value = sum((f.quantity * f.price for f in entries), Decimal("0"))
        exit_ids = [o.id for o in db.scalars(select(StockPaperOrder).where(
            StockPaperOrder.trial_lot_id == lot.id,
            StockPaperOrder.side == "sell")).all()]
        exits = db.scalars(select(StockPaperFill).where(
            StockPaperFill.order_id.in_(exit_ids), StockPaperFill.side == "sell")).all() if exit_ids else []
        exit_qty = sum((f.quantity for f in exits), Decimal("0"))
        remaining = max(Decimal("0"), entry_qty - exit_qty)
        allocated += remaining * (entry_value / entry_qty)
    # A reservation without a lot is still allocated, but only its unfilled
    # remainder; filled quantity is represented above once a lot exists.
    orders = db.scalars(select(StockPaperOrder).where(
        StockPaperOrder.account_id == db.scalar(select(StockPaperAccount.id).where(
            StockPaperAccount.broker == "alpaca_paper")),
        StockPaperOrder.strategy_id == trial.strategy_id,
        StockPaperOrder.side == "buy",
        StockPaperOrder.status.not_in(("cancelled", "rejected", "failed")),
    )).all() if trial.strategy_id else []
    for order in orders:
        fills = db.scalars(select(StockPaperFill).where(
            StockPaperFill.order_id == order.id, StockPaperFill.side == "buy")).all()
        filled = sum((f.quantity for f in fills), Decimal("0"))
        remaining = max(Decimal("0"), order.quantity - filled)
        # Lot allocation above owns the filled quantity.  This pass owns only
        # the unfilled remainder, including partially-filled lot-linked orders.
        if remaining:
            reference = order.limit_price or (order.reserved_cash / order.quantity if order.quantity else Decimal("0"))
            allocated += remaining * reference
    return allocated

def _sync_trial_lot_state(db: Session, trial: StockPaperTrial) -> list[StockPaperTrialLot]:
    lots = db.scalars(select(StockPaperTrialLot).where(StockPaperTrialLot.trial_id == trial.id)).all()
    for lot in lots:
        entry = sum((f.quantity for f in db.scalars(select(StockPaperFill).where(
            StockPaperFill.order_id == lot.entry_order_id, StockPaperFill.side == "buy")).all()), Decimal("0"))
        ids = [o.id for o in db.scalars(select(StockPaperOrder).where(
            StockPaperOrder.trial_lot_id == lot.id,
            StockPaperOrder.side == "sell")).all()]
        sold = sum((f.quantity for f in db.scalars(select(StockPaperFill).where(
            StockPaperFill.order_id.in_(ids), StockPaperFill.side == "sell")).all()), Decimal("0")) if ids else Decimal("0")
        if entry > 0 and sold >= entry:
            lot.exit_status, lot.exited_quantity = "closed", sold
        elif ids:
            latest = max((db.get(StockPaperOrder, i) for i in ids), key=lambda o: o.id)
            lot.exit_status = latest.status
            lot.exited_quantity = sold
    return lots

def _ensure_exit_intents(db: Session, trial: StockPaperTrial, reason: str, now: datetime) -> None:
    """Create durable risk-reducing intents for every trial-owned open lot."""
    for lot in _sync_trial_lot_state(db, trial):
        entry = sum((f.quantity for f in db.scalars(select(StockPaperFill).where(
            StockPaperFill.order_id == lot.entry_order_id,
            StockPaperFill.side == "buy",
        )).all()), Decimal("0"))
        entry_order = db.get(StockPaperOrder, lot.entry_order_id) if lot.entry_order_id else None
        entry_can_still_fill = bool(
            entry_order and entry_order.status not in {"cancelled", "rejected", "failed"}
        )
        if entry > (lot.exited_quantity or Decimal("0")) or entry_can_still_fill:
            lot.exit_reason = lot.exit_reason or reason
            lot.exit_decided_at = lot.exit_decided_at or now

def _trial_has_managed_exposure(db: Session, trial: StockPaperTrial) -> bool:
    for lot in _sync_trial_lot_state(db, trial):
        entry = sum((f.quantity for f in db.scalars(select(StockPaperFill).where(
            StockPaperFill.order_id == lot.entry_order_id, StockPaperFill.side == "buy")).all()), Decimal("0"))
        if entry > (lot.exited_quantity or Decimal("0")):
            return True
        pending = db.get(StockPaperOrder, lot.entry_order_id) if lot.entry_order_id else None
        if pending and pending.status in {"reserved", "submitting", "new", "accepted",
                                          "pending_new", "partially_filled", "unknown",
                                          "open", "held"}:
            return True
    if trial.strategy_id and db.query(StockPaperOrder).filter(
        StockPaperOrder.strategy_id == trial.strategy_id,
        StockPaperOrder.status.in_(("reserved", "submitting", "new", "accepted",
                                    "pending_new", "partially_filled", "unknown", "open", "held"))
    ).first():
        return True
    return False

def _now(): return datetime.now(timezone.utc)
def _hash(v): return hashlib.sha256(json.dumps(v, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


_FEATURE_DATA_REJECTIONS = {
    "missing_verified_daily_observation",
    "insufficient_daily_feature_history",
    "daily_feature_session_mismatch",
    "feature_timestamp_not_before_decision",
    "stale_decision_expired",
    "trial_window_complete",
}
_LINEAGE_KEYS = (
    "model_run_id", "model_hash", "snapshot_id", "dataset_sha256",
    "cutoff_date", "universe", "cost_assumptions", "binding_hash", "policy_sha256",
)


def _utc(value: datetime) -> datetime:
    """Normalize legacy naive database datetimes without changing their value."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _frozen_window_sessions(
    trial: StockPaperTrial, as_of: datetime,
) -> list[tuple[datetime.date, tuple[datetime, datetime]]]:
    """First N completed regular sessions after start, never a rolling window.

    A missing decision remains in the selected session.  Only exchange
    holidays/non-sessions are absent, so elapsed days cannot replace a missing
    evidence day later in the run.
    """
    if not trial.started_at:
        return []
    start = _utc(trial.started_at)
    end = _utc(as_of)
    cursor = start.astimezone(NY).date()
    result = []
    for _ in range(400):
        if len(result) >= int(trial.policy["regular_sessions"]):
            break
        bounds = session_bounds(cursor)
        if bounds:
            opened, closed = _utc(bounds[0]), _utc(bounds[1])
            if opened >= start and closed <= end:
                result.append((cursor, (opened, closed)))
        # Once a session starts after the as-of boundary no future session can
        # be complete under that boundary.
        if bounds and _utc(bounds[0]) > end:
            break
        cursor += timedelta(days=1)
    return result


def _decision_matches_trial_lineage(trial: StockPaperTrial, decision: StockPaperTrialDecision) -> bool:
    lineage = decision.lineage or {}
    return all(lineage.get(key) == trial.lineage.get(key) for key in _LINEAGE_KEYS)


def _source_evidence(decision: StockPaperTrialDecision) -> tuple[str, str | None]:
    """Classify only persisted source facts; never reconstruct historical health."""
    lineage = decision.lineage or {}
    if not lineage.get("bar_provider") or not lineage.get("bar_exchange_timestamp"):
        return "unknown", "historical_bar_provenance_unavailable"
    if lineage.get("feed_status") != "ready" or not lineage.get("feed_checked_at"):
        return "unknown", "historical_feed_health_unavailable"
    feature_timestamp = lineage.get("feature_timestamp")
    if not feature_timestamp:
        return "unknown", "historical_feature_timestamp_unavailable"
    try:
        feature_at = datetime.fromisoformat(str(feature_timestamp).replace("Z", "+00:00"))
        decision_at = _utc(decision.decision_timestamp)
        if _utc(feature_at) >= decision_at:
            return "rejected", "feature_timestamp_not_before_decision"
    except (TypeError, ValueError):
        return "unknown", "historical_feature_timestamp_unparseable"
    return "verified", None


def _lineage_timestamp(lineage: dict, key: str) -> datetime | None:
    value = lineage.get(key)
    if not value:
        return None
    try:
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def _execution_evidence(
    db: Session, trial: StockPaperTrial, decision: StockPaperTrialDecision,
) -> dict:
    """Safe identifiers for a trial-owned order/fill chain, never broker payloads."""
    if not decision.order_id:
        return {"status": "not_applicable" if decision.action == "reject" else "unlinked"}
    order = db.get(StockPaperOrder, decision.order_id)
    lot = db.scalar(select(StockPaperTrialLot).where(
        StockPaperTrialLot.trial_id == trial.id,
        StockPaperTrialLot.entry_decision_id == decision.id,
        StockPaperTrialLot.entry_order_id == decision.order_id,
    ))
    owned = bool(order and lot and (trial.strategy_id is None or order.strategy_id == trial.strategy_id))
    if not order or not owned:
        return {"status": "unlinked", "order_id": decision.order_id}
    fills = db.scalars(select(StockPaperFill).where(
        StockPaperFill.order_id == order.id,
    ).order_by(StockPaperFill.filled_at, StockPaperFill.id)).all()
    return {
        "status": "linked",
        "order_id": order.id,
        "client_order_id": order.client_order_id,
        "order_status": order.status,
        "lot_id": lot.id,
        "fill_ids": [fill.id for fill in fills],
        "broker_activity_ids": [fill.broker_activity_id for fill in fills],
        "broker_order_ids": sorted({fill.broker_order_id for fill in fills if fill.broker_order_id}),
        "costs_known": all(fill.cost_known and fill.fee is not None for fill in fills) if fills else None,
    }


def build_trial_session_evidence(
    db: Session, trial: StockPaperTrial, *, as_of: datetime | None = None,
) -> dict:
    """Pure, deterministic session evidence used by metrics and readiness reports.

    This deliberately has no feed probes, no synchronization, and no state
    changes.  It can therefore be used to review old evidence safely.
    """
    boundary = _utc(as_of or _now())
    sessions = _frozen_window_sessions(trial, boundary)
    universe = sorted({str(symbol).upper() for symbol in trial.lineage.get("universe", [])})
    decisions = db.scalars(select(StockPaperTrialDecision).where(
        StockPaperTrialDecision.trial_id == trial.id,
    ).order_by(StockPaperTrialDecision.bar_timestamp.desc(), StockPaperTrialDecision.id.asc())).all()
    grouped: dict[tuple[datetime.date, str], list[StockPaperTrialDecision]] = {}
    session_dates = {day for day, _ in sessions}
    for decision in decisions:
        timestamp = _utc(decision.bar_timestamp)
        day = timestamp.astimezone(NY).date()
        if day in session_dates and decision.symbol.upper() in universe:
            grouped.setdefault((day, decision.symbol.upper()), []).append(decision)

    rows, observed, verified, rejected, duplicate_exclusions, unknown_health = [], 0, 0, 0, 0, 0
    for day, bounds in sessions:
        symbols = []
        for symbol in universe:
            candidates = grouped.get((day, symbol), [])
            # The completed-bar worker chooses the most recent completed bar.
            # Any additional decision in the same expected slot is visible but
            # cannot inflate coverage.
            canonical = candidates[0] if candidates else None
            duplicates = candidates[1:]
            duplicate_exclusions += len(duplicates)
            result = {
                "symbol": symbol,
                "expected_decisions": 1,
                "observed_decisions": 0,
                "verified_decisions": 0,
                "duplicate_exclusions": len(duplicates),
                "status": "missing",
                "missing_reason": "no_persisted_decision",
                "rejected_reason": None,
                "historical_feed_health": {"status": "unknown", "reason": "no_decision"},
                "execution": {"status": "not_applicable"},
            }
            if canonical:
                in_session = bounds[0] <= _utc(canonical.bar_timestamp) < bounds[1]
                lineage_ok = _decision_matches_trial_lineage(trial, canonical)
                stale = canonical.rejection_reason in _FEATURE_DATA_REJECTIONS
                source_status, source_reason = _source_evidence(canonical)
                handoff_at = _utc(trial.started_at) if trial.started_at else None
                decision_after_handoff = (
                    handoff_at is None
                    or _utc(canonical.decision_timestamp) >= handoff_at
                )
                observation_at = _lineage_timestamp(
                    canonical.lineage or {}, "observation_timestamp"
                )
                observation_after_handoff = (
                    observation_at is None
                    or handoff_at is None
                    or observation_at >= handoff_at
                )
                decision_out = {
                    "id": canonical.id,
                    "bar_timestamp": _utc(canonical.bar_timestamp).isoformat(),
                    "decision_timestamp": _utc(canonical.decision_timestamp).isoformat(),
                    "action": canonical.action,
                    "qualifying": canonical.qualifying,
                    "rejection_reason": canonical.rejection_reason,
                    "source_ids": {
                        "model_run_id": (canonical.lineage or {}).get("model_run_id"),
                        "snapshot_id": (canonical.lineage or {}).get("snapshot_id"),
                        "feature_hash": (canonical.lineage or {}).get("feature_hash"),
                        "bar_exchange_timestamp": (canonical.lineage or {}).get("bar_exchange_timestamp"),
                    },
                }
                result["decision"] = decision_out
                result["execution"] = _execution_evidence(db, trial, canonical)
                if not in_session:
                    result.update(status="missing", missing_reason="decision_outside_regular_session")
                elif not decision_after_handoff:
                    result.update(
                        status="missing",
                        missing_reason="decision_precedes_trial_handoff",
                    )
                elif not observation_after_handoff:
                    result.update(
                        status="missing",
                        missing_reason="observation_precedes_trial_handoff",
                    )
                elif not lineage_ok:
                    result.update(status="missing", missing_reason="decision_lineage_mismatch")
                elif stale:
                    result.update(status="rejected", missing_reason=None,
                                  rejected_reason=canonical.rejection_reason)
                    rejected += 1
                else:
                    observed += 1
                    result.update(observed_decisions=1, missing_reason=None,
                                  historical_feed_health={"status": source_status, "reason": source_reason})
                    if canonical.action == "reject":
                        result["status"] = "rejected"
                        result["rejected_reason"] = canonical.rejection_reason or "decision_rejected"
                        rejected += 1
                    elif source_status == "verified":
                        result.update(status="verified", verified_decisions=1)
                        verified += 1
                    else:
                        result.update(status="unknown", missing_reason=source_reason)
                if source_status == "unknown":
                    unknown_health += 1
            symbols.append(result)
        rows.append({
            "session_date": day.isoformat(),
            "opened_at": bounds[0].isoformat(),
            "closed_at": bounds[1].isoformat(),
            "status": "elapsed",
            "symbols": symbols,
        })
    expected = len(sessions) * len(universe)
    return {
        "version": 1,
        "as_of": boundary.isoformat(),
        "window": {
            "started_at": _utc(trial.started_at).isoformat() if trial.started_at else None,
            "regular_sessions_required": int(trial.policy["regular_sessions"]),
            "elapsed_regular_sessions": len(sessions),
            "complete": len(sessions) >= int(trial.policy["regular_sessions"]),
            "session_dates": [day.isoformat() for day, _ in sessions],
        },
        "sessions": rows,
        "aggregates": {
            "expected_decisions": expected,
            "observed_decisions": observed,
            "verified_decisions": verified,
            "missing_decisions": expected - observed,
            "rejected_decisions": rejected,
            "duplicate_exclusions": duplicate_exclusions,
            "unknown_historical_feed_health": unknown_health,
            "decision_coverage": str(Decimal(observed) / Decimal(expected)) if expected else "0",
            "verified_decision_coverage": str(Decimal(verified) / Decimal(expected)) if expected else "0",
        },
        "source_lineage": {key: trial.lineage.get(key) for key in _LINEAGE_KEYS},
    }


def validate_trial_artifact(db: Session, trial: StockPaperTrial) -> dict:
    """Revalidate the exact immutable binding on every worker invocation."""
    binding = db.get(StockPaperModelBinding, trial.binding_id)
    model = db.get(StockModelRegistry, binding.model_run_id) if binding else None
    snapshot = db.get(StockDatasetSnapshot, binding.snapshot_id) if binding else None
    if (
        not binding
        or not model
        or not snapshot
        or model.snapshot_id != snapshot.snapshot_id
        or binding.paper_only is False
        or binding.live_authorized is True
    ):
        raise StockTrainingError("Trial immutable lineage is unavailable")
    if binding.binding_sha256 != trial.lineage.get("binding_hash"):
        raise StockTrainingError("Trial immutable binding hash has changed")
    if model.manifest_sha256 != trial.lineage.get("model_hash") or snapshot.snapshot_id != trial.lineage.get("snapshot_id"):
        raise StockTrainingError("Trial immutable lineage has changed")
    manifest = validate_registered_stock_model(model, _dataset_from_record(snapshot))
    if trial.lineage.get("policy_sha256") != _hash(trial.policy):
        raise StockTrainingError("Trial approved policy has changed")
    if _hash({k: trial.lineage[k] for k in ("model_run_id", "model_hash", "snapshot_id", "dataset_sha256", "cutoff_date", "universe", "cost_assumptions", "binding_hash", "policy_sha256")}) != trial.lineage.get("lineage_sha256", _hash({})):
        # Older rows predate the digest; new rows below always carry one.
        if trial.lineage.get("lineage_sha256"): raise StockTrainingError("Trial lineage digest mismatch")
    return manifest


def trial_feed_preflight(
    db: Session,
    trial: StockPaperTrial,
    *,
    now: datetime | None = None,
    authenticated_probe: bool = False,
) -> dict:
    """Return safe, current-session feed and paper-ledger readiness evidence."""
    observed_at = now or _now()
    symbols = [str(value).upper() for value in trial.lineage.get("universe", [])]
    statuses = []
    bounds = session_bounds(observed_at.astimezone(NY).date())
    in_session = bool(bounds and bounds[0] <= observed_at < bounds[1])
    next_open = None if in_session else next_regular_session_open(observed_at).isoformat()
    frozen_universe = set(symbols) == set(ALLOWED_SYMBOLS)
    probe_results = None
    if frozen_universe and in_session and authenticated_probe:
        probe = preflight_intraday(db, symbols, now=observed_at)
        probe_results = {item["symbol"]: item for item in probe.get("results", [])}
    for symbol in symbols:
        try:
            status = (
                probe_results.get(symbol)
                if probe_results is not None
                else feed_status(db, symbol, now=observed_at)
            )
            if status is None:
                raise RuntimeError("Missing authenticated SIP preflight result")
            statuses.append(
                {
                    "symbol": symbol,
                    "status": status.get("status"),
                    "entitlement_state": status.get("entitlement_state"),
                    "exchange_timestamp": (
                        status["exchange_timestamp"].isoformat()
                        if isinstance(status.get("exchange_timestamp"), datetime)
                        else status.get("exchange_timestamp")
                        if status.get("exchange_timestamp")
                        else None
                    ),
                    "ingestion_timestamp": (
                        status["ingestion_timestamp"].isoformat()
                        if isinstance(status.get("ingestion_timestamp"), datetime)
                        else status.get("ingestion_timestamp")
                        if status.get("ingestion_timestamp")
                        else None
                    ),
                    "latency_seconds": status.get("latency_seconds"),
                    "missing_intervals": status.get("missing_intervals", []),
                    "unavailable_reason": status.get("unavailable_reason"),
                }
            )
        except Exception:
            statuses.append(
                {
                    "symbol": symbol,
                    "status": "unavailable",
                    "entitlement_state": "unverified",
                    "exchange_timestamp": None,
                    "ingestion_timestamp": None,
                    "latency_seconds": None,
                    "missing_intervals": [],
                    "unavailable_reason": "Authenticated Alpaca SIP feed status unavailable",
                }
            )
    account = db.query(StockPaperAccount).filter_by(broker="alpaca_paper").one_or_none()
    ledger_ready = bool(
        account
        and account.status == "reconciled"
        and not account.reconciliation_required
        and account.accounting_verified
        and not account.unexplained_residual
    )
    # The approved trial is the four-symbol frozen universe and must only be
    # resumed from an active regular-session preflight. Older synthetic
    # one-symbol fixtures remain usable for non-operational unit coverage.
    legacy_fixture_outside_session = not frozen_universe and not in_session
    feed_configuration_valid = settings.alpaca_feed.strip().lower() == "sip"
    feed_ready = (
        (
            feed_configuration_valid
            and in_session
            and bool(statuses)
            and all(item["status"] == "ready" for item in statuses)
        )
        if frozen_universe
        else (
            feed_configuration_valid
            and (
                legacy_fixture_outside_session
                or not statuses
                or all(item["status"] == "ready" for item in statuses)
            )
        )
    )
    failures = [
        f"{item['symbol']}: {item.get('unavailable_reason') or item['status']}"
        for item in statuses
        if item["status"] != "ready"
    ]
    if not feed_configuration_valid:
        reason = "Alpaca SIP feed is not configured"
    elif frozen_universe and not in_session:
        reason = "Regular-session authenticated preflight is required"
    elif failures and not legacy_fixture_outside_session:
        reason = failures[0]
    elif not ledger_ready:
        reason = "Alpaca paper ledger is not reconciled"
    else:
        reason = None
    return {
        "status": "ready" if feed_ready and ledger_ready else "blocked",
        "ready": feed_ready and ledger_ready,
        "checked_at": observed_at.isoformat(),
        "regular_session": in_session,
        "next_regular_session_open": next_open,
        "symbols": statuses,
        "paper_ledger": {
            "status": "reconciled" if ledger_ready else "blocked",
            "reason": None if ledger_ready else "Alpaca paper ledger is not reconciled",
        },
        "reason": reason,
        "paper_only": True,
        "live_authorized": False,
    }

def create_trial(
    db: Session, *, binding_id: int, actor: str, source_cycle_id: str | None = None,
    authorization: dict | None = None,
) -> StockPaperTrial:
    if source_cycle_id:
        existing = db.scalar(select(StockPaperTrial).where(
            StockPaperTrial.source_cycle_id == source_cycle_id,
        ))
        if existing:
            if existing.binding_id != binding_id:
                raise StockTrainingError("Scheduled cycle already owns a different forward trial")
            return existing
    binding = db.get(StockPaperModelBinding, binding_id)
    if not binding or not binding.paper_only or binding.live_authorized:
        raise StockTrainingError("Only an immutable paper-only model binding may approve a trial")
    model, snapshot = db.get(StockModelRegistry, binding.model_run_id), db.get(StockDatasetSnapshot, binding.snapshot_id)
    if not model or not snapshot or model.snapshot_id != snapshot.snapshot_id:
        raise StockTrainingError("Binding registry or snapshot evidence is missing")
    try:
        manifest = validate_registered_stock_model(model, _dataset_from_record(snapshot))
    except StockTrainingError:
        raise
    lineage = {"model_run_id": model.run_id, "model_hash": model.manifest_sha256,
               "snapshot_id": snapshot.snapshot_id, "dataset_sha256": snapshot.dataset_sha256,
               "cutoff_date": snapshot.cutoff_date.isoformat(), "universe": snapshot.universe,
               "cost_assumptions": manifest.get("cost_assumptions"),
               "binding_hash": binding.binding_sha256}
    lineage["policy_sha256"] = _hash(POLICY)
    blocked = None
    if not snapshot.metadata_json.get("binding_eligible", False):
        blocked = snapshot.metadata_json.get("binding_eligibility_reason", "Snapshot is not binding eligible")
    lineage["lineage_sha256"] = _hash(lineage)
    row = StockPaperTrial(id=str(uuid4()), binding_id=binding.id, actor=actor,
                          source_cycle_id=source_cycle_id,
                          status="blocked" if blocked else "approved", policy=dict(POLICY),
                          lineage=lineage, blocked_reason=blocked)
    db.add(row)
    return row

def start_trial(
    db: Session,
    trial_id: str,
    *,
    actor: str = "system",
    authorization: dict | None = None,
) -> StockPaperTrial:
    row = db.get(StockPaperTrial, trial_id)
    if not row: raise StockTrainingError("Trial not found")
    if row.status not in {"approved", "paused", "blocked"}: raise StockTrainingError("Trial cannot be started")
    immutable_block = bool(
        row.blocked_reason
        and not (
            row.blocked_reason.startswith("Alpaca paper ledger")
            or row.blocked_reason.startswith("fresh_complete_feed_required")
            or row.blocked_reason.startswith("Regular-session")
        )
    )
    try:
        validate_trial_artifact(db, row)
    except StockTrainingError:
        if immutable_block:
            return row
        raise
    if immutable_block:
        return row
    if row.policy.get("paper_only") is not True or row.policy.get("live_authorized") is not False:
        row.status, row.blocked_reason = "blocked", "Trial policy is not paper-only"
        write_audit_log(
            db,
            event_type="stock_forward_trial",
            action="resume",
            status="blocked",
            message="Trial resume blocked by immutable paper-only policy",
            entity_type="stock_paper_trial",
            payload={"trial_id": row.id, "operator": actor, "authorization": authorization or {}, "paper_only": True, "live_authorized": False},
        )
        return row
    # Starting is intentionally conservative: observe/reconcile task must establish these facts.
    from app.models.stock_paper import StockPaperAccount
    account = db.query(StockPaperAccount).filter_by(broker="alpaca_paper").one_or_none()
    if (
        not account
        or account.status != "reconciled"
        or account.reconciliation_required
        or account.unexplained_residual
    ):
        row.status, row.blocked_reason = "blocked", "Alpaca paper ledger is not reconciled"
        write_audit_log(
            db,
            event_type="stock_forward_trial",
            action="resume",
            status="blocked",
            message=row.blocked_reason,
            entity_type="stock_paper_trial",
            payload={"trial_id": row.id, "operator": actor, "authorization": authorization or {}, "paper_only": True, "live_authorized": False},
        )
        return row
    now = _now()
    preflight = trial_feed_preflight(db, row, now=now, authenticated_probe=True)
    if not preflight["ready"]:
        row.status = "blocked"
        row.blocked_reason = f"fresh_complete_feed_required:{preflight['reason']}"
        write_audit_log(
            db,
            event_type="stock_forward_trial",
            action="resume",
            status="blocked",
            message=row.blocked_reason,
            entity_type="stock_paper_trial",
            payload={"trial_id": row.id, "operator": actor, "authorization": authorization or {}, "preflight": preflight},
        )
        return row
    # Binding eligibility blocks are immutable; operational preflight blocks
    # may clear only after every check above succeeds.
    if immutable_block:
        return row
    row.blocked_reason = None
    if row.baseline_equity is None:
        row.baseline_equity, row.baseline_at, row.peak_equity = account.equity, now, account.equity
    elif row.peak_equity is None or account.equity > row.peak_equity:
        row.peak_equity = account.equity
    row.status, row.started_at, row.pause_reason = "running", row.started_at or now, None
    write_audit_log(
        db,
        event_type="stock_forward_trial",
        action="resume",
        status="resumed",
        message="Operator resumed paper trial after authenticated SIP and ledger preflight",
        entity_type="stock_paper_trial",
        payload={"trial_id": row.id, "operator": actor, "authorization": authorization or {}, "preflight": preflight},
    )
    return row

def pause_trial(
    db: Session,
    trial_id: str,
    reason: str,
    *,
    actor: str = "system",
    authorization: dict | None = None,
) -> StockPaperTrial:
    row = db.get(StockPaperTrial, trial_id)
    if not row: raise StockTrainingError("Trial not found")
    if row.status == "running": row.status, row.pause_reason = "paused", reason
    write_audit_log(
        db,
        event_type="stock_forward_trial",
        action="pause",
        status="complete",
        message=reason,
        entity_type="stock_paper_trial",
        payload={"trial_id": row.id, "actor": actor, "authorization": authorization or {}},
    )
    return row

def _accounting_halt_reason(account: StockPaperAccount | None) -> str | None:
    """Return the fail-closed trial reason for an unsafe paper ledger."""
    if (
        not account
        or account.status != "reconciled"
        or account.reconciliation_required
    ):
        return "account_uncertainty"
    if not account.accounting_verified or account.unexplained_residual:
        return "accounting_review_required"
    return None

def _halt_trial_for_accounting(
    db: Session,
    trial: StockPaperTrial,
    account: StockPaperAccount | None,
) -> str | None:
    """Pause active trial work and persist safe evidence of an accounting halt."""
    reason = _accounting_halt_reason(account)
    if reason is None:
        return None
    before = (trial.status, trial.pause_reason, trial.blocked_reason)
    if trial.status == "running":
        trial.status, trial.pause_reason = "paused", reason
    elif trial.status == "paused":
        trial.pause_reason = reason
    elif trial.status == "blocked":
        trial.blocked_reason = reason
    after = (trial.status, trial.pause_reason, trial.blocked_reason)
    if before != after and trial.status in {"paused", "blocked"}:
        write_audit_log(
            db,
            event_type="stock_forward_trial",
            action="accounting_halt",
            status="blocked",
            message="Forward trial halted because Alpaca paper accounting requires review",
            entity_type="stock_paper_trial",
            payload={
                "trial_id": trial.id,
                "reason": reason,
                "account_status": account.status if account else None,
                "reconciliation_required": (
                    account.reconciliation_required if account else True
                ),
                "accounting_verified": (
                    account.accounting_verified if account else False
                ),
                "unexplained_residual": (
                    account.unexplained_residual if account else False
                ),
            },
        )
    return reason

def stop_trial(
    db: Session,
    trial_id: str,
    *,
    actor: str = "system",
    reason: str = "Operator stopped paper forward trial",
    authorization: dict | None = None,
) -> StockPaperTrial:
    row = db.get(StockPaperTrial, trial_id)
    if not row: raise StockTrainingError("Trial not found")
    now = _now()
    _ensure_exit_intents(db, row, "operator_stop", now)
    open_lots = _trial_has_managed_exposure(db, row)
    if row.status not in {"stopped", "completed"}:
        row.status, row.stopped_at = ("stopped", now) if open_lots else ("completed", now)
    write_audit_log(
        db,
        event_type="stock_forward_trial",
        action="stop",
        status="complete",
        message=reason,
        entity_type="stock_paper_trial",
        payload={"trial_id": row.id, "actor": actor, "authorization": authorization or {}},
    )
    return row

def record_decision(db: Session, trial_id: str, *, symbol: str, bar_timestamp: datetime,
                    qualifying: bool = False, reference_price: Decimal | None = None,
                    evidence: bool = False, feature_timestamp: datetime | None = None) -> StockPaperTrialDecision:
    row = db.get(StockPaperTrial, trial_id)
    if not row: raise StockTrainingError("Trial not found")
    if row.status != "running": raise StockTrainingError("Trial is not running")
    decision_at = _now()
    reason = None
    if feature_timestamp is None or feature_timestamp >= decision_at: reason = "feature_timestamp_not_before_decision"
    elif not qualifying: reason = "model_signal_not_qualifying"
    elif not evidence: reason = "missing_qualifying_evidence"
    action = "buy" if reason is None else "reject"
    decision = StockPaperTrialDecision(trial_id=trial_id, symbol=symbol.upper(), bar_timestamp=bar_timestamp,
        decision_timestamp=decision_at, action=action, qualifying=reason is None,
        rejection_reason=reason, lineage=_json_safe(row.lineage))
    try:
        with db.begin_nested():
            # Keep both INSERT and constraint handling inside the savepoint;
            # otherwise SQLAlchemy can leave the outer Session in rollback
            # state before the idempotent lookup runs.
            db.add(decision)
            db.flush()
    except IntegrityError:
        return db.scalar(select(StockPaperTrialDecision).where(
            StockPaperTrialDecision.trial_id == trial_id,
            StockPaperTrialDecision.symbol == symbol.upper(),
            StockPaperTrialDecision.bar_timestamp == bar_timestamp))
    if reason is None:
        try:
            order = reserve_stock_paper_order(db, symbol=symbol, side="buy", quantity=Decimal("1"),
                reference_price=reference_price or Decimal("0"), idempotency_key=f"trial:{trial_id}:{symbol}:{bar_timestamp.isoformat()}",
                source="manual_control_room")
            decision.order_id = order.id
            dispatch_reserved_order(db, order.id)
        except StockPaperError as exc:
            decision.action, decision.qualifying, decision.rejection_reason = "reject", False, str(exc)
    return decision

def observe_trial(db: Session, trial_id: str) -> dict:
    """Internal-only worker entry point. No request payload can create decisions."""
    trial = db.get(StockPaperTrial, trial_id)
    if not trial: return {"status": "missing", "trial_id": trial_id}
    if trial.status not in {"running", "paused", "stopped", "blocked"}:
        return {"status": trial.status, "trial_id": trial_id}
    _sync_trial_lot_state(db, trial)
    now = _now()
    if trial.status == "stopped":
        _ensure_exit_intents(db, trial, "operator_stop", now)
        return {"status": "stopped", "trial_id": trial_id, "decisions": 0,
                "reason": trial.pause_reason, "paper_only": True}
    account = db.query(StockPaperAccount).filter_by(broker="alpaca_paper").one_or_none()
    if account and _halt_trial_for_accounting(db, trial, account):
        return {
            "status": trial.status,
            "trial_id": trial_id,
            "decisions": 0,
            "reason": trial.pause_reason or trial.blocked_reason,
            "paper_only": True,
        }
    manifest = validate_trial_artifact(db, trial)
    bounds = session_bounds(now.astimezone(timezone.utc).date())
    feed_checks = {}
    for symbol in trial.lineage.get("universe", []):
        try:
            status = feed_status(db, symbol, now=now)
        except Exception as exc:
            if trial.status == "running":
                trial.status = "paused"
            trial.pause_reason = f"fresh_complete_feed_required:{symbol}:feed_unavailable:{exc}"
            return {"status": "paused", "trial_id": trial_id, "reason": trial.pause_reason}
        if status.get("status") in {"unavailable", "incomplete", "stale"}:
            reason = status.get("unavailable_reason") or status.get("status") or "feed unavailable"
            if trial.status == "running":
                trial.status = "paused"
            trial.pause_reason = f"fresh_complete_feed_required:{symbol}:{reason}"
            return {"status": "paused", "trial_id": trial_id, "reason": trial.pause_reason}
        feed_checks[symbol] = status
    # Daily inference is deliberately gated until the regular session has
    # closed.  The 1m Alpaca bar is execution lineage only; features are built
    # from the latest completed, verified Yahoo daily observation.
    if not bounds or now < bounds[1]:
        return {"status": "observed", "trial_id": trial_id, "decisions": 0,
                "reason": "regular_session_not_closed", "paper_only": True}
    if account is None:
        accounting_reason = _halt_trial_for_accounting(db, trial, account)
        return {
            "status": trial.status,
            "trial_id": trial_id,
            "decisions": 0,
            "reason": accounting_reason,
            "paper_only": True,
        }
    symbols = trial.lineage.get("universe", [])
    artifact = Path(getattr(db.get(StockModelRegistry, trial.lineage["model_run_id"]), "artifact_path", ""))
    model_path, calibrator_path = artifact / "selected_model.joblib", artifact / "calibrator.joblib"
    if not model_path.is_file() or not calibrator_path.is_file():
        raise StockTrainingError("Exact selected model and calibrator artifacts are unavailable")
    model, calibrator = joblib.load(model_path), joblib.load(calibrator_path)
    if trial.peak_equity is None:
        trial.peak_equity = account.equity
    elif account.equity > trial.peak_equity:
        trial.peak_equity = account.equity
    if trial.peak_equity > 0 and account.equity <= trial.peak_equity * Decimal("0.98"):
        trial.status, trial.pause_reason = "paused", "drawdown_limit_2_percent"
        _ensure_exit_intents(db, trial, "drawdown_limit_2_percent", now)
        return {"status": "paused", "trial_id": trial_id, "reason": trial.pause_reason}
    trial_dd = _trial_equity_curve_max_drawdown(db, trial, now)
    if trial_dd is not None and trial_dd >= Decimal("0.02"):
        trial.status, trial.pause_reason = "paused", "trial_drawdown_limit_2_percent"
        _ensure_exit_intents(db, trial, "trial_drawdown_limit_2_percent", now)
        return {"status": "paused", "trial_id": trial_id, "executed": 0}
    # Risk-reducing exits are evaluated before any new entry.  A trial position
    # is attributable through its order.strategy_id and is never sold short.
    lots = db.scalars(select(StockPaperTrialLot).where(StockPaperTrialLot.trial_id == trial.id)).all()
    for lot in lots:
        latest_exit = db.get(StockPaperOrder, lot.exit_order_id) if lot.exit_order_id else None
        if latest_exit and latest_exit.status in {"reserved", "submitting", "new", "accepted",
                                                   "pending_new", "partially_filled", "unknown",
                                                   "open", "held"}:
            continue
        candidates = db.scalars(select(IntradayBar).where(
            IntradayBar.symbol == lot.symbol, IntradayBar.timeframe == "1m",
            IntradayBar.provider == "alpaca", IntradayBar.exchange_timestamp.is_not(None)
        ).order_by(IntradayBar.opened_at.desc())).all()
        final_bar = next((bar for bar in candidates
            if (lambda opened: (session_bounds(opened.date()) and
                session_bounds(opened.date())[0] <= opened < session_bounds(opened.date())[1] and opened < now))
               (bar.opened_at.replace(tzinfo=timezone.utc) if bar.opened_at.tzinfo is None else bar.opened_at)), None)
        entries = db.scalars(select(StockPaperFill).where(
            StockPaperFill.order_id == lot.entry_order_id, StockPaperFill.side == "buy"
        )).all()
        if not final_bar or not entries:
            continue
        buy_filled = sum((f.quantity for f in entries), Decimal("0"))
        entry_price = sum((f.quantity * f.price for f in entries), Decimal("0")) / buy_filled
        exit_ids = [o.id for o in db.scalars(select(StockPaperOrder).where(
            StockPaperOrder.trial_lot_id == lot.id,
            StockPaperOrder.side == "sell")).all()]
        sold = sum((f.quantity for f in db.scalars(select(StockPaperFill).where(
            StockPaperFill.order_id.in_(exit_ids), StockPaperFill.side == "sell")).all()), Decimal("0")) if exit_ids else Decimal("0")
        remaining_owned = max(Decimal("0"), buy_filled - sold)
        if remaining_owned <= 0:
            continue
        stop = final_bar.close <= entry_price * Decimal("0.98")
        entry_decision = db.get(StockPaperTrialDecision, lot.entry_decision_id)
        horizon = False
        if entry_decision:
            candidates = db.scalars(select(IntradayBar.opened_at).where(
                IntradayBar.symbol == lot.symbol, IntradayBar.timeframe == "1m",
                IntradayBar.provider == "alpaca", IntradayBar.exchange_timestamp.is_not(None),
                IntradayBar.opened_at > entry_decision.bar_timestamp,
                IntradayBar.opened_at <= now,
            ).order_by(IntradayBar.opened_at.asc())).all()
            eligible = set()
            for opened_at in candidates:
                opened_at = opened_at.replace(tzinfo=timezone.utc) if opened_at.tzinfo is None else opened_at
                day = opened_at.date()
                session = session_bounds(day)
                if session and session[0] <= opened_at <= session[1]:
                    eligible.add(day)
            horizon = len(eligible) >= 5
        if stop or horizon:
            lot.exit_reason = "stop" if stop else "horizon"
            lot.exit_decided_at = now
            lot.exit_reference_price = final_bar.close
    if trial.status != "running":
        return {"status": trial.status, "trial_id": trial_id, "decisions": 0, "paper_only": True}
    inserted = 0
    for symbol in symbols:
        bars = db.scalars(select(IntradayBar).where(
            IntradayBar.timeframe == "1m", IntradayBar.symbol == symbol
        ).order_by(IntradayBar.opened_at.desc())).all()
        bars = [bar for bar in bars
                if (bar.opened_at.replace(tzinfo=timezone.utc) if bar.opened_at.tzinfo is None else bar.opened_at)
                < bounds[1] and
                (bar.opened_at.replace(tzinfo=timezone.utc) if bar.opened_at.tzinfo is None else bar.opened_at) < now
                and str(bar.provider).lower() == "alpaca" and bar.exchange_timestamp is not None]
        if not bars:
            continue
        bar = bars[0]
        opened = bar.opened_at.replace(tzinfo=timezone.utc) if bar.opened_at.tzinfo is None else bar.opened_at
        if opened < bounds[0] or opened < bounds[1] - timedelta(minutes=1):
            continue
        existing = db.scalar(select(StockPaperTrialDecision.id).where(
            StockPaperTrialDecision.trial_id == trial.id,
            StockPaperTrialDecision.symbol == symbol,
            StockPaperTrialDecision.bar_timestamp == bar.opened_at,
        ))
        if existing: continue
        daily = db.scalars(select(MarketPrice).where(
            MarketPrice.symbol == symbol, MarketPrice.source.in_(("yfinance", "yahoo_chart")),
            MarketPrice.price_date <= now.astimezone(timezone.utc).date()
        ).order_by(MarketPrice.price_date.asc())).all()
        reason, probability, features, feature_timestamp = None, None, None, None
        feature_data_version, feature_provider = None, None
        if not daily:
            reason = "missing_verified_daily_observation"
        else:
            providers = {str(row.source) for row in daily}
            if len(providers) != 1:
                reason = "mixed_feature_provider_provenance"
            else:
                feature_provider = next(iter(providers))
                feature_data_version = market_price_vintage(daily)
            try:
                frame = pd.DataFrame([{"date": r.price_date, "open": float(r.open), "close": float(r.adjusted_close or r.close),
                                       "volume": int(r.volume)} for r in daily])
            except (TypeError, ValueError):
                frame = pd.DataFrame()
            try:
                featured = generate_features(frame, version="1").dropna(subset=FEATURES)
            except (KeyError, ValueError):
                featured = pd.DataFrame()
            if reason == "mixed_feature_provider_provenance":
                featured = pd.DataFrame()
            elif featured.empty:
                reason = "insufficient_daily_feature_history"
            else:
                latest = featured.iloc[-1]
                feature_timestamp = pd.Timestamp(latest["date"]).to_pydatetime().replace(tzinfo=timezone.utc)
                intended_session = bounds[0].astimezone(timezone.utc).date()
                if feature_timestamp.date() != intended_session:
                    reason = "daily_feature_session_mismatch"
                elif feature_timestamp >= now:
                    reason = "feature_timestamp_not_before_decision"
                else:
                    values = np.asarray([latest[name] for name in FEATURES], dtype=float).reshape(1, -1)
                    raw = model.predict_proba(values)[:, 1]
                    probability = float(_calibrated_probability(calibrator, raw)[0])
                    features = {name: float(latest[name]) for name in FEATURES}
                    if probability < 0.5:
                        reason = "model_signal_not_qualifying"
        # Daily data arrives independently after the close. Do not consume the
        # unique session decision key until the exact session feature exists;
        # the next minute cycle can safely retry.
        if reason in {
            "missing_verified_daily_observation",
            "insufficient_daily_feature_history",
            "daily_feature_session_mismatch",
            "feature_timestamp_not_before_decision",
            "mixed_feature_provider_provenance",
        }:
            continue
        lineage = {**_json_safe(trial.lineage), "bar_provider": bar.provider,
                   "bar_exchange_timestamp": bar.exchange_timestamp.isoformat() if bar.exchange_timestamp else None,
                   "execution_reference_timestamp": opened.isoformat(),
                   "observation_timestamp": now.isoformat(),
                   "decision_cutoff_timestamp": now.isoformat(),
                   "feature_timestamp": feature_timestamp.isoformat() if feature_timestamp else None,
                   "feature_data_version": feature_data_version,
                   "feature_provider": feature_provider,
                   "feature_config_id": feature_config_id(),
                   "feature_row_count": len(daily),
                   "feed_status": feed_checks.get(symbol, {}).get("status"),
                   "feed_checked_at": now.isoformat(),
                   "feature_hash": _hash(features) if features else None, "probability": probability,
                   "threshold": 0.5, "model_artifact": str(model_path)}
        action = "reject" if reason else "buy"
        decision = StockPaperTrialDecision(
            trial_id=trial.id, symbol=symbol, bar_timestamp=bar.opened_at,
            decision_timestamp=now, action=action, qualifying=not reason,
            rejection_reason=reason, lineage=lineage)
        db.add(decision)
        try:
            with db.begin_nested():
                db.flush()
            inserted += 1
        except IntegrityError:
            pass
        if reason:
            continue
        # Evidence is persisted from the exact manifest, but this stage only
        # records a pending decision.  No broker reservation or dispatch is
        # permitted after the session closes.
        allowed, evidence_reason = _evidence_allows_trade(db, trial, manifest, now)
        if not allowed:
            decision.action, decision.qualifying, decision.rejection_reason = "reject", False, evidence_reason
            continue
    return {"status": "observed", "trial_id": trial_id, "decisions": inserted, "paper_only": True}

def execute_pending_decisions(db: Session, trial_id: str) -> dict:
    """Execute persisted post-close decisions only during the next regular session."""
    trial = db.get(StockPaperTrial, trial_id)
    if not trial:
        return {"status": "missing", "trial_id": trial_id}
    if trial.status not in {"running", "paused", "stopped", "blocked"}:
        return {"status": trial.status, "trial_id": trial_id, "executed": 0}
    _sync_trial_lot_state(db, trial)
    now = _now()
    account = db.query(StockPaperAccount).filter_by(broker="alpaca_paper").one_or_none()
    if account and _halt_trial_for_accounting(db, trial, account):
        return {
            "status": trial.status,
            "trial_id": trial_id,
            "executed": 0,
            "reason": trial.pause_reason or trial.blocked_reason,
        }
    bounds = session_bounds(now.astimezone(timezone.utc).date())
    if not bounds or not (bounds[0] <= now < bounds[1]):
        return {"status": "waiting", "trial_id": trial_id, "executed": 0,
                "reason": "regular_session_required"}
    if account is None:
        accounting_reason = _halt_trial_for_accounting(db, trial, account)
        return {
            "status": trial.status,
            "trial_id": trial_id,
            "executed": 0,
            "reason": accounting_reason,
        }
    # Entries are executable only in the session immediately following the
    # decision's eligible close.
    previous_day = now.astimezone(timezone.utc).date() - timedelta(days=1)
    previous_bounds = None
    for _ in range(10):
        previous_bounds = session_bounds(previous_day)
        if previous_bounds:
            break
        previous_day -= timedelta(days=1)
    if trial.peak_equity is None or account.equity > trial.peak_equity:
        trial.peak_equity = account.equity
    drawdown_blocked = bool(
        trial.peak_equity > 0 and account.equity <= trial.peak_equity * Decimal("0.98")
    )
    if drawdown_blocked:
        if trial.status != "stopped":
            trial.status = "paused"
        trial.pause_reason = "drawdown_limit_2_percent"
        _ensure_exit_intents(db, trial, "drawdown_limit_2_percent", now)
    # Dispatch durable post-close exit intents first.  Only completed current
    # session references are accepted by the shared ledger.
    for lot in db.scalars(select(StockPaperTrialLot).where(
        StockPaperTrialLot.trial_id == trial.id, StockPaperTrialLot.exit_reason.is_not(None)
    )).all():
        current = db.get(StockPaperOrder, lot.exit_order_id) if lot.exit_order_id else None
        if current and current.status in {"reserved", "submitting", "new", "accepted",
                                          "pending_new", "partially_filled", "unknown", "open", "held"}:
            continue
        entries = db.scalars(select(StockPaperFill).where(
            StockPaperFill.order_id == lot.entry_order_id, StockPaperFill.side == "buy")).all()
        exit_ids = [o.id for o in db.scalars(select(StockPaperOrder).where(
            StockPaperOrder.trial_lot_id == lot.id,
            StockPaperOrder.side == "sell")).all()]
        sold = sum((f.quantity for f in db.scalars(select(StockPaperFill).where(
            StockPaperFill.order_id.in_(exit_ids), StockPaperFill.side == "sell")).all()), Decimal("0")) if exit_ids else Decimal("0")
        remaining = max(Decimal("0"), sum((f.quantity for f in entries), Decimal("0")) - sold)
        if remaining <= 0:
            continue
        reference = db.scalar(select(IntradayBar).where(
            IntradayBar.symbol == lot.symbol, IntradayBar.timeframe == "1m",
            IntradayBar.provider == "alpaca", IntradayBar.exchange_timestamp.is_not(None),
            IntradayBar.opened_at >= bounds[0], IntradayBar.opened_at < now
        ).order_by(IntradayBar.opened_at.desc()))
        if not reference:
            continue
        attempts = db.scalars(select(StockPaperOrder).where(
            StockPaperOrder.trial_lot_id == lot.id,
            StockPaperOrder.side == "sell")).all()
        try:
            close = reserve_stock_paper_order(db, symbol=lot.symbol, side="sell",
                quantity=remaining, reference_price=reference.close,
                idempotency_key=f"trial-exit:{lot.id}:attempt-{len(attempts) + 1}",
                source="manual_control_room")
            close.strategy_id = trial.strategy_id
            close.trial_lot_id = lot.id
            db.flush()
            lot.exit_order_id, lot.exit_status = close.id, "reserved"
            dispatch_reserved_order(db, close.id)
        except StockPaperError:
            if trial.status != "stopped":
                if trial.status == "running":
                    trial.status = "paused"
            trial.pause_reason = "exit_dispatch_uncertain"
            return {"status": trial.status, "trial_id": trial_id, "executed": 0}
    if drawdown_blocked:
        return {"status": trial.status, "trial_id": trial_id, "executed": 0,
                "reason": trial.pause_reason}
    executed = 0
    pending = db.scalars(select(StockPaperTrialDecision).where(
        StockPaperTrialDecision.trial_id == trial_id,
        StockPaperTrialDecision.action == "buy",
        StockPaperTrialDecision.qualifying.is_(True),
        StockPaperTrialDecision.order_id.is_(None),
    ).order_by(StockPaperTrialDecision.decision_timestamp.asc())).all() if trial.status == "running" else []
    for decision in pending:
        if not previous_bounds or decision.bar_timestamp.date() != previous_bounds[0].date():
            decision.action, decision.qualifying, decision.rejection_reason = "reject", False, "stale_decision_expired"
            continue
        symbol = decision.symbol
        try:
            feed = feed_status(db, symbol, now=now)
            if feed.get("status") != "ready":
                trial.status, trial.pause_reason = "paused", f"fresh_complete_feed_required:{symbol}"
                break
            reference = db.scalar(select(IntradayBar).where(
                IntradayBar.symbol == symbol, IntradayBar.timeframe == "1m",
                IntradayBar.provider == "alpaca",
                IntradayBar.opened_at >= bounds[0], IntradayBar.opened_at < now,
                IntradayBar.exchange_timestamp.is_not(None),
            ).order_by(IntradayBar.opened_at.desc()))
            if not reference:
                continue
            # Fixed policy: trial remaining, global symbol/equity cap, and
            # 0.25% equity risk at the declared 2% stop.
            positions = db.query(StockPaperPosition).filter_by(account_id=account.id).all()
            total_exposure = sum((p.market_value or Decimal("0") for p in positions), Decimal("0"))
            symbol_exposure = sum((p.market_value or Decimal("0") for p in positions if p.symbol == symbol), Decimal("0"))
            trial_exposure = _trial_allocated_notional(db, trial)
            remaining = max(Decimal("0"), Decimal("10000") - trial_exposure)
            symbol_cap = max(Decimal("0"), account.equity * Decimal("0.10") - symbol_exposure)
            trial_stop_risk_cap = account.equity * Decimal("0.0025") / Decimal("0.02")
            ledger_risk_cap = account.equity * Decimal(str(DEFAULT_RISK_RULES["max_risk_per_trade"]))
            risk_cap = min(trial_stop_risk_cap, ledger_risk_cap)
            notional = min(remaining, symbol_cap, risk_cap,
                           max(Decimal("0"), account.equity * Decimal("0.10") - total_exposure))
            quantity = (notional / reference.close).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
            if quantity <= 0:
                decision.action, decision.qualifying, decision.rejection_reason = "reject", False, "position_size_zero"
                continue
            if not trial.strategy_id:
                _evidence_allows_trade(db, trial, validate_trial_artifact(db, trial), now)
            signal = StrategySignal(strategy_id=trial.strategy_id, symbol=symbol,
                signal_time=now, action="BUY",
                probability_up=Decimal(str(decision.lineage.get("probability") or 0)),
                probability_down=Decimal(str(1 - float(decision.lineage.get("probability") or 0))),
                confidence=Decimal(str(decision.lineage.get("probability") or 0)),
                reason="task12_forward_trial", features=decision.lineage.get("features"))
            db.add(signal)
            db.flush()
            order = reserve_stock_paper_order(db, symbol=symbol, side="buy", quantity=quantity,
                reference_price=reference.close,
                idempotency_key=f"trial:{trial.id}:entry:{decision.id}",
                source="manual_control_room", signal_id=signal.id)
            order.strategy_id = trial.strategy_id
            decision.order_id = order.id
            db.add(StockPaperTrialLot(
                trial_id=trial.id, symbol=symbol, entry_decision_id=decision.id,
                entry_order_id=order.id, quantity=quantity,
                entry_session=now.date().isoformat(), planned_horizon_sessions=5,
                stop_fraction=Decimal("0.02"),
            ))
            db.flush()
            dispatch_reserved_order(db, order.id)
            executed += 1
        except StockPaperError as exc:
            decision.action, decision.qualifying, decision.rejection_reason = "reject", False, str(exc)
    return {"status": trial.status, "trial_id": trial_id, "executed": executed, "paper_only": True}

def evaluate_trial(db: Session, trial_id: str) -> StockPaperTrialMetric:
    """Persist conservative broker-attributed metrics; never treats unknown fees as zero."""
    trial = db.get(StockPaperTrial, trial_id)
    if not trial: raise StockTrainingError("Trial not found")
    _sync_trial_lot_state(db, trial)
    decisions = db.scalars(select(StockPaperTrialDecision).where(StockPaperTrialDecision.trial_id == trial_id)).all()
    lots = db.scalars(select(StockPaperTrialLot).where(StockPaperTrialLot.trial_id == trial_id)).all()
    closed, wins, gross, net, fills_seen = 0, 0, Decimal("0"), Decimal("0"), 0
    costs_known = True
    for lot in lots:
        entries = db.scalars(select(StockPaperFill).where(
            StockPaperFill.order_id == lot.entry_order_id, StockPaperFill.side == "buy"
        )).all()
        exit_ids = [o.id for o in db.scalars(select(StockPaperOrder).where(
            StockPaperOrder.trial_lot_id == lot.id,
            StockPaperOrder.side == "sell")).all()]
        exits = db.scalars(select(StockPaperFill).where(
            StockPaperFill.order_id.in_(exit_ids), StockPaperFill.side == "sell"
        )).all() if exit_ids else []
        fills_seen += len(entries) + len(exits)
        if any(not f.cost_known or f.fee is None for f in entries + exits):
            costs_known = False
        if not entries or not exits:
            continue
        entry_qty = sum((f.quantity for f in entries), Decimal("0"))
        exit_qty = sum((f.quantity for f in exits), Decimal("0"))
        matched = min(entry_qty, exit_qty)
        if matched <= 0:
            continue
        entry_price = sum((f.quantity * f.price for f in entries), Decimal("0")) / entry_qty
        exit_price = sum((f.quantity * f.price for f in exits), Decimal("0")) / exit_qty
        outcome = matched * (exit_price - entry_price)
        gross += outcome
        fully_closed = exit_qty >= entry_qty
        wins += int(fully_closed and outcome > 0)
        closed += int(fully_closed)
        if costs_known:
            net += outcome - sum((f.fee or Decimal("0") for f in entries + exits), Decimal("0"))
    if not fills_seen:
        costs_known = False
    provisional_gross = gross
    exact_outcomes = sorted(_lot_outcomes(db, lots), key=lambda x: x["close_at"])
    closed = len(exact_outcomes)
    wins = sum(int(x["outcome"] > 0) for x in exact_outcomes)
    gross = sum((x["outcome"] for x in exact_outcomes), Decimal("0"))
    closed_costs_known = all(x["costs_known"] for x in exact_outcomes)
    if closed and costs_known and closed_costs_known:
        net = sum((x["net_outcome"] for x in exact_outcomes), Decimal("0"))
    else:
        net = Decimal("0")
    win_rate = (Decimal(wins) / Decimal(closed)) if closed and costs_known else None
    expectancy = (net / Decimal(closed)) if closed and costs_known and closed_costs_known else None
    as_of = _now()
    accuracy_report, accuracy_evidence = persist_accuracy_evidence(db, trial, as_of=as_of)
    session_evidence = build_trial_session_evidence(db, trial, as_of=as_of)
    aggregates = session_evidence["aggregates"]
    universe = trial.lineage.get("universe", [])
    dates = [datetime.fromisoformat(value).date() for value in session_evidence["window"]["session_dates"]]
    coverage = Decimal(aggregates["decision_coverage"])
    sessions = session_evidence["window"]["elapsed_regular_sessions"]
    if sessions >= int(trial.policy["regular_sessions"]):
        if trial.status in {"running", "paused", "blocked"}:
            trial.status, trial.stopped_at = "stopped", trial.stopped_at or as_of
        # Freeze new decisions at the boundary and turn remaining lots into
        # durable risk-reducing intents; dispatch is still regular-session gated.
        for decision in decisions:
            if decision.order_id is None and decision.action == "buy":
                decision.action, decision.qualifying, decision.rejection_reason = "reject", False, "trial_window_complete"
        for lot in _sync_trial_lot_state(db, trial):
            if _trial_has_managed_exposure(db, trial) and lot.exit_status != "closed":
                lot.exit_reason = lot.exit_reason or "trial_window_complete"
                lot.exit_decided_at = lot.exit_decided_at or as_of
        if not _trial_has_managed_exposure(db, trial):
            trial.status = "completed"
    benchmark_returns = []
    if dates and universe:
        for symbol in universe:
            first = db.scalar(select(IntradayBar.close).where(
                IntradayBar.symbol == symbol, IntradayBar.timeframe == "1m",
                IntradayBar.provider == "alpaca",
                IntradayBar.opened_at >= datetime.combine(dates[0], datetime.min.time(), tzinfo=timezone.utc),
                IntradayBar.opened_at < datetime.combine(dates[0] + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc),
            ).order_by(IntradayBar.opened_at.asc()))
            last = db.scalar(select(IntradayBar.close).where(
                IntradayBar.symbol == symbol, IntradayBar.timeframe == "1m",
                IntradayBar.provider == "alpaca",
                IntradayBar.opened_at >= datetime.combine(dates[-1], datetime.min.time(), tzinfo=timezone.utc),
                IntradayBar.opened_at < datetime.combine(dates[-1] + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc),
            ).order_by(IntradayBar.opened_at.desc()))
            if first and last and first > 0:
                benchmark_returns.append((last - first) / first)
    benchmark = (sum(benchmark_returns, Decimal("0")) / Decimal(len(benchmark_returns))
                 if len(benchmark_returns) == len(universe) else None)
    account = db.query(StockPaperAccount).filter_by(broker="alpaca_paper").one_or_none()
    account_drawdown = ((trial.peak_equity - account.equity) / trial.peak_equity
                        if account and trial.peak_equity and trial.peak_equity > 0 else None)
    trial_drawdown = _trial_equity_curve_max_drawdown(db, trial, as_of)
    payload = {"expected_observations": aggregates["expected_decisions"],
               "observed_observations": aggregates["observed_decisions"],
                "observed_sessions": sessions, "elapsed_regular_sessions": sessions,
                "evidenced_sessions": sum(any(symbol["observed_decisions"] for symbol in session["symbols"])
                                         for session in session_evidence["sessions"]),
                "decision_coverage": str(coverage),
                "verified_decision_coverage": aggregates["verified_decision_coverage"],
                "session_evidence": session_evidence,
               "closed_trades": closed, "winning_trades": wins,
               "win_rate": str(win_rate) if win_rate is not None else None,
                "gross_pnl": str(gross) if closed else None,
                "provisional_gross_pnl": str(provisional_gross) if provisional_gross else None,
                "gross_pnl_provisional": False if closed else True,
                "net_pnl": str(net) if costs_known and closed and closed_costs_known else None,
               "expectancy": str(expectancy) if expectancy is not None else None,
               "max_drawdown": str(trial_drawdown) if trial_drawdown is not None else None,
               "account_drawdown": str(account_drawdown) if account_drawdown is not None else None,
               "benchmark_buy_hold": str(benchmark) if benchmark is not None else None,
               "costs_known": costs_known,
               "accuracy": accuracy_evidence,
               "accuracy_report_id": accuracy_report.id,
               "accuracy_report_hash": accuracy_report.report_hash}
    sufficient = sessions >= int(trial.policy["regular_sessions"])
    if not sufficient:
        classification = "insufficient" if trial.status in {"stopped", "completed"} else "accumulating"
    elif closed < int(trial.policy["minimum_closed_trades"]) or coverage < Decimal(str(trial.policy["minimum_decision_coverage"])) or not costs_known:
        classification = "insufficient"
    elif net <= 0 or gross <= 0 or payload["benchmark_buy_hold"] is None or payload["max_drawdown"] is None:
        classification = "failing"
    else:
        classification = "passing"
    # The payload is the frozen aggregate snapshot embedded in versioned
    # readiness reports; keep its status aligned with the metric row.
    payload["classification"] = classification
    metric = StockPaperTrialMetric(trial_id=trial_id, as_of=as_of, classification=classification, payload=payload)
    db.add(metric)
    return metric