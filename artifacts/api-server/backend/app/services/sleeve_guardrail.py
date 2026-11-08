"""Monthly sleeves: exempt from the 20-trade revert, judged by a harm guardrail on daily active P&L.

A sleeve that rebalances monthly makes about twelve decisions a year, so the
experiment engine's ``fixed_20`` verdict (20 closed trades after a change
against the 20 before) would need years to read and would then read noise.
The feasibility report (``reports/Retail bot edge feasibility.md``, step 1 and
"Read every verdict this plan produces the same way") exempts monthly sleeves
from it and runs a harm guardrail on the sleeve's **daily active return
against the passive arm** instead.  ``fixed_20`` keeps acting on the
short-horizon strategies and the e-process stays in shadow; neither is
changed here.

The pre-registered rule (``RULE``, fixed here before any sleeve exists):

* ``active_t`` is the date-clustered t of the mean daily active return
  (sleeve return minus passive-arm return, one observation per session;
  ``verdict_stats.paired_stats``, CR0 by session date).  After at least
  ``MIN_SESSIONS`` (60) sessions, ``active_t <= T_HARM`` (-2) is harm: a
  one-sided test at roughly the 2.3% level that the sleeve loses to the arm.
* At any time, a drawdown of the active wealth ratio
  ``prod(1 + sleeve) / prod(1 + passive)`` from its running peak of more than
  ``DRAWDOWN_LIMIT`` (15%) is harm.
* Otherwise ``watching`` (under 60 sessions) or ``ok``.  No rows = ``no_data``,
  and nothing is done.

On harm the guardrail pauses the sleeve's strategy row (a
``strategy_governance`` / ``sleeve_guardrail_pause`` audit row), and a change
the experiment engine adopted on a sleeve row is reverted by the engine's own
revert path.  Re-admission is a person's or a fresh governed cycle's call.

A sleeve row is a Strategy whose ``parameters["sleeve_cadence"] == "monthly"``
(``is_monthly_sleeve``).  The target-weight sleeve engine (step 2) records
each sleeve's daily P&L; this module reads it defensively from any of:

* registered readers (``register_reader(fn)``, ``fn(db, strategy) -> rows``),
* ``parameters["sleeve_daily_pnl"]`` on the row,
* audit rows with ``event_type`` in ``PNL_EVENTS`` and ``action`` in
  ``PNL_ACTIONS`` whose ``entity_id`` is the row (or payload ``strategy_id`` /
  ``strategy_type`` names it), one per session or carrying a ``rows`` list.

A row is ``{"session": "YYYY-MM-DD", "sleeve_return": r, "passive_return": p}``
or ``{"session": ..., "active_return": a}`` (``date`` and ``return`` /
``benchmark_return`` are accepted aliases); returns are fractions.
Unreadable or malformed rows are skipped, never raised.  Paper only.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Optional

import numpy as np

from app.core.config import settings

logger = logging.getLogger(__name__)
UTC = timezone.utc
EVENT = "sleeve_guardrail"
GOVERNANCE_EVENT = "strategy_governance"
PAUSE_ACTION = "sleeve_guardrail_pause"
CADENCE_KEY = "sleeve_cadence"
MONTHLY = "monthly"
MIN_SESSIONS = 60
T_HARM = -2.0
DRAWDOWN_LIMIT = 0.15
PNL_EVENTS = ("sleeve_engine", "target_weight_sleeve", "sleeve_pnl", "target_weight_engine")
PNL_ACTIONS = ("daily_pnl", "sleeve_daily_pnl", "sleeve_pnl")
PNL_PARAMETER = "sleeve_daily_pnl"
PAUSED_STATUSES = frozenset({"paused", "retired"})
RULE = (f"Monthly sleeves skip the fixed_20 verdict. Harm, judged on the daily active return (sleeve minus passive "
        f"arm): after >= {MIN_SESSIONS} sessions a date-clustered t of the mean daily active return <= {T_HARM:g}, or "
        f"at any time a drawdown of the active wealth ratio prod(1+sleeve)/prod(1+passive) of more than "
        f"{DRAWDOWN_LIMIT:.0%} from its peak. Harm pauses the sleeve row and reverts an engine change on it; no daily "
        f"P&L = no data, nothing done.")

_READERS: list[Callable] = []


def register_reader(reader: Callable) -> Callable:
    """Add a daily-P&L reader (``reader(db, strategy) -> iterable of rows``); returns it (usable as a decorator)."""
    if reader not in _READERS:
        _READERS.append(reader)
    return reader


def is_monthly_sleeve(strategy: Any) -> bool:
    """A strategy row (or its parameters mapping) whose ``sleeve_cadence`` is ``"monthly"``."""
    if strategy is None:
        return False
    if isinstance(strategy, Mapping):
        params = strategy.get("parameters") if isinstance(strategy.get("parameters"), Mapping) else strategy
    else:
        params = getattr(strategy, "parameters", None)
    if not isinstance(params, Mapping):
        return False
    return str(params.get(CADENCE_KEY) or "").strip().lower() == MONTHLY


def _number(row: Mapping, *keys: str) -> Optional[float]:
    for key in keys:
        if key in row and row[key] is not None:
            try:
                value = float(row[key])
            except (TypeError, ValueError):
                return None
            return value if math.isfinite(value) else None
    return None


def normalise_rows(rows: Iterable[Any]) -> list[dict]:
    """``[{"session", "active_return", "sleeve_return", "passive_return"}]`` sorted by session; bad rows dropped."""
    out: dict[str, dict] = {}
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        session = str(row.get("session") or row.get("date") or "")[:10]
        if len(session) != 10:
            continue
        sleeve = _number(row, "sleeve_return", "return")
        passive = _number(row, "passive_return", "benchmark_return")
        active = _number(row, "active_return")
        if active is None and sleeve is not None and passive is not None:
            active = sleeve - passive
        if active is None:
            continue
        out[session] = {"session": session, "active_return": active, "sleeve_return": sleeve, "passive_return": passive}
    return [out[key] for key in sorted(out)]


def _audit_rows(db, strategy) -> list[dict]:
    from app.models import AuditLog

    rows = db.query(AuditLog).filter(AuditLog.event_type.in_(PNL_EVENTS), AuditLog.action.in_(PNL_ACTIONS)).order_by(
        AuditLog.id).all()
    out = []
    for row in rows:
        payload = row.payload or {}
        named = (row.entity_id == strategy.id or payload.get("strategy_id") == strategy.id
                 or (payload.get("strategy_type") and payload.get("strategy_type") == strategy.strategy_type))
        if not named:
            continue
        if isinstance(payload.get("rows"), list):
            out.extend(payload["rows"])
        else:
            out.append(payload)
    return out


def sleeve_daily_returns(db, strategy) -> list[dict]:
    """Every readable daily active-return row for one sleeve; ``[]`` when none exists yet."""
    rows: list = []
    for reader in list(_READERS):
        try:
            rows.extend(reader(db, strategy) or [])
        except Exception as exc:  # a reader that cannot read is no data, never an error
            logger.warning("sleeve guardrail: reader %s failed for %s: %s", getattr(reader, "__name__", reader),
                           getattr(strategy, "strategy_type", None), exc)
    params = getattr(strategy, "parameters", None) or {}
    if isinstance(params.get(PNL_PARAMETER), list):
        rows.extend(params[PNL_PARAMETER])
    try:
        rows.extend(_audit_rows(db, strategy))
    except Exception as exc:
        db.rollback()
        logger.warning("sleeve guardrail: audit rows unavailable: %s", exc)
    return normalise_rows(rows)


def harm_guardrail(rows: Iterable[Mapping]) -> dict:
    """The pre-registered rule on one sleeve's daily rows (``RULE``)."""
    from app.services.yardstick import clustered_t

    rows = normalise_rows(rows)
    base = {"rule": RULE, "min_sessions": MIN_SESSIONS, "t_harm": T_HARM, "drawdown_limit": DRAWDOWN_LIMIT}
    if not rows:
        return base | {"status": "no_data", "harm": False, "sessions": 0,
                       "reason": "no daily sleeve P&L recorded yet; the guardrail does nothing"}
    active = np.asarray([row["active_return"] for row in rows], dtype=float)
    sleeve = [row["sleeve_return"] for row in rows]
    passive = [row["passive_return"] for row in rows]
    if all(s is not None for s in sleeve) and all(p is not None for p in passive):
        ratio = np.cumprod(1 + np.asarray(sleeve, dtype=float)) / np.cumprod(1 + np.asarray(passive, dtype=float))
    else:
        ratio = np.cumprod(1 + active)
    curve = np.concatenate([[1.0], ratio])
    drawdown = float(np.max(1 - curve / np.maximum.accumulate(curve)))
    t = clustered_t(active, [row["session"] for row in rows]) if len(rows) >= 2 else {"t_stat": None, "mean_bps": None}
    sessions = len(rows)
    reasons = []
    if drawdown > DRAWDOWN_LIMIT:
        reasons.append(f"active drawdown {drawdown:.1%} > {DRAWDOWN_LIMIT:.0%}")
    if sessions >= MIN_SESSIONS and t.get("t_stat") is not None and t["t_stat"] <= T_HARM:
        reasons.append(f"date-clustered t {t['t_stat']:+.2f} <= {T_HARM:g} after {sessions} sessions")
    harm = bool(reasons)
    status = "harm" if harm else ("watching" if sessions < MIN_SESSIONS else "ok")
    return base | {"status": status, "harm": harm, "sessions": sessions, "first_session": rows[0]["session"],
                   "last_session": rows[-1]["session"], "mean_active_bps_per_day": t.get("mean_bps"),
                   "t_stat": t.get("t_stat"), "active_drawdown": round(drawdown, 4),
                   "cumulative_active": round(float(ratio[-1] - 1.0), 6),
                   "reason": "; ".join(reasons) if reasons else (
                       f"{sessions} sessions, t {t.get('t_stat')}, active drawdown {drawdown:.1%}")}


