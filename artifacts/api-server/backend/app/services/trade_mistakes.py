"""Post-trade mistake attribution for closed paper trades.

Every closed forward-paper lot is linked back to the decision that opened it
(``StockPaperTrialLot.entry_decision_id``) and classified into zero or more
mistake categories.  Results are aggregated per strategy so the learning loop
can see *why* a strategy loses, not only *that* it loses.

Safety contract:
* Read-only.  Nothing here mutates strategy state, bindings, or orders.
* Lessons are proposals.  Each carries ``auto_apply: False`` and
  ``requires_fresh_holdout_and_forward_trial: True``: a suggested change must
  be trained into a new challenger and pass the governed one-use holdout and a
  completed forward paper trial before it can affect paper trading.
* Attribution uses information observed after the trade closed (excursions,
  exit fills).  It is hindsight evaluation and must never be fed back as a
  feature into decisions dated before the outcome was observed.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import IntradayBar, StockPaperTrial, StockPaperTrialDecision
from app.models.stock_paper import StockPaperFill, StockPaperOrder, StockPaperTrialLot


CLASSIFIER_VERSION = "trade-mistakes-v1"

# Forward-trial policy: 0.25% of equity at risk at the declared 2% stop.
DEFAULT_RISK_PER_TRADE = 0.0025
OVERSIZE_TOLERANCE = 1.5
STOP_SLIPPAGE_TOLERANCE = 1.5
COST_SHARE_LIMIT = 0.5
OVERCONFIDENT_PROBABILITY = 0.60

# Priority order used to pick a single primary mistake for a losing trade.
MISTAKE_PRIORITY = (
    "oversized",
    "cost_dominated",
    "regime_mismatch",
    "bad_entry",
    "stop_slippage",
    "premature_stop",
    "gave_back_gains",
    "signal_decay",
    "overconfident_signal",
    "unexplained_loss",
)

LESSONS = {
    "oversized": "Loss exceeded the per-trade risk budget; review sizing against the declared stop before entry.",
    "cost_dominated": "Fees/slippage consumed the gross edge; require expected return to clear round-trip cost by a margin.",
    "regime_mismatch": "Entry regime differed from the regime the strategy was validated in; add or tighten a regime filter.",
    "bad_entry": "Price moved against the entry almost immediately; review entry timing/confirmation features.",
    "stop_slippage": "Exit filled well beyond the declared stop; stops evaluated on completed bars may need gap protection.",
    "premature_stop": "Stopped out, then price recovered within the planned horizon; the stop may be too tight for volatility.",
    "gave_back_gains": "Trade showed a meaningful open gain that was given back; consider a profit-protection exit.",
    "signal_decay": "Edge faded before the planned horizon; consider a shorter horizon or a decay-aware exit.",
    "overconfident_signal": "High-confidence signal lost; check probability calibration for this strategy.",
    "unexplained_loss": "Loss without a distinctive pattern; may be ordinary variance.",
}


def _f(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def classify_trade(trade: dict) -> dict:
    """Classify one closed trade.  Pure and deterministic.

    Required keys: ``entry_price``, ``exit_price``, ``quantity``.
    Optional keys: ``fees`` (None = unknown), ``stop_fraction``, ``exit_reason``
    (``stop``/``horizon``/other), ``max_favorable_excursion`` and
    ``max_adverse_excursion`` (fractions vs entry, MFE >= 0, MAE <= 0),
    ``post_exit_return`` (exit to planned-horizon end), ``probability``,
    ``entry_regime``, ``validated_regimes``, ``equity_at_entry``,
    ``risk_per_trade``.
    """
    entry = _f(trade.get("entry_price"))
    exit_ = _f(trade.get("exit_price"))
    quantity = _f(trade.get("quantity"))
    if not entry or entry <= 0 or exit_ is None or not quantity or quantity <= 0:
        return {
            "classifier_version": CLASSIFIER_VERSION,
            "status": "unclassifiable",
            "reason": "entry price, exit price, and quantity are required",
            "mistakes": [],
            "primary_mistake": None,
        }
    fees = _f(trade.get("fees"))
    costs_known = fees is not None
    gross_pnl = (exit_ - entry) * quantity
    net_pnl = gross_pnl - fees if costs_known else None
    gross_return = exit_ / entry - 1
    net_return = (net_pnl / (entry * quantity)) if net_pnl is not None else None
    effective_pnl = net_pnl if net_pnl is not None else gross_pnl
    effective_return = net_return if net_return is not None else gross_return
    stop = _f(trade.get("stop_fraction")) or 0.02
    exit_reason = str(trade.get("exit_reason") or "").lower()
    mfe = _f(trade.get("max_favorable_excursion"))
    mae = _f(trade.get("max_adverse_excursion"))
    post_exit = _f(trade.get("post_exit_return"))
    probability = _f(trade.get("probability"))
    equity = _f(trade.get("equity_at_entry"))
    risk_budget = _f(trade.get("risk_per_trade")) or DEFAULT_RISK_PER_TRADE

    mistakes: list[str] = []
    losing = effective_pnl <= 0
    if costs_known and gross_pnl > 0 and net_pnl is not None and net_pnl <= 0:
        mistakes.append("cost_dominated")
    elif costs_known and losing and fees > 0 and fees >= COST_SHARE_LIMIT * abs(effective_pnl):
        mistakes.append("cost_dominated")
    if losing:
        if equity and equity > 0 and abs(effective_pnl) / equity > risk_budget * OVERSIZE_TOLERANCE:
            mistakes.append("oversized")
        entry_regime = trade.get("entry_regime")
        validated = trade.get("validated_regimes")
        if entry_regime and validated and str(entry_regime) not in {str(item) for item in validated}:
            mistakes.append("regime_mismatch")
        if mfe is not None and mfe < 0.25 * stop and gross_return < 0:
            mistakes.append("bad_entry")
        if exit_reason == "stop" and gross_return < -stop * STOP_SLIPPAGE_TOLERANCE:
            mistakes.append("stop_slippage")
        if exit_reason == "stop" and post_exit is not None and post_exit >= stop:
            mistakes.append("premature_stop")
        if mfe is not None and mfe >= 2 * stop:
            mistakes.append("gave_back_gains")
        elif exit_reason == "horizon" and mfe is not None and mfe > 0:
            mistakes.append("signal_decay")
        if probability is not None and probability >= OVERCONFIDENT_PROBABILITY:
            mistakes.append("overconfident_signal")
        if not mistakes:
            mistakes.append("unexplained_loss")
    primary = next((name for name in MISTAKE_PRIORITY if name in mistakes), None)
    return {
        "classifier_version": CLASSIFIER_VERSION,
        "status": "loss" if losing else "win",
        "mistakes": mistakes,
        "primary_mistake": primary,
        "gross_pnl": round(gross_pnl, 8),
        "net_pnl": round(net_pnl, 8) if net_pnl is not None else None,
        "gross_return": round(gross_return, 8),
        "net_return": round(net_return, 8) if net_return is not None else None,
        "effective_return": round(effective_return, 8),
        "costs_known": costs_known,
        "max_favorable_excursion": mfe,
        "max_adverse_excursion": mae,
        "lesson": LESSONS.get(primary) if primary else None,
    }


def aggregate_mistakes(rows: Iterable[dict]) -> dict:
    """Aggregate classified trades into per-strategy mistake profiles."""
    by_strategy: dict[str, dict] = {}
    for row in rows:
        key = str(row.get("strategy_id"))
        bucket = by_strategy.setdefault(key, {
            "strategy_id": row.get("strategy_id"),
            "closed_trades": 0, "wins": 0, "losses": 0,
            "sum_effective_return": 0.0, "mistake_counts": Counter(), "primary_counts": Counter(),
        })
        classification = row.get("classification") or {}
        if classification.get("status") not in {"win", "loss"}:
            continue
        bucket["closed_trades"] += 1
        bucket["wins"] += int(classification["status"] == "win")
        bucket["losses"] += int(classification["status"] == "loss")
        bucket["sum_effective_return"] += float(classification.get("effective_return") or 0)
        bucket["mistake_counts"].update(classification.get("mistakes") or [])
        if classification.get("primary_mistake"):
            bucket["primary_counts"][classification["primary_mistake"]] += 1
    strategies = []
    for bucket in by_strategy.values():
        closed = bucket["closed_trades"]
        losses = bucket["losses"]
        primary = bucket["primary_counts"].most_common()
        top = primary[0][0] if primary else None
        strategies.append({
            "strategy_id": bucket["strategy_id"],
            "closed_trades": closed,
            "wins": bucket["wins"],
            "losses": losses,
            "avg_effective_return": round(bucket["sum_effective_return"] / closed, 8) if closed else None,
            "mistake_counts": dict(sorted(bucket["mistake_counts"].items())),
            "primary_mistake_counts": dict(sorted(bucket["primary_counts"].items())),
            "primary_mistake_share_of_losses": {
                name: round(count / losses, 6) for name, count in sorted(bucket["primary_counts"].items())
            } if losses else {},
            "top_mistake": top,
            "lessons": [
                {
                    "mistake": name,
                    "losses": count,
                    "proposal": LESSONS[name],
                    "auto_apply": False,
                    "requires_fresh_holdout_and_forward_trial": True,
                }
                for name, count in primary
            ],
        })
    strategies.sort(key=lambda item: (-(item["losses"] or 0), str(item["strategy_id"])))
    return {"classifier_version": CLASSIFIER_VERSION, "strategies": strategies,
            "paper_only": True, "live_authorized": False, "mutates_strategy_state": False}


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _excursions(db: Session, symbol: str, start: datetime, end: datetime, entry: float) -> tuple[Optional[float], Optional[float]]:
    bars = db.scalars(select(IntradayBar).where(
        IntradayBar.symbol == symbol, IntradayBar.timeframe == "1m",
        IntradayBar.opened_at >= start, IntradayBar.opened_at <= end,
    )).all()
    if not bars or entry <= 0:
        return None, None
    high = max(float(bar.high) for bar in bars)
    low = min(float(bar.low) for bar in bars)
    return max(0.0, high / entry - 1), min(0.0, low / entry - 1)


def trial_lot_trades(db: Session, trial: StockPaperTrial) -> list[dict]:
    """Build classifier inputs for every fully closed lot of a trial."""
    lots = db.scalars(select(StockPaperTrialLot).where(
        StockPaperTrialLot.trial_id == trial.id).order_by(StockPaperTrialLot.id)).all()
    rows: list[dict] = []
    for lot in lots:
        entries = db.scalars(select(StockPaperFill).where(
            StockPaperFill.order_id == lot.entry_order_id, StockPaperFill.side == "buy")).all() if lot.entry_order_id else []
        exit_ids = [order.id for order in db.scalars(select(StockPaperOrder).where(
            StockPaperOrder.trial_lot_id == lot.id, StockPaperOrder.side == "sell")).all()]
        exits = db.scalars(select(StockPaperFill).where(
            StockPaperFill.order_id.in_(exit_ids), StockPaperFill.side == "sell")).all() if exit_ids else []
        entry_qty = sum((fill.quantity for fill in entries), Decimal("0"))
        exit_qty = sum((fill.quantity for fill in exits), Decimal("0"))
        if entry_qty <= 0 or exit_qty < entry_qty:
            continue
        entry_price = sum((fill.quantity * fill.price for fill in entries), Decimal("0")) / entry_qty
        exit_price = sum((fill.quantity * fill.price for fill in exits), Decimal("0")) / exit_qty
        costs_known = all(fill.cost_known and fill.fee is not None for fill in entries + exits)
        fees = sum((fill.fee or Decimal("0") for fill in entries + exits), Decimal("0")) if costs_known else None
        entered_at = min(_aware(fill.filled_at) for fill in entries)
        exited_at = max(_aware(fill.filled_at) for fill in exits)
        mfe, mae = _excursions(db, lot.symbol, entered_at, exited_at, float(entry_price))
        decision = db.get(StockPaperTrialDecision, lot.entry_decision_id)
        lineage = (decision.lineage if decision else None) or {}
        trade = {
            "lot_id": lot.id,
            "trial_id": trial.id,
            "strategy_id": trial.strategy_id,
            "entry_decision_id": lot.entry_decision_id,
            "symbol": lot.symbol,
            "entered_at": entered_at.isoformat(),
            "exited_at": exited_at.isoformat(),
            "entry_price": float(entry_price),
            "exit_price": float(exit_price),
            "quantity": float(entry_qty),
            "fees": float(fees) if fees is not None else None,
            "stop_fraction": float(lot.stop_fraction or Decimal("0.02")),
            "planned_horizon_sessions": lot.planned_horizon_sessions,
            "exit_reason": lot.exit_reason,
            "max_favorable_excursion": mfe,
            "max_adverse_excursion": mae,
            "probability": _f(lineage.get("probability")),
            "entry_regime": lineage.get("market_regime"),
            "validated_regimes": lineage.get("validated_regimes"),
            "equity_at_entry": float(trial.baseline_equity) if trial.baseline_equity else None,
            "risk_per_trade": _f((trial.policy or {}).get("max_risk_per_trade")),
        }
        rows.append(trade | {"classification": classify_trade(trade)})
    return rows


def trial_mistake_report(db: Session, trial_id: str) -> dict:
    trial = db.get(StockPaperTrial, trial_id)
    if trial is None:
        raise ValueError("Trial not found")
    rows = trial_lot_trades(db, trial)
    return {
        "trial_id": trial.id,
        "strategy_id": trial.strategy_id,
        "trades": rows,
        "summary": aggregate_mistakes(rows),
        "paper_only": True,
        "live_authorized": False,
        "mutates_strategy_state": False,
    }


def strategy_mistake_report(db: Session, *, limit_trials: int = 50) -> dict:
    trials = db.scalars(select(StockPaperTrial).where(StockPaperTrial.strategy_id.is_not(None))
                        .order_by(StockPaperTrial.created_at.desc()).limit(limit_trials)).all()
    rows: list[dict] = []
    for trial in trials:
        rows.extend(trial_lot_trades(db, trial))
    return aggregate_mistakes(rows) | {"trials_considered": len(trials), "trades_considered": len(rows)}
