"""Defensive sleeve: Design A as a zero-capital shadow beside the slow book, and its one re-decision (paper only).

Step 3 of ``reports/Retail bot edge feasibility.md``; the run, the decision
and this build are in ``reports/defensive-sleeve-20261107.md``.  The frozen
designs, the backtest and the rule are in ``defensive_sleeve_rule`` (``RULE``,
``DECISION``).  On 2026-11-07 both designs failed the rule, so the static
passive arm keeps all of the book's capital and Design A (TSMOM-IV) runs here
as a shadow.  It places no orders and holds no capital.

The shadow row
    A ``Strategy`` (``strategy_type = SHADOW_TYPE``), created once by the seed,
    with ``parameters["sleeve_cadence"] = "monthly"`` (so ``fixed_20`` never
    judges it and the step 1 harm guardrail reads it), ``capital_fraction = 0``
    and **no** ``target_weights`` (the book's ``sleeve_allocations`` lists it
    as a zero-capital sleeve and ``book_symbols`` never fences its ETFs from
    the slot cycle).  Its status ``SHADOW_STATUS`` is one no cycle selects.

Monthly target weights and hypothetical tranched fills
    Four tranches on the book's own schedule (tranche k on the k-th
    ``target_weight_rebalance_weekday``, or the next session that week).  On
    a tranche's session the signal is Design A at the previous close and the
    hypothetical fill is that session's close, at ``COST_BPS`` per side of
    one-way turnover.  Each tranche starts as a quarter of the notional in
    BIL and drifts between its rebalances, as in the backtest.  The notional
    is ``SLICE`` of account equity when the shadow starts.  Prices are
    yfinance adjusted closes (the $0 data budget); state is carried as values
    and weights, so later dividend adjustments of the history do not move it.

Audit rows (``event_type = EVENT`` unless noted, no migration)
    ``shadow_start``        the notional, the tranches in BIL
    ``shadow_tranche``      one per tranche per month: signal session, target
                            weights, hypothetical fills (symbol, side,
                            notional, price), turnover and cost
    ``sleeve_daily_pnl``    (``event_type = "sleeve_pnl"``, ``entity_id`` = the
                            shadow row) one per session: ``sleeve_return``,
                            ``passive_return`` (the book's passive-arm return
                            for the session when recorded, else the arm's
                            weights on VOO/SGOV closes), ``active_return``,
                            P&L and value, tranche states.  This is the shape
                            ``sleeve_guardrail`` reads, so the guardrail
                            judges the shadow's forward record too.
    ``redecision_scheduled`` / ``redecision_fired`` / ``redecision_verdict``
                            the trigger's state (``redecision_state``)

Re-decision
    Once: at ``REDECIDE_MONTHS`` after the decision date, or when SPY closes
    more than ``SPY_DRAWDOWN_TRIGGER`` below its running peak since the
    decision's data end, whichever is first.  On firing it notifies, re-runs
    ``defensive_sleeve_rule.evaluate`` on fresh yfinance history and records
    the verdict (retried on later runs until it can).  A pass is reported for
    the owner to fund; nothing here funds a sleeve.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional

import pandas as pd

from app.core.config import settings
from app.models import AuditLog, Strategy
from app.services import defensive_sleeve_rule as rule
from app.services.audit import write_audit_log

logger = logging.getLogger(__name__)
UTC = timezone.utc

EVENT = "defensive_sleeve"
PNL_EVENT = "sleeve_pnl"            # read by sleeve_guardrail (PNL_EVENTS)
PNL_ACTION = "sleeve_daily_pnl"     # read by sleeve_guardrail (PNL_ACTIONS)
START_ACTION = "shadow_start"
TRANCHE_ACTION = "shadow_tranche"
SCHEDULED_ACTION = "redecision_scheduled"
FIRED_ACTION = "redecision_fired"
VERDICT_ACTION = "redecision_verdict"
ADMISSION_ACTION = "defensive_shadow_admission"
GOVERNANCE_EVENT = "strategy_governance"
SHADOW_TYPE = "defensive_tsmom_iv"
SHADOW_NAME = "Defensive Sleeve A (TSMOM-IV, shadow)"
SHADOW_STATUS = "target_weight_shadow"
DESIGN = rule.DESIGN_A
COST_BPS = rule.COSTS_BPS[0]
SLICE = rule.SLICE
TRANCHES = (1, 2, 3, 4)
BACKFILL_SESSIONS = 30
HISTORY_DAYS = 560  # calendar days fetched before the first session needed (253+ sessions of signal history)
DECIDED_ON = date.fromisoformat(rule.DECISION["decided_on"])
DATA_END = date.fromisoformat(rule.DECISION["data_end"])

Fetcher = Callable[[list[str], date], pd.DataFrame]


def add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    for last in (31, 30, 29, 28):
        try:
            return date(year, month, min(day.day, last))
        except ValueError:
            continue
    raise ValueError(day)


REDECIDE_ON = add_months(DECIDED_ON, rule.REDECIDE_MONTHS)


def default_fetcher(symbols: list[str], start: date) -> pd.DataFrame:
    """Daily adjusted closes from yfinance (the $0 data budget), one column per symbol."""
    import yfinance as yf

    frame = yf.download(sorted(set(symbols)), start=start.isoformat(), auto_adjust=True, progress=False)["Close"]
    if isinstance(frame, pd.Series):
        frame = frame.to_frame(symbols[0])
    frame.index = pd.DatetimeIndex(frame.index).tz_localize(None).normalize()
    return frame.dropna(how="all")


# ------------------------------------------------------------------ rows

def shadow_row(db) -> Optional[Strategy]:
    return db.query(Strategy).filter(Strategy.strategy_type == SHADOW_TYPE).order_by(Strategy.id).first()


def _rows(db, action: str, event: str = EVENT, entity_id: Optional[int] = None) -> list[AuditLog]:
    query = db.query(AuditLog).filter(AuditLog.event_type == event, AuditLog.action == action)
    if entity_id is not None:
        query = query.filter(AuditLog.entity_id == entity_id)
    return query.order_by(AuditLog.id).all()


def _latest(db, action: str, event: str = EVENT, entity_id: Optional[int] = None) -> Optional[AuditLog]:
    rows = _rows(db, action, event, entity_id)
    return rows[-1] if rows else None


def admit_shadow(db) -> Optional[str]:
    """One-time seed step: the zero-capital Design A shadow row and the re-decision schedule."""
    exists = db.query(Strategy.id).filter((Strategy.strategy_type == SHADOW_TYPE) | (Strategy.name == SHADOW_NAME)).first()
    already = db.query(AuditLog.id).filter(AuditLog.event_type == GOVERNANCE_EVENT,
                                           AuditLog.action == ADMISSION_ACTION).first()
    if exists or already:
        return None
    row = Strategy(
        name=SHADOW_NAME, strategy_type=SHADOW_TYPE,
        description=("Design A (TSMOM-IV over SPY, EFA, EEM, IEF, TLT, GLD, DBC, VNQ; T-bills otherwise) as a "
                     "zero-capital shadow beside the passive arm: monthly target weights in four tranches, "
                     "hypothetical fills and daily shadow P&L against the arm, no orders. It failed the step 3 "
                     f"rule on 2026-11-07 and is re-decided once under it ({rule.REPORT})."),
        parameters={"sleeve_cadence": "monthly", "sleeve_role": "defensive_shadow", "design": DESIGN,
                    "capital_fraction": 0, "zero_capital": True, "universe": list(rule.SYMBOLS),
                    "cost_bps_per_side": COST_BPS, "notional_fraction_of_equity": SLICE,
                    "source_report": rule.REPORT},
        is_active=True, current_status=SHADOW_STATUS,
    )
    db.add(row)
    db.flush()
    write_audit_log(db, event_type=GOVERNANCE_EVENT, entity_type="strategy", entity_id=row.id,
                    action=ADMISSION_ACTION, status="complete",
                    message=f"{rule.REPORT}: Design A admitted as a zero-capital shadow sleeve (no orders, no capital).",
                    payload={"new_status": SHADOW_STATUS, "decision": rule.DECISION["outcome"], "rule": rule.RULE,
                             "source": rule.REPORT, "paper_only": True})
    schedule_redecision(db)
    db.flush()
    return SHADOW_STATUS


def schedule_redecision(db) -> dict:
    row = _latest(db, SCHEDULED_ACTION)
    if row is not None:
        return row.payload or {}
    payload = {"decided_on": DECIDED_ON.isoformat(), "data_end": DATA_END.isoformat(),
               "due_on": REDECIDE_ON.isoformat(), "spy_drawdown_trigger": rule.SPY_DRAWDOWN_TRIGGER,
               "spy_peak_since": DATA_END.isoformat(), "rule": rule.RULE, "once": True, "paper_only": True}
    write_audit_log(db, event_type=EVENT, entity_type="defensive_sleeve", action=SCHEDULED_ACTION, status="scheduled",
                    message=(f"Defensive-sleeve re-decision scheduled: {REDECIDE_ON} or the next SPY drawdown deeper "
                             f"than {rule.SPY_DRAWDOWN_TRIGGER:.0%}, whichever comes first"), payload=payload)
    return payload


# ------------------------------------------------------------ shadow state

def tranche_due(day: date, sessions: list[date], fired: set[tuple[str, int]]) -> Optional[int]:
    """The tranche whose rebalance falls on ``day``: the first session on or after its scheduled date that week."""
    from app.services.target_weight_book import cycle_of, tranche_dates

    for k, scheduled in tranche_dates(day.year, day.month).items():
        if (cycle_of(day), k) in fired or not (scheduled <= day < scheduled + timedelta(days=7)):
            continue
        earlier = [s for s in sessions if scheduled <= s < day]
        if not earlier:
            return k
    return None


def _drift(weights: Mapping[str, float], rets: Mapping[str, float]) -> tuple[float, dict[str, float]]:
    growth = sum(w * (1 + rets.get(s, 0.0)) for s, w in weights.items())
    gross = sum(w * rets.get(s, 0.0) for s, w in weights.items())
    drifted = {s: w * (1 + rets.get(s, 0.0)) / growth for s, w in weights.items()} if growth else dict(weights)
    return gross, drifted


def step_session(state: Mapping, day: date, prev_day: date, closes: pd.DataFrame, tranche: Optional[int]) -> dict:
    """One session of the shadow (pure): drift every tranche, then trade the due tranche at the close."""
    row, prev = closes.loc[pd.Timestamp(day)], closes.loc[pd.Timestamp(prev_day)]
    rets = {s: float(row[s] / prev[s] - 1) for s in rule.SYMBOLS if pd.notna(row.get(s)) and pd.notna(prev.get(s))}
    before = sum(t["value"] for t in state["tranches"].values())
    tranches, fill = {}, None
    for key, t in state["tranches"].items():
        gross, drifted = _drift(t["weights"], rets)
        tranches[key] = {"value": t["value"] * (1 + gross), "weights": drifted, "last_rebalance": t.get("last_rebalance")}
    if tranche is not None:
        history = closes.loc[:pd.Timestamp(prev_day), list(rule.SYMBOLS)]
        target = rule.design_a_weights(history)
        if target is not None:
            t = tranches[str(tranche)]
            turnover = sum(abs(target.get(s, 0.0) - t["weights"].get(s, 0.0)) for s in set(target) | set(t["weights"]))
            cost = turnover * COST_BPS / 1e4 * t["value"]
            fills = [{"symbol": s, "side": "buy" if target.get(s, 0.0) > t["weights"].get(s, 0.0) else "sell",
                      "notional": round(abs(target.get(s, 0.0) - t["weights"].get(s, 0.0)) * t["value"], 2),
                      "price": round(float(row[s]), 4), "price_basis": "adjusted close (yfinance)"}
                     for s in rule.SYMBOLS if abs(target.get(s, 0.0) - t["weights"].get(s, 0.0)) * t["value"] >= 0.01]
            fill = {"tranche": tranche, "signal_session": prev_day.isoformat(), "fill_session": day.isoformat(),
                    "target_weights": {s: round(w, 6) for s, w in target.items()},
                    "previous_weights": {s: round(w, 6) for s, w in t["weights"].items()},
                    "tranche_value": round(t["value"], 2), "turnover": round(turnover, 6),
                    "cost": round(cost, 4), "cost_bps_per_side": COST_BPS, "fills": fills}
            tranches[str(tranche)] = {"value": t["value"] - cost, "weights": dict(target),
                                      "last_rebalance": day.isoformat()}
    after = sum(t["value"] for t in tranches.values())
    return {"session": day.isoformat(), "previous_session": prev_day.isoformat(), "tranches": tranches,
            "value": after, "pnl": after - before, "sleeve_return": (after / before - 1) if before else 0.0,
            "fill": fill, "returns": rets}


def _book_passive_return(db, day: date) -> Optional[float]:
    for row in reversed(_rows(db, "sleeve_daily_pnl", event="target_weight_book")):
        payload = row.payload or {}
        if payload.get("session") == day.isoformat():
            value = (payload.get("passive_arm") or {}).get("return")
            return float(value) if value is not None else None
    return None


def _modeled_passive_return(closes: pd.DataFrame, day: date, prev_day: date) -> Optional[float]:
    from app.services.target_weight_book import passive_weights

    weights = passive_weights()
    total = 0.0
    for symbol, weight in weights.items():
        if symbol not in closes.columns:
            return None
        now, before = closes.at[pd.Timestamp(day), symbol], closes.at[pd.Timestamp(prev_day), symbol]
        if pd.isna(now) or pd.isna(before) or before <= 0:
            return None
        total += float(weight) * float(now / before - 1)
    return total


def _last_state(db, strategy_id: int) -> Optional[dict]:
    row = _latest(db, PNL_ACTION, event=PNL_EVENT, entity_id=strategy_id)
    if row is not None:
        payload = row.payload or {}
        return {"session": payload["session"], "tranches": payload["tranches"]}
    start = _latest(db, START_ACTION, entity_id=strategy_id)
    if start is not None:
        payload = start.payload or {}
        return {"session": payload["session"], "tranches": payload["tranches"]}
    return None


def _account_equity(db) -> Optional[float]:
    try:
        from app.services.stock_paper_ledger import active_paper_account

        account = active_paper_account(db)
        return float(account.equity) if account is not None and account.equity else None
    except Exception:  # no account yet: the default notional stands
        return None


def run_shadow(db, *, now: Optional[datetime] = None, fetcher: Optional[Fetcher] = None) -> dict:
    """Book the shadow's sessions since the last one, and check the re-decision trigger (no orders)."""
    from app.services.target_weight_book import cycle_of, last_closed_session

    now = (now or datetime.now(UTC)).astimezone(UTC)
    base = {"job": "defensive_sleeve_daily_job", "paper_only": True, "orders": 0, "live_trading": False}
    if not settings.defensive_shadow_enabled:
        return base | {"status": "skipped", "reason": "the defensive shadow is disabled"}
    row = shadow_row(db)
    if row is None or not row.is_active or row.current_status == "retired":
        return base | {"status": "skipped", "reason": "no active defensive shadow row"}
    if row.current_status == "paper_trading_candidate":
        # ``resume_candidate_strategies`` moves every paused row; a shadow has no capital to resume.
        old, row.current_status = row.current_status, SHADOW_STATUS
        write_audit_log(db, event_type=GOVERNANCE_EVENT, entity_type="strategy", entity_id=row.id,
                        action="defensive_shadow_status_restored", status="complete",
                        message=f"Defensive shadow restored to {SHADOW_STATUS} after a resume-all (zero capital)",
                        payload={"old_status": old, "new_status": SHADOW_STATUS, "paper_only": True})
    through = last_closed_session(now)
    if through is None:
        return base | {"status": "skipped", "reason": "no closed session"}
    fetcher = fetcher or default_fetcher
    state = _last_state(db, row.id)
    first_needed = date.fromisoformat(state["session"]) if state else through
    symbols = list(rule.SYMBOLS) + sorted(set(_passive_symbols()))
    try:
        closes = fetcher(symbols, min(first_needed, DATA_END) - timedelta(days=HISTORY_DAYS))
    except Exception as exc:
        return base | {"status": "failed", "reason": f"price history unavailable: {exc.__class__.__name__}"}
    closes = closes.sort_index()
    closes = closes.loc[closes.index <= pd.Timestamp(through)]
    sessions = [ts.date() for ts in closes.dropna(subset=list(rule.SYMBOLS)).index]
    result = base | {"status": "complete", "through": through.isoformat(), "recorded": [], "tranches": []}
    if not sessions:
        return result | {"status": "failed", "reason": "no complete closes for the design's universe"}
    if state is None:
        start = sessions[-1]
        equity = _account_equity(db) or 100_000.0
        notional = round(equity * SLICE, 2)
        tranches = {str(k): {"value": notional / len(TRANCHES), "weights": {rule.CASH: 1.0}, "last_rebalance": None}
                    for k in TRANCHES}
        write_audit_log(db, event_type=EVENT, entity_type="strategy", entity_id=row.id, action=START_ACTION,
                        status="started",
                        message=f"Defensive shadow (Design A) started on {start}: notional {notional:.2f}, all tranches in BIL",
                        payload={"session": start.isoformat(), "notional": notional, "equity": equity,
                                 "tranches": tranches, "design": DESIGN, "zero_capital": True, "paper_only": True})
        state = {"session": start.isoformat(), "tranches": tranches}
        result["started"] = start.isoformat()
    fired = {((r.payload or {}).get("cycle"), int((r.payload or {}).get("tranche", 0)))
             for r in _rows(db, TRANCHE_ACTION, entity_id=row.id)}
    pending = [d for d in sessions if d > date.fromisoformat(state["session"])][-BACKFILL_SESSIONS:]
    for day in pending:
        prev_day = max(d for d in sessions if d < day)
        tranche = tranche_due(day, sessions, fired)
        step = step_session(state, day, prev_day, closes, tranche)
        book = _book_passive_return(db, day)
        passive = book if book is not None else _modeled_passive_return(closes, day, prev_day)
        if step["fill"] is not None:
            write_audit_log(db, event_type=EVENT, entity_type="strategy", entity_id=row.id, action=TRANCHE_ACTION,
                            status="hypothetical",
                            message=(f"Shadow tranche {tranche} of {cycle_of(day)}: hypothetical fills at the "
                                     f"{day} close, turnover {step['fill']['turnover']:.2f} (no orders)"),
                            payload=step["fill"] | {"cycle": cycle_of(day), "design": DESIGN, "zero_capital": True,
                                                    "paper_only": True})
            fired.add((cycle_of(day), tranche))
            result["tranches"].append(tranche)
        payload = {"session": day.isoformat(), "previous_session": prev_day.isoformat(), "strategy_id": row.id,
                   "strategy_type": SHADOW_TYPE, "design": DESIGN,
                   "sleeve_return": round(step["sleeve_return"], 10),
                   "passive_return": round(passive, 10) if passive is not None else None,
                   "active_return": round(step["sleeve_return"] - passive, 10) if passive is not None else None,
                   "passive_source": "target_weight_book" if book is not None else "modeled VOO/SGOV closes",
                   "pnl": round(step["pnl"], 4), "value": round(step["value"], 2),
                   "tranches": {k: {"value": t["value"], "weights": t["weights"], "last_rebalance": t["last_rebalance"]}
                                for k, t in step["tranches"].items()},
                   "rebalanced_tranche": tranche if step["fill"] else None, "zero_capital": True, "paper_only": True}
        write_audit_log(db, event_type=PNL_EVENT, entity_type="strategy", entity_id=row.id, action=PNL_ACTION,
                        status="recorded",
                        message=(f"Defensive shadow {day}: {step['sleeve_return']:+.4%} vs passive arm "
                                 f"{(passive if passive is not None else float('nan')):+.4%} (no orders)"),
                        payload=payload)
        state = {"session": day.isoformat(), "tranches": step["tranches"]}
        result["recorded"].append(day.isoformat())
    result["redecision"] = check_redecision(db, now=now, through=through, closes=closes, fetcher=fetcher)
    db.commit()
    return result


