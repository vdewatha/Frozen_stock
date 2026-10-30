"""Paper-only closed feedback loop from losing trades to behaviour changes.

1. ``trade_attribution`` explains every closed paper trade (append-only).
2. ``parameter_application.build_loss_agenda`` ranks strategies/symbols by
   recent attributed losses; the nightly experiment pass follows it and
   passing candidates become governed, versioned parameter sets.
3. ``meta_label_modes`` promotes a strategy's meta-label filter from shadow
   to enforce (and back) on measured precision/recall.
4. ``strategy_allocation`` scales each strategy's paper risk fraction by a
   bounded function of its recent net realized performance.

This module runs the nightly bookkeeping and builds the compact, auditable
summary that promotion-readiness and soak reports embed.  Nothing here can
submit an order or touch live trading.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Strategy

UTC = timezone.utc
SUMMARY_VERSION = "learning-from-losses-v1"


def autotrader_strategy_types(db: Session) -> list[str]:
    return sorted(set(db.scalars(select(Strategy.strategy_type).where(
        Strategy.current_status == "paper_trading_active")).all()))


def run_loss_feedback(db: Session, *, now: Optional[datetime] = None) -> dict:
    """Nightly: attribute closed trades, then review per-strategy meta-label modes."""
    from app.services.meta_label_modes import review_meta_label_modes
    from app.services.trade_attribution import run_trade_attribution

    now = now or datetime.now(UTC)
    attribution = run_trade_attribution(db, now=now)
    db.commit()
    modes = review_meta_label_modes(db, autotrader_strategy_types(db), now=now)
    db.commit()
    return {"status": "complete", "attribution": attribution, "meta_label_modes": modes,
            "paper_only": True, "live_authorized": False}


def learning_from_losses_summary(db: Session, *, since: Optional[datetime] = None,
                                 until: Optional[datetime] = None,
                                 trial_ids: Optional[Iterable[str]] = None) -> dict:
    """Deterministic evidence block for readiness and soak reports.

    Never raises: an unavailable store is reported as ``status: unavailable``
    (unknown, not healthy).
    """
    try:
        with db.begin_nested():  # a failed read must not poison the caller's transaction
            return _summary(db, since=since, until=until, trial_ids=trial_ids)
    except Exception as exc:  # evidence must be explicit, never silently healthy
        return {"version": SUMMARY_VERSION, "status": "unavailable",
                "reason": f"{exc.__class__.__name__}: {exc}", "paper_only": True, "live_authorized": False}


def _summary(db: Session, *, since, until, trial_ids) -> dict:
    from app.services.meta_label_modes import meta_label_mode_projection
    from app.services.parameter_application import build_loss_agenda, parameter_application_projection
    from app.services.strategy_allocation import allocation_projection
    from app.services.trade_attribution import attribution_summary, current_attributions

    until = until or datetime.now(UTC)
    since = since or until - timedelta(days=settings.loss_feedback_lookback_days)
    rows = [row for row in current_attributions(db, since=since)
            if row.closed_at is not None and _utc(row.closed_at) <= until]
    trial_summary = None
    if trial_ids is not None:
        trial_rows = current_attributions(db, trial_ids=list(trial_ids))
        trial_summary = _compact(attribution_summary(trial_rows))
    agenda = build_loss_agenda(db, now=until)
    applications = parameter_application_projection(db, limit=10)
    modes = meta_label_mode_projection(db, events_limit=10)
    allocation = allocation_projection(db, limit_sessions=1)
    return {
        "version": SUMMARY_VERSION,
        "status": "available" if rows else "no_attributed_trades",
        "window": {"since": since.isoformat(), "until": until.isoformat()},
        "attribution": _compact(attribution_summary(rows)),
        "trial_attribution": trial_summary,
        "agenda": [{key: item[key] for key in ("rank", "strategy_type", "losses", "loss_return", "focus", "symbols")}
                   for item in agenda["items"]],
        "parameter_applications": [
            {"strategy_type": item["strategy_type"], "status": item["status"], "cycle_id": item["cycle_id"],
             "version": (item["parameter_set"] or {}).get("version"),
             "experiment_id": (item["parameter_set"] or {}).get("experiment_id"),
             "attribution_ids": ((item["parameter_set"] or {}).get("lineage") or {}).get("attribution_ids", [])[:20]}
            for item in applications["applications"]
        ],
        "active_parameter_sets": applications["active"],
        "meta_label_modes": [{"strategy_type": row["strategy_type"], "mode": row["mode"],
                              "changed_at": row["changed_at"]} for row in modes["strategies"]],
        "allocation": allocation["current"],
        "allocation_policy": allocation["policy"],
        "paper_only": True,
        "live_authorized": False,
    }


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _compact(summary: dict) -> dict:
    top = 5
    return {
        "trades": summary["trades"], "losses": summary["losses"], "loss_rate": summary["loss_rate"],
        "sum_return": summary["sum_return"], "loss_return": summary["loss_return"],
        "costs_unknown": summary["costs_unknown"],
        "by_strategy": summary["by_strategy"][:top], "by_symbol": summary["by_symbol"][:top],
        "by_exit_cause": summary["by_exit_cause"], "by_regime": summary["by_regime"][:top],
        "primary_mistakes": summary["primary_mistakes"],
        "attribution_count": len(summary["attribution_ids"]),
    }
