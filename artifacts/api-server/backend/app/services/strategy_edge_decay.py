"""Rolling out-of-sample edge-decay detection and paper-only demotion.

The forward paper trial is the only out-of-sample evidence that is broker
observed.  This module looks at the most recent closed, trial-owned lots of a
strategy and pauses it in paper when the rolling mean (net of known costs)
is below the baseline with one-sided statistical confidence.

Safety contract:
* It can only *remove* trading permission: it sets ``Strategy.current_status``
  to ``paused`` and the strategy's ``StockPaperStrategyEvidence.status`` to
  ``suspended``.  The stock-paper ledger only accepts ``approved`` evidence for
  buys, so suspension blocks new entries.  Exits are untouched.
* There is no automatic un-pause.  A suspended strategy can only trade again
  through a new governed learning cycle (fresh one-use holdout + completed
  forward paper trial), which creates a new trial-owned strategy.
* It never touches bindings, kill switches, live settings, or orders.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import StockPaperTrial, Strategy
from app.models.stock_paper import StockPaperStrategyEvidence, StockPaperTrialLot
from app.services.audit import write_audit_log
from app.services.notifications import create_notification


DECAY_POLICY = {
    "version": "edge-decay-v1",
    "window": 20,
    "min_samples": 8,
    "baseline_return": 0.0,
    "confidence": 0.95,
}
SUSPENDED_EVIDENCE_STATUS = "suspended"
PAUSED_STATUSES = {"paused", "retired"}

# One-sided 95% Student-t critical values by degrees of freedom.
_T95 = {
    1: 6.314, 2: 2.920, 3: 2.353, 4: 2.132, 5: 2.015, 6: 1.943, 7: 1.895, 8: 1.860,
    9: 1.833, 10: 1.812, 11: 1.796, 12: 1.782, 13: 1.771, 14: 1.761, 15: 1.753,
    16: 1.746, 17: 1.740, 18: 1.734, 19: 1.729, 20: 1.725, 21: 1.721, 22: 1.717,
    23: 1.714, 24: 1.711, 25: 1.708, 26: 1.706, 27: 1.703, 28: 1.701, 29: 1.699, 30: 1.697,
}


def _t_critical(df: int) -> float:
    if df <= 0:
        return float("inf")
    return _T95.get(df, 1.645 if df > 120 else 1.66)


def assess_edge_decay(returns: Iterable[float], *, policy: Optional[dict] = None) -> dict:
    """Classify the rolling edge.  Pure and deterministic.

    ``returns`` are per-trade fractional returns in chronological order.
    ``decayed`` means the one-sided 95% upper confidence bound of the rolling
    mean is below the baseline, i.e. the strategy is losing with confidence.
    """
    policy = DECAY_POLICY | (policy or {})
    if float(policy["confidence"]) != 0.95:
        raise ValueError("Only the 95% one-sided confidence level is supported")
    values = [float(item) for item in returns if item is not None and math.isfinite(float(item))]
    window = values[-int(policy["window"]):]
    n = len(window)
    base = {
        "policy": policy,
        "samples": n,
        "total_closed": len(values),
        "baseline_return": float(policy["baseline_return"]),
    }
    if n < int(policy["min_samples"]):
        return base | {"status": "insufficient", "mean_return": None, "upper_confidence_bound": None,
                       "reason": f"{n} closed trade(s) in window; {policy['min_samples']} required"}
    mean = sum(window) / n
    variance = sum((item - mean) ** 2 for item in window) / (n - 1)
    stderr = math.sqrt(variance / n)
    upper = mean + _t_critical(n - 1) * stderr
    baseline = float(policy["baseline_return"])
    if upper < baseline:
        status, reason = "decayed", "rolling out-of-sample mean is below baseline with 95% confidence"
    elif mean < baseline:
        status, reason = "decaying", "rolling mean is below baseline but not yet statistically confident"
    else:
        status, reason = "healthy", None
    return base | {"status": status, "mean_return": round(mean, 8), "stderr": round(stderr, 8),
                   "upper_confidence_bound": round(upper, 8), "reason": reason}


def lot_returns(outcomes: list[dict]) -> list[float]:
    """Per-lot fractional return (net when costs are known) in close order."""
    result = []
    for outcome in sorted(outcomes, key=lambda item: item["close_at"]):
        basis = outcome.get("entry_cost")
        if not basis or Decimal(str(basis)) <= 0:
            continue
        pnl = outcome.get("net_outcome") if outcome.get("net_outcome") is not None else outcome.get("outcome")
        result.append(float(Decimal(str(pnl)) / Decimal(str(basis))))
    return result


def strategy_is_suspended(db: Session, strategy_id: Optional[int]) -> Optional[str]:
    """Return a blocking reason when a strategy may not open new paper entries."""
    if not strategy_id:
        return None
    strategy = db.get(Strategy, strategy_id)
    if strategy is not None and strategy.current_status in PAUSED_STATUSES:
        return "strategy_paused"
    evidence = db.scalar(select(StockPaperStrategyEvidence).where(
        StockPaperStrategyEvidence.strategy_id == strategy_id))
    if evidence is not None and evidence.status != "approved":
        return "strategy_evidence_suspended"
    return None


def suspend_strategy_for_decay(db: Session, strategy_id: int, assessment: dict, *, now: datetime,
                               source: str) -> dict:
    """Pause a strategy in paper and suspend its execution evidence.  Idempotent."""
    strategy = db.get(Strategy, strategy_id)
    evidence = db.scalar(select(StockPaperStrategyEvidence).where(
        StockPaperStrategyEvidence.strategy_id == strategy_id))
    old_status = strategy.current_status if strategy else None
    changed = False
    if strategy is not None and strategy.current_status not in PAUSED_STATUSES:
        strategy.current_status = "paused"
        changed = True
    if evidence is not None and evidence.status != SUSPENDED_EVIDENCE_STATUS:
        evidence.status = SUSPENDED_EVIDENCE_STATUS
        evidence.provenance = {
            **(evidence.provenance or {}),
            "suspended_at": now.isoformat(),
            "suspended_by": source,
            "suspension_policy": DECAY_POLICY["version"],
        }
        changed = True
    if changed:
        payload = {
            "old_status": old_status,
            "new_status": strategy.current_status if strategy else None,
            "assessment": assessment,
            "paper_only": True,
            "live_authorized": False,
            "reactivation": "requires a new governed cycle with fresh holdout and forward paper trial",
        }
        write_audit_log(db, event_type="strategy_edge_decay", entity_type="strategy", entity_id=strategy_id,
                        action="pause", status="complete",
                        message="Strategy paused in paper after statistically confident edge decay.",
                        payload=payload)
        create_notification(db, category="strategy_governance", severity="warning",
                            source="strategy_edge_decay", title="Strategy paused for edge decay",
                            message=(f"{strategy.name if strategy else strategy_id}: rolling mean "
                                     f"{assessment.get('mean_return')} (UCB {assessment.get('upper_confidence_bound')}) "
                                     "is below baseline."),
                            entity_type="strategy", entity_id=strategy_id, payload=payload)
    return {"strategy_id": strategy_id, "changed": changed, "old_status": old_status,
            "new_status": strategy.current_status if strategy else None,
            "evidence_status": evidence.status if evidence else None}


def review_strategy_edge(db: Session, strategy_id: int, outcomes: list[dict], *, now: Optional[datetime] = None,
                         source: str = "strategy_edge_decay") -> dict:
    now = now or datetime.now(timezone.utc)
    assessment = assess_edge_decay(lot_returns(outcomes))
    action = None
    if assessment["status"] == "decayed":
        action = suspend_strategy_for_decay(db, strategy_id, assessment, now=now, source=source)
    return {"strategy_id": strategy_id, "assessment": assessment, "action": action}


def _outcomes_by_strategy(db: Session) -> dict[int, list]:
    from app.services.stock_forward_trial import _lot_outcomes

    trials = db.scalars(select(StockPaperTrial).where(StockPaperTrial.strategy_id.is_not(None))).all()
    by_strategy: dict[int, list] = {}
    for trial in trials:
        lots = db.scalars(select(StockPaperTrialLot).where(StockPaperTrialLot.trial_id == trial.id)).all()
        by_strategy.setdefault(trial.strategy_id, []).extend(_lot_outcomes(db, lots))
    return by_strategy


def edge_decay_projection(db: Session) -> dict:
    """Read-only view of every trial strategy's rolling edge; never mutates."""
    rows = []
    for strategy_id, outcomes in sorted(_outcomes_by_strategy(db).items()):
        rows.append({"strategy_id": strategy_id, "assessment": assess_edge_decay(lot_returns(outcomes)),
                     "blocked_reason": strategy_is_suspended(db, strategy_id)})
    return {"policy": DECAY_POLICY, "strategies": rows, "paper_only": True, "live_authorized": False}


def run_edge_decay_review(db: Session, *, now: Optional[datetime] = None) -> dict:
    """Review every trial-owned strategy; pause those with confident decay."""
    now = now or datetime.now(timezone.utc)
    by_strategy = _outcomes_by_strategy(db)
    reviews = [review_strategy_edge(db, strategy_id, outcomes, now=now, source="strategy_edge_decay_job")
               for strategy_id, outcomes in sorted(by_strategy.items())]
    paused = [row["strategy_id"] for row in reviews if row["action"] and row["action"]["changed"]]
    db.commit()
    return {"status": "complete", "job": "strategy_edge_decay_job", "reviewed": len(reviews),
            "paused": paused, "reviews": reviews, "paper_only": True, "live_authorized": False}