def _passive_symbols() -> tuple[str, str]:
    from app.services.target_weight_book import passive_symbols

    return passive_symbols()


# ------------------------------------------------------------- re-decision

def redecision_state(db) -> dict:
    scheduled = _latest(db, SCHEDULED_ACTION)
    fired = _latest(db, FIRED_ACTION)
    verdict = _latest(db, VERDICT_ACTION)
    payload = (scheduled.payload if scheduled is not None else None) or {
        "decided_on": DECIDED_ON.isoformat(), "due_on": REDECIDE_ON.isoformat(),
        "spy_drawdown_trigger": rule.SPY_DRAWDOWN_TRIGGER, "spy_peak_since": DATA_END.isoformat()}
    state = "decided" if verdict is not None else "fired" if fired is not None else "scheduled"
    return {"state": state, "rule": rule.RULE, "decided_on": payload.get("decided_on"), "due_on": payload.get("due_on"),
            "spy_drawdown_trigger": payload.get("spy_drawdown_trigger"), "spy_peak_since": payload.get("spy_peak_since"),
            "fired": (fired.payload or {}) | {"audit_id": fired.id} if fired is not None else None,
            "verdict": (verdict.payload or {}) | {"audit_id": verdict.id} if verdict is not None else None,
            "once": True}


def trigger_reason(today: date, spy_drawdown: Optional[float]) -> Optional[str]:
    """Which trigger fires (pure): the date first, then a SPY close more than 20% below its peak."""
    if today >= REDECIDE_ON:
        return f"{rule.REDECIDE_MONTHS} months since the decision ({REDECIDE_ON})"
    if spy_drawdown is not None and spy_drawdown < -rule.SPY_DRAWDOWN_TRIGGER:
        return f"SPY drawdown {spy_drawdown:.1%} from its peak since {DATA_END}, deeper than {rule.SPY_DRAWDOWN_TRIGGER:.0%}"
    return None


