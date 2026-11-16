"""Does the learning loop improve outcomes?  One traceable record per chain.

The experiment engine runs, and a run that completes or a parameter that is
adopted proves nothing about trading.  What would is a record that can be
followed end to end:

    diagnosed problem -> experiment -> change -> forward evaluation -> retained or reverted

``learning_trace`` builds that record from the engine's own audit rows (it
writes nothing).  One chain per experiment:

* **diagnosis**: the lesson the experiment was run on, with the evidence id
  the engine recorded (``experiment_engine.lesson_identity``) and the losing
  trades behind it.  An experiment recorded before 2026-11-16 has no id; the
  chain says so rather than inventing a link;
* **experiment**: what was replayed, how many setups, its decision, and what
  it cost in replay seconds;
* **change**: the adopted setting, if any (most experiments adopt nothing);
* **forward evaluation**: the latest verdict on the change, the paper trades
  it was judged on against the trades it needs, the expectancy before and
  after, and the realised gain with its source.  ``paired_shadow`` is a
  concurrent comparison against the alternatives on the same decisions;
  ``before_after`` is not a control (the market moved too) and is labelled;
* **outcome**: ``no_change``, ``watching``, ``retained``, ``reverted`` or
  ``closed`` (judged, inconclusive).

``summary`` counts the chains by outcome and adds up the research cost.
``evidence`` is the honest reading: only a retained change judged on a
sufficient sample counts as the loop having improved something.

Read-only; paper only.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog

EVENT = "experiment_engine"
OUTCOMES = ("no_change", "watching", "retained", "reverted", "closed")
STATE_OUTCOME = {"confirmed": "retained", "reverted": "reverted", "closed": "closed", "watching": "watching"}


def _rows(db: Session, action: str, limit: Optional[int] = None) -> list:
    query = (select(AuditLog.id, AuditLog.status, AuditLog.payload, AuditLog.created_at)
             .where(AuditLog.event_type == EVENT, AuditLog.action == action).order_by(AuditLog.id.desc()))
    if limit:
        query = query.limit(limit)
    return db.execute(query).all()


def _number(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _forward(change: Mapping, verdicts: list[Mapping]) -> Optional[dict]:
    if not verdicts:
        return None
    last = verdicts[-1]
    watch = change.get("watch") or {}
    before, after = last.get("before") or change.get("recent_before") or {}, last.get("after") or {}
    trades, needed = int(after.get("trades") or 0), int(watch.get("min_trades") or 0)
    exp_before, exp_after = _number(before.get("expectancy_bps")), _number(after.get("expectancy_bps"))
    source = last.get("realised_source")
    return {
        "state": last.get("state"), "verdict": last.get("verdict"), "judged_at": last.get("judged_at"),
        "verdicts_recorded": len(verdicts), "method": last.get("verdict_method"),
        "trades_after": trades, "trades_needed": needed, "sample_sufficient": bool(needed and trades >= needed),
        "expectancy_before_bps": exp_before, "expectancy_after_bps": exp_after,
        "before_after_bps": (round(exp_after - exp_before, 1) if exp_before is not None and exp_after is not None
                             else None),
        "expected_gain_bps": _number(last.get("expected_gain_bps")),
        "realised_gain_bps": _number(last.get("realised_gain_bps")), "realised_source": source,
        "comparison": ("concurrent: the alternatives scored on the same decisions" if source and "shadow" in str(source)
                       else "before against after: not a control, the market moved too"),
        "shadow": last.get("shadow"),
    }


def _chain(audit_id: int, status: str, payload: Mapping, changes: Mapping[str, Mapping],
           verdicts: Mapping[str, list]) -> dict:
    change_id = payload.get("change_id")
    change = changes.get(change_id) if change_id else None
    forward = _forward(change, verdicts.get(change_id) or []) if change else None
    if change is None:
        outcome = "no_change"
    elif forward is None:
        outcome = "watching"
    else:
        outcome = STATE_OUTCOME.get(str(forward["state"]), "watching")
    gaps = []
    if not payload.get("lesson_id"):
        gaps.append("no lesson id: the experiment was recorded before lessons carried one")
    elif payload.get("lesson_source") == "trade_attribution" and not payload.get("lesson_attribution_ids"):
        gaps.append("the lesson names no losing trade")
    if payload.get("cost") is None:
        gaps.append("no cost recorded")
    if payload.get("adopted") and payload.get("applied") and change is None:
        gaps.append("the experiment says it adopted a change, but no change row is linked")
    if change is not None and forward is None:
        gaps.append("adopted, not judged yet")
    if forward is not None and outcome in ("retained", "reverted") and not forward["sample_sufficient"]:
        short = f"judged on {forward['trades_after']} trades, fewer than the {forward['trades_needed']} it needs"
        gaps.append(short + (" (decided on shadow evidence)" if forward.get("shadow") else ""))
    cost = payload.get("cost") or {}
    return {
        "experiment_id": audit_id, "ran_at": payload.get("ran_at"), "strategy_type": payload.get("strategy_type"),
        "template": payload.get("template"),
        "diagnosis": {"lesson_id": payload.get("lesson_id"), "cause": payload.get("trigger"),
                      "lesson": payload.get("lesson"), "losses": payload.get("lesson_count"),
                      "scope": payload.get("lesson_scope"), "source": payload.get("lesson_source"),
                      "attribution_ids": payload.get("lesson_attribution_ids") or []},
        "experiment": {"status": status, "adopted": payload.get("adopted"),
                       "description": payload.get("adopted_description"),
                       "setups": cost.get("setups") if cost else len(payload.get("rows") or {}) or None,
                       "cost_seconds": _number(cost.get("seconds"))},
        "change": ({"change_id": change_id, "adopted_at": change.get("adopted_at"),
                    "description": change.get("description"), "before": change.get("before"),
                    "after": change.get("after")} if change else None),
        "forward": forward, "outcome": outcome, "complete": not gaps, "gaps": gaps,
    }


def learning_trace(db: Session, *, limit: int = 100) -> dict:
    """``GET /stock-paper/learning-trace``: every experiment followed from its lesson to its outcome (read-only)."""
    changes = {str((payload or {}).get("change_id")): dict(payload or {}) for _id, _status, payload, _at in
               _rows(db, "change_adopted")}
    verdicts: dict[str, list] = {}
    for _id, _status, payload, _at in reversed(_rows(db, "change_verdict")):  # oldest first
        verdicts.setdefault(str((payload or {}).get("change_id")), []).append(dict(payload or {}))
    chains = [_chain(audit_id, status, dict(payload or {}), changes, verdicts)
              for audit_id, status, payload, _at in _rows(db, "experiment", limit)]
    linked = {chain["change"]["change_id"] for chain in chains if chain["change"]}
    orphans = sorted(change_id for change_id in changes if change_id not in linked)
    outcomes = {name: sum(1 for chain in chains if chain["outcome"] == name) for name in OUTCOMES}
    retained = [chain for chain in chains if chain["outcome"] == "retained"]
    proven = [chain for chain in retained if chain["forward"] and chain["forward"]["sample_sufficient"]]
    gains = [chain["forward"]["realised_gain_bps"] for chain in proven
             if chain["forward"]["realised_gain_bps"] is not None]
    seconds = [chain["experiment"]["cost_seconds"] for chain in chains if chain["experiment"]["cost_seconds"] is not None]
    if not chains:
        evidence = "No experiment has run yet, so there is nothing to judge."
    elif not proven:
        evidence = (f"Not yet: {len(chains)} experiments, {sum(1 for c in chains if c['change'])} changes adopted, "
                    f"none retained on a sufficient forward sample. A completed run or an adopted parameter is not "
                    f"evidence of better trading.")
    else:
        mean = round(sum(gains) / len(gains), 1) if gains else None
        evidence = (f"{len(proven)} change(s) retained on a sufficient forward sample"
                    + (f", mean realised gain {mean:+.1f} bps a trade" if mean is not None else "")
                    + f"; {outcomes['reverted']} reverted. Each is one strategy's own trades, not a pooled test.")
    return {
        "summary": {"chains": len(chains), "with_lesson_id": sum(1 for c in chains if c["diagnosis"]["lesson_id"]),
                    "complete": sum(1 for c in chains if c["complete"]),
                    "changes_adopted": sum(1 for c in chains if c["change"]), "outcomes": outcomes,
                    "retained_on_sufficient_sample": len(proven),
                    "research_seconds": round(sum(seconds), 1) if seconds else None,
                    "experiments_with_cost": len(seconds)},
        "evidence": evidence, "chains": chains,
        "unlinked_changes": orphans,
        "note": ("A chain is diagnosed problem, experiment, change, forward evaluation, outcome. 'retained' means the "
                 "forward judge confirmed the change; 'closed' means it was judged and found inconclusive."),
        "paper_only": True, "live_authorized": False,
    }
