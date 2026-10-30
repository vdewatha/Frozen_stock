"""Per-strategy meta-label mode: automatic SHADOW -> ENFORCE and back.

Evidence comes from attributed autotrader round trips whose entry signal
carried a meta-label record.  A trade is a positive outcome when its
effective (net when known) return is above zero.

* In SHADOW the filter does not block, so both ``take`` and ``skip`` BUYs
  were traded and precision/recall are measurable:
  precision = wins among ``take`` / ``take`` trades,
  recall = wins marked ``take`` / all wins,
  lift = precision - base win rate.
  A strategy is promoted to ENFORCE once the shadow sample, precision,
  recall and lift all clear the configured bar.
* In ENFORCE only ``take`` BUYs trade.  Their win rate (enforced precision)
  is monitored; below the demotion bar over the minimum sample the strategy
  falls back to SHADOW.

Every transition is recorded as an immutable event with its evidence.  The
global ``paper_meta_label_mode`` still wins: ``off`` disables the filter and
``enforce`` enforces every strategy.  Paper-only; live trading is untouched.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.loss_feedback import StockMetaLabelMode, StockMetaLabelModeEvent
from app.services.audit import write_audit_log

UTC = timezone.utc
MODES = ("shadow", "enforce")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def shadow_metrics(samples: list[dict]) -> dict:
    """Precision/recall of shadow ``take`` decisions against realized wins."""
    scored = [row for row in samples if row.get("decision") in {"take", "skip"}]
    take = [row for row in scored if row["decision"] == "take"]
    wins = [row for row in scored if row["win"]]
    true_pos = sum(1 for row in take if row["win"])
    precision = true_pos / len(take) if take else None
    recall = true_pos / len(wins) if wins else None
    base = len(wins) / len(scored) if scored else None
    return {
        "samples": len(scored), "take": len(take), "skip": len(scored) - len(take), "wins": len(wins),
        "true_positives": true_pos,
        "precision": round(precision, 6) if precision is not None else None,
        "recall": round(recall, 6) if recall is not None else None,
        "base_win_rate": round(base, 6) if base is not None else None,
        "lift": round(precision - base, 6) if precision is not None and base is not None else None,
    }


def enforced_metrics(samples: list[dict]) -> dict:
    taken = [row for row in samples if row.get("decision") == "take"]
    wins = sum(1 for row in taken if row["win"])
    return {"samples": len(taken), "wins": wins,
            "precision": round(wins / len(taken), 6) if taken else None}


def promotion_check(metrics: dict) -> tuple[bool, str]:
    bar = {
        "min_samples": settings.meta_label_enforce_min_samples,
        "min_precision": settings.meta_label_enforce_min_precision,
        "min_recall": settings.meta_label_enforce_min_recall,
        "min_lift": settings.meta_label_enforce_min_lift,
    }
    if metrics["samples"] < bar["min_samples"]:
        return False, f"shadow sample {metrics['samples']} below minimum {bar['min_samples']}"
    if metrics["precision"] is None or metrics["precision"] < bar["min_precision"]:
        return False, f"shadow precision {metrics['precision']} below {bar['min_precision']}"
    if metrics["recall"] is None or metrics["recall"] < bar["min_recall"]:
        return False, f"shadow recall {metrics['recall']} below {bar['min_recall']}"
    if metrics["lift"] is None or metrics["lift"] < bar["min_lift"]:
        return False, f"shadow lift {metrics['lift']} below {bar['min_lift']}"
    return True, (f"shadow precision {metrics['precision']} / recall {metrics['recall']} / lift {metrics['lift']} "
                  f"over {metrics['samples']} trades cleared the enforce bar")


def demotion_check(metrics: dict) -> tuple[bool, str]:
    minimum = settings.meta_label_demote_min_samples
    if metrics["samples"] < minimum:
        return False, f"enforced sample {metrics['samples']} below review minimum {minimum}"
    if metrics["precision"] is not None and metrics["precision"] < settings.meta_label_demote_precision:
        return True, (f"enforced precision {metrics['precision']} over {metrics['samples']} trades fell below "
                      f"{settings.meta_label_demote_precision}")
    return False, f"enforced precision {metrics['precision']} holds"


def _samples(db: Session, strategy_type: str, mode: str, since: Optional[datetime]) -> list[dict]:
    from app.services.trade_attribution import current_attributions

    rows = current_attributions(db, sources=["scheduled_round_trip"], strategy_types=[strategy_type])
    out = []
    for row in rows:
        meta = (row.detail or {}).get("meta_label") or {}
        if meta.get("mode") != mode:
            continue
        if since is not None and _utc(row.opened_at) < since:
            continue
        effective = row.net_return if row.net_return is not None else row.gross_return
        out.append({"decision": meta.get("decision"), "win": float(effective) > 0, "attribution_id": row.id})
    return out


def _state(db: Session, strategy_type: str, now: datetime) -> StockMetaLabelMode:
    row = db.scalar(select(StockMetaLabelMode).where(StockMetaLabelMode.strategy_type == strategy_type))
    if row is None:
        row = StockMetaLabelMode(strategy_type=strategy_type, mode="shadow", changed_at=now, evidence={})
        db.add(row)
        db.flush()
    return row


def _transition(db: Session, row: StockMetaLabelMode, to_mode: str, reason: str, evidence: dict, now: datetime) -> None:
    db.add(StockMetaLabelModeEvent(strategy_type=row.strategy_type, from_mode=row.mode, to_mode=to_mode,
                                   reason=reason, evidence=evidence))
    write_audit_log(db, event_type="meta_label_mode", action=f"{row.mode}_to_{to_mode}", status="complete",
                    message=f"{row.strategy_type}: {reason}", entity_type="strategy_type",
                    entity_id=None, payload={"strategy_type": row.strategy_type, "paper_only": True, **evidence})
    row.mode = to_mode
    row.changed_at = now
    row.evidence = evidence | {"reason": reason}


def review_meta_label_modes(db: Session, strategy_types: list[str], *, now: Optional[datetime] = None) -> list[dict]:
    """Evaluate promotion/demotion for each strategy type (paper only)."""
    now = now or datetime.now(UTC)
    out = []
    for strategy_type in sorted(set(strategy_types)):
        row = _state(db, strategy_type, now)
        if row.mode == "shadow":
            # After a demotion, promotion needs a fresh shadow sample: trades
            # opened before the return to shadow cannot re-qualify enforcement.
            returned = db.scalar(select(StockMetaLabelModeEvent.id).where(
                StockMetaLabelModeEvent.strategy_type == strategy_type,
                StockMetaLabelModeEvent.to_mode == "shadow").limit(1))
            since = _utc(row.changed_at) if returned is not None and row.changed_at else None
            metrics = shadow_metrics(_samples(db, strategy_type, "shadow", since))
            ok, reason = promotion_check(metrics)
            evidence = {"shadow": metrics, "reviewed_at": now.isoformat()}
            if ok and settings.meta_label_auto_enforce_enabled:
                _transition(db, row, "enforce", reason, evidence, now)
            else:
                row.evidence = evidence | {"reason": reason}
        else:
            metrics = enforced_metrics(_samples(db, strategy_type, "enforce", _utc(row.changed_at)))
            demote, reason = demotion_check(metrics)
            evidence = {"enforced": metrics, "promotion_evidence": (row.evidence or {}).get("shadow"),
                        "reviewed_at": now.isoformat()}
            if demote or not settings.meta_label_auto_enforce_enabled:
                _transition(db, row, "shadow", reason if demote else "auto-enforce disabled by configuration",
                            evidence, now)
            else:
                row.evidence = {**(row.evidence or {}), "enforced": metrics, "reviewed_at": now.isoformat(),
                                "monitor_reason": reason}
        out.append(mode_out(row))
    db.flush()
    return out


def enforced_strategy_types(db: Session) -> set[str]:
    if not settings.meta_label_auto_enforce_enabled:
        return set()
    return set(db.scalars(select(StockMetaLabelMode.strategy_type).where(StockMetaLabelMode.mode == "enforce")).all())


def mode_out(row: StockMetaLabelMode) -> dict:
    return {"strategy_type": row.strategy_type, "mode": row.mode,
            "changed_at": _utc(row.changed_at).isoformat() if row.changed_at else None,
            "evidence": row.evidence or {}}


def meta_label_mode_projection(db: Session, *, events_limit: int = 50) -> dict:
    rows = db.scalars(select(StockMetaLabelMode).order_by(StockMetaLabelMode.strategy_type)).all()
    events = db.scalars(select(StockMetaLabelModeEvent).order_by(StockMetaLabelModeEvent.id.desc())
                        .limit(events_limit)).all()
    return {
        "configured_mode": settings.paper_meta_label_mode,
        "auto_enforce_enabled": settings.meta_label_auto_enforce_enabled,
        "bar": {
            "min_samples": settings.meta_label_enforce_min_samples,
            "min_precision": settings.meta_label_enforce_min_precision,
            "min_recall": settings.meta_label_enforce_min_recall,
            "min_lift": settings.meta_label_enforce_min_lift,
            "demote_min_samples": settings.meta_label_demote_min_samples,
            "demote_precision": settings.meta_label_demote_precision,
        },
        "strategies": [mode_out(row) for row in rows],
        "transitions": [
            {"id": event.id, "strategy_type": event.strategy_type, "from_mode": event.from_mode,
             "to_mode": event.to_mode, "reason": event.reason, "evidence": event.evidence,
             "created_at": _utc(event.created_at).isoformat() if event.created_at else None}
            for event in events
        ],
        "paper_only": True,
    }