def _notify(db, *, title: str, message: str, severity: str, payload: dict) -> None:
    try:
        from app.services.notifications import create_notification

        create_notification(db, category="defensive_sleeve", severity=severity, source="defensive_sleeve",
                            title=title, message=message[:1000], entity_type="audit_log", payload=payload)
    except Exception as exc:  # the record stands without the notification
        logger.info("defensive-sleeve notification unavailable: %s", exc.__class__.__name__)


def check_redecision(db, *, now: datetime, through: date, closes: Optional[pd.DataFrame] = None,
                     fetcher: Optional[Fetcher] = None) -> dict:
    """Fire the one re-decision when due, then re-run the frozen rule and record its verdict (retried until done)."""
    state = redecision_state(db)
    if state["state"] == "decided":
        return {"state": "decided"}
    spy = None
    if closes is not None and rule.EQUITY in closes.columns:
        spy = rule.spy_drawdown(closes.loc[closes.index >= pd.Timestamp(DATA_END), rule.EQUITY])
    if state["state"] == "scheduled":
        reason = trigger_reason(through, (spy or {}).get("current"))
        if reason is None:
            return {"state": "scheduled", "spy_drawdown": spy}
        payload = {"fired_on": through.isoformat(), "reason": reason, "spy_drawdown": spy, "rule": rule.RULE,
                   "paper_only": True}
        write_audit_log(db, event_type=EVENT, entity_type="defensive_sleeve", action=FIRED_ACTION, status="fired",
                        message=f"Defensive-sleeve re-decision fired: {reason}", payload=payload)
        _notify(db, title="Defensive sleeve re-decision fired", severity="warning", payload=payload,
                message=f"{reason}. The step 3 rule is re-run once on fresh history: {rule.RULE}")
    try:
        history = (fetcher or default_fetcher)(list(rule.SYMBOLS), date(2006, 6, 1))
        result = rule.evaluate(history.loc[history.index <= pd.Timestamp(through)])
    except Exception as exc:  # retried by the next run
        logger.warning("defensive-sleeve re-decision could not run yet: %s", exc)
        return {"state": "fired", "verdict": "pending", "error": f"{exc.__class__.__name__}"}
    passes = {d: body["passes"] for d, body in result["designs"].items()}
    cells = {d: {k: {"arm_sharpe": round(c["base"]["sharpe"], 4), "slice_sharpe": round(c["slices"][f"{rule.SLICE:.2f}"]["sharpe"], 4),
                     "arm_max_drawdown": round(c["base"]["max_drawdown"], 4),
                     "slice_max_drawdown": round(c["slices"][f"{rule.SLICE:.2f}"]["max_drawdown"], 4),
                     "passes": c["passes"]} for k, c in body["cells"].items()} for d, body in result["designs"].items()}
    qualifies = [d for d, ok in passes.items() if ok]
    payload = {"decided_on": through.isoformat(), "data_end": result["data_end"], "passes": passes, "cells": cells,
               "qualifies_for_funding": qualifies, "rule": rule.RULE,
               "action": ("owner to fund the passing design at 20% of equity from the T-bill leg" if qualifies
                          else "no change: the static arm keeps the capital; the shadow keeps recording"),
               "paper_only": True}
    write_audit_log(db, event_type=EVENT, entity_type="defensive_sleeve", action=VERDICT_ACTION,
                    status="passed" if qualifies else "failed",
                    message=(f"Defensive-sleeve re-decision: " + (", ".join(qualifies) + " pass the rule" if qualifies
                                                                   else "both designs fail the rule again")),
                    payload=payload)
    _notify(db, title="Defensive sleeve re-decided", severity="info", payload={"passes": passes},
            message=payload["action"] + f" (data to {result['data_end']}).")
    return {"state": "decided", "passes": passes}