def sleeve_rows(db) -> list:
    from app.models import Strategy

    return [row for row in db.query(Strategy).order_by(Strategy.id).all() if is_monthly_sleeve(row)]


def pause_sleeve(db, strategy, verdict: Mapping, *, now: datetime) -> bool:
    from app.services.audit import write_audit_log

    if strategy.current_status in PAUSED_STATUSES:
        return False
    old = strategy.current_status
    strategy.current_status = "paused"
    write_audit_log(db, event_type=GOVERNANCE_EVENT, entity_type="strategy", entity_id=strategy.id,
                    action=PAUSE_ACTION, status="complete",
                    message=f"{strategy.strategy_type} paused by the monthly-sleeve harm guardrail: {verdict.get('reason')}",
                    payload={"old_status": old, "new_status": "paused", "verdict": dict(verdict),
                             "source": EVENT, "judged_at": now.isoformat(), "paper_only": True,
                             "live_authorized": False})
    return True


def review_monthly_sleeves(db, *, now: Optional[datetime] = None) -> dict:
    """Daily: run the guardrail on every monthly sleeve; pause one that harms (paper only)."""
    from app.services.audit import write_audit_log

    now = now or datetime.now(UTC)
    if not settings.sleeve_guardrail_enabled:
        return {"status": "disabled", "paper_only": True}
    reviewed = []
    for row in sleeve_rows(db):
        verdict = harm_guardrail(sleeve_daily_returns(db, row))
        paused = False
        if verdict["harm"] and settings.sleeve_guardrail_action_enabled:
            paused = pause_sleeve(db, row, verdict, now=now)
        reviewed.append({"strategy_id": row.id, "strategy_type": row.strategy_type, "status": verdict["status"],
                         "sessions": verdict["sessions"], "t_stat": verdict.get("t_stat"),
                         "active_drawdown": verdict.get("active_drawdown"), "reason": verdict["reason"],
                         "paused": paused})
    write_audit_log(db, event_type=EVENT, entity_type="strategy", action="review", status="complete",
                    message=f"sleeve guardrail: {len(reviewed)} monthly sleeves, "
                            f"{sum(1 for r in reviewed if r['status'] == 'harm')} harmful, "
                            f"{sum(1 for r in reviewed if r['paused'])} paused",
                    payload={"reviewed": reviewed, "rule": RULE, "judged_at": now.isoformat(), "paper_only": True})
    db.commit()
    return {"status": "complete", "reviewed": reviewed, "rule": RULE, "paper_only": True}


def guardrail_view(db) -> dict:
    rows = []
    for row in sleeve_rows(db):
        verdict = harm_guardrail(sleeve_daily_returns(db, row))
        rows.append({"strategy_id": row.id, "strategy_type": row.strategy_type, "current_status": row.current_status,
                     **{k: verdict.get(k) for k in ("status", "sessions", "t_stat", "active_drawdown", "reason")}})
    return {"rule": RULE, "enabled": bool(settings.sleeve_guardrail_enabled),
            "action_enabled": bool(settings.sleeve_guardrail_action_enabled), "sleeves": rows}