# ---------------------------------------------------------------- viewer

def view(db) -> dict:
    """The field on GET /stock-paper/target-weight-book: decision, shadow record, re-decision trigger."""
    row = shadow_row(db)
    shadow = None
    if row is not None:
        pnl = [r.payload or {} for r in _rows(db, PNL_ACTION, event=PNL_EVENT, entity_id=row.id)]
        start = _latest(db, START_ACTION, entity_id=row.id)
        sleeve = passive = 1.0
        for p in pnl:
            sleeve *= 1 + float(p.get("sleeve_return") or 0.0)
            passive *= 1 + float(p.get("passive_return") or 0.0)
        last = pnl[-1] if pnl else None
        tranches = [(r.payload or {}) | {"audit_id": r.id} for r in _rows(db, TRANCHE_ACTION, entity_id=row.id)[-4:]]
        shadow = {"strategy_id": row.id, "name": row.name, "status": row.current_status, "design": DESIGN,
                  "zero_capital": True, "orders": "none", "cost_bps_per_side": COST_BPS,
                  "started_on": (start.payload or {}).get("session") if start else None,
                  "notional": (start.payload or {}).get("notional") if start else None,
                  "sessions": len(pnl), "last_session": last.get("session") if last else None,
                  "value": last.get("value") if last else None,
                  "cumulative_return": round(sleeve - 1, 6), "passive_cumulative_return": round(passive - 1, 6),
                  "active_wealth_ratio": round(sleeve / passive - 1, 6) if passive else None,
                  "tranches": last.get("tranches") if last else None, "recent_tranches": tranches}
    return {"rule": rule.RULE, "report": rule.REPORT,
            "decision": {k: rule.DECISION[k] for k in ("decided_on", "data_end", "arm", "funded", "shadow", "outcome")}
            | {"passes": {d: b["passes"] for d, b in rule.DECISION["designs"].items()}},
            "shadow": shadow, "redecision": redecision_state(db), "paper_only": True}
