"""Setup shadow: every candidate gate value earns a forward track record from the live BUY decisions.

An adopted gate change is judged after ``experiment_verdict_min_trades``
closed paper trades on the new setup (``experiment_engine.review_changes``).
With a handful of trades a week that verdict takes months, and the values the
engine did *not* adopt earn nothing at all.  This module evaluates every
candidate value of every gate template in shadow, from the same decisions the
live setup makes, so a forward record exists for all of them at once.

1. **Record alternative decisions** (``record_cycle_decisions``; called at
   the end of every paper cycle).  For each strategy with a BUY signal (one
   its own gate withheld included), each candidate value of each gate
   template (``experiment_engine.GATE_TEMPLATES``: SPY-trend market gate
   off/10/20/50, VIX below 20/25/30/off, earnings blackout 0/10/20) is
   decided from the states the cycle already loaded: the SPY frame behind the
   entry protections, the process-cached VIX reading, the earnings calendar
   cache.  No broker call, no extra lookup per symbol.  One compact
   ``setup_shadow`` audit row per cycle whose picture changed (symbol,
   strategy, template, candidate, allowed, live allowed, decision price).
2. **Score the hypothetical trades** (``record_scores``; daily after the
   close on the fast learning lane).  Each decision is one hypothetical trade
   per (symbol, strategy, session): entry at the next open, then the
   strategy's tested exits over stored daily bars, costs on both sides, the
   same ``gate_cost.score_trades`` the gate-cost audit uses.  A trade still
   inside its horizon is ``open``.  Scores are ``setup_shadow_score`` rows.
3. **Forward track record per candidate** (``track_records`` /
   ``shadow_track_record``).  Per (strategy, template, candidate): forward
   trades, expectancy (bps, net of the cost model), win rate, and the delta
   against the live value over the overlapping decisions.  The live value is
   scored on the same counterfactual trades, so the delta measures the gate
   decision alone (not fills, not the other gates).
4. **Feed the engine.**  ``review_changes`` adds the alternatives' record to
   each verdict and calls a change ``not_helping`` when an alternative beats
   the adopted value by ``REVERT_EXPECTANCY_BPS`` over ``min_trades``
   overlapping decisions; ``plan_experiments`` treats a candidate that beats
   the live setup (``shadow_lessons``) as a lesson with trigger
   ``shadow_evidence``, replayed and adopted under the engine's unchanged
   pre-registered rule.

Paper only.  Nothing here places, blocks or exits an order; the recorder
reads the cycle's results after the orders phase.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import logging
from typing import Any, Callable, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import AuditLog
from app.services.audit import write_audit_log
from app.services import gate_cost
from app.services.experiment_engine import GATE_TEMPLATES, TEMPLATE_BY_NAME, REVERT_EXPECTANCY_BPS, Template

logger = logging.getLogger(__name__)
UTC = timezone.utc
ET = ZoneInfo("America/New_York")
EVENT = "stock_paper"
ACTION_DECISIONS = "setup_shadow"
ACTION_SCORE = "setup_shadow_score"
TRIGGER = "shadow_evidence"
MAX_STORED_DECISIONS = 3000
LESSON_TEXT = ("In shadow, {template} = {candidate} would have earned {delta:+.1f} bps per trade more than the live "
               "{live} over {overlap} of the same BUY decisions ({trades} forward trades, {expectancy:+.1f} bps).")
PROPOSAL_TEXT = "Replay the strategy with this value against its current setup under the pre-registered rule."


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _bps(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value * 10_000, 1)


# ------------------------------------------------------- the live candidate

def live_candidate(template: Template, parameters: Optional[Mapping]) -> dict:
    """The live value of ``template`` on a strategy row, in the template's candidate shape."""
    params = dict(parameters or {})
    if template.name == "market_gate":
        from app.services.market_gate import gate_window

        return {"market_gate_sma": gate_window(params)}
    if template.name == "vix_gate":
        from app.services.strategies.vix_gate import DEFAULT_LEVELS, DEFAULT_RISE_DAYS, DEFAULT_RISE_PCT, VIX_GATES

        gate = params.get("vix_gate") or None
        if gate not in VIX_GATES:
            return {"vix_gate": None, "vix_level": None}
        try:
            level = float(params.get("vix_level", DEFAULT_LEVELS.get(gate, 0.0)))
        except (TypeError, ValueError):
            level = DEFAULT_LEVELS.get(gate, 0.0)
        out = {"vix_gate": gate, "vix_level": level}
        if gate == "not_rising":
            out.update(vix_rise_pct=float(params.get("vix_rise_pct", DEFAULT_RISE_PCT)),
                       vix_rise_days=int(params.get("vix_rise_days", DEFAULT_RISE_DAYS)))
        return out
    if template.name == "earnings_blackout":
        from app.services.earnings_calendar import strategy_blackout_sessions

        sessions = strategy_blackout_sessions(params)
        if sessions is None:
            sessions = int(settings.paper_earnings_blackout_sessions or 0)
        return {"earnings_blackout_sessions": sessions}
    return {key: params.get(key) for key in template.candidates[0]}


def candidate_label(template: Template, candidate: Mapping) -> str:
    """The template's label, or a generic ``key=value`` one for a value its labeller cannot name."""
    try:
        return template.label(dict(candidate))
    except Exception:
        return ",".join(f"{key}={candidate.get(key)}" for key in template.candidates[0])


def candidates_with_live(template: Template, strategy_type: str, live: Mapping) -> dict[str, dict]:
    """``label -> candidate`` for the template's candidates plus the live value (once)."""
    out = {candidate_label(template, c): dict(c) for c in template.candidates_for(strategy_type)}
    live_label = candidate_label(template, live)
    out.setdefault(live_label, dict(live))
    return out


# ------------------------------------------------------- the cycle's states

class CycleContext:
    """What the cycle already knows, read once: SPY gate states, the VIX reading, the earnings cache."""

    def __init__(self, db: Optional[Session], *, now: datetime, protections: Any = None,
                 spy: Any = None, vix: Optional[Mapping] = None, earnings: Optional[Callable] = None) -> None:
        self.db = db
        self.now = _utc(now)
        self.session = gate_cost.last_completed_session(self.now)
        self.protections = protections
        self._spy = spy
        self._spy_loaded = spy is not None
        self._states: dict[int, dict] = dict(getattr(protections, "market_states", None) or {})
        self._vix = dict(vix) if vix is not None else None
        self._earnings = earnings

    # SPY: the frame the entry protections loaded for the live market gate;
    # loaded once from the trusted daily bars only when no strategy is gated.
    def spy(self):
        if not self._spy_loaded:
            self._spy_loaded = True
            frame = getattr(self.protections, "market_history", None)
            if frame is None and self.db is not None:
                try:
                    from app.services.market_gate import MARKET_SYMBOL
                    from app.services.trusted_data import trusted_history

                    frame = trusted_history(self.db, MARKET_SYMBOL, require_active=False)[0]
                except Exception as exc:  # fail closed for the gated candidates, as live
                    logger.info("setup shadow: SPY history unavailable: %s", exc.__class__.__name__)
                    frame = None
            self._spy = frame
        return self._spy

    def market_state(self, window: int) -> dict:
        from app.services.market_gate import market_state

        window = int(window or 0)
        if window not in self._states:
            self._states[window] = market_state(self.spy(), window)
        return self._states[window]

    def vix(self) -> dict:
        if self._vix is None:
            from app.services.strategies.vix_gate import vix_reading

            try:
                self._vix = vix_reading(self.session.isoformat())
            except Exception as exc:  # the gate fails open live; the shadow does too
                logger.info("setup shadow: VIX reading unavailable: %s", exc.__class__.__name__)
                self._vix = {"vix": None, "vix_date": None, "vix_change_pct": None, "reason": "vix_unavailable"}
        return self._vix

    def earnings_block(self, symbol: str, sessions: int) -> Optional[bool]:
        """True when a report falls inside ``sessions``; None when the calendar is not cached for the symbol."""
        from app.services.earnings_calendar import earnings_blackout, earnings_known

        if self._earnings is not None:
            return self._earnings(symbol, sessions)
        if not earnings_known(symbol):
            return None
        return earnings_blackout(symbol, self.now, sessions=sessions) is not None

    def summary(self) -> dict:
        states = {str(window): {"allowed": state.get("allowed"), "reason": state.get("reason")}
                  for window, state in sorted(self._states.items())}
        return {"session": self.session.isoformat(), "vix": self._vix, "market_states": states}


def vix_allows(reading: Mapping, candidate: Mapping) -> bool:
    """The VIX gate's decision for ``candidate`` from a reading (fail open without a VIX close)."""
    gate = candidate.get("vix_gate")
    if not gate:
        return True
    vix = reading.get("vix")
    if vix is None:
        return True
    level = candidate.get("vix_level")
    if gate == "below":
        return float(vix) < float(level if level is not None else 25.0)
    if gate == "above":
        return float(vix) >= float(level if level is not None else 20.0)
    change = reading.get("vix_change_pct")
    return change is None or float(change) <= float(candidate.get("vix_rise_pct", 10.0))


def decide_candidate(template: Template, candidate: Mapping, *, symbol: str, context: CycleContext) -> Optional[bool]:
    """Would this candidate value have let the BUY through?  ``None`` when the state is unknown."""
    if template.name == "market_gate":
        window = int(candidate.get("market_gate_sma") or 0)
        return True if window <= 0 else bool(context.market_state(window).get("allowed"))
    if template.name == "vix_gate":
        return vix_allows(context.vix(), candidate)
    if template.name == "earnings_blackout":
        sessions = int(candidate.get("earnings_blackout_sessions") or 0)
        if sessions <= 0:
            return True
        blocked = context.earnings_block(symbol, sessions)
        return None if blocked is None else not blocked
    return None


# ---------------------------------------------------------------- recording

def is_buy_decision(result: Mapping) -> bool:
    return result.get("signal_action") == "BUY" or result.get("gated_action") == "BUY"


def cycle_decisions(results: Iterable[Mapping], strategies: Mapping[str, Any], *, context: CycleContext) -> list[dict]:
    """Every BUY decision of a cycle with each template's live value and candidate decisions."""
    decisions = []
    for result in results:
        if not is_buy_decision(result) or not result.get("symbol") or not result.get("strategy"):
            continue
        strategy = strategies.get(result["strategy"])
        if strategy is None:
            continue
        symbol = str(result["symbol"]).upper()
        parameters = getattr(strategy, "parameters", None) or {}
        templates = {}
        for template in GATE_TEMPLATES:
            live = live_candidate(template, parameters)
            live_label = candidate_label(template, live)
            allowed = {label: decide_candidate(template, candidate, symbol=symbol, context=context)
                       for label, candidate in candidates_with_live(template, result["strategy"], live).items()}
            templates[template.name] = {"live": live_label, "live_allowed": allowed[live_label], "candidates": allowed}
        price = result.get("reference_price")
        decisions.append({
            "symbol": symbol, "strategy": str(result["strategy"]), "mode": result.get("mode", "governed"),
            "price": str(price) if price is not None else None,
            "live_status": result.get("status"), "live_reason": result.get("reason"),
            "templates": templates,
        })
    decisions.sort(key=lambda item: (item["symbol"], item["strategy"]))
    return decisions


def _picture(session: str, decisions: Iterable[Mapping]) -> list:
    """What must change for a new row: the session and every decision without its price or live status."""
    return [session, [(d["symbol"], d["strategy"], d["templates"]) for d in decisions]]


def latest_decision_row(db: Session) -> Optional[AuditLog]:
    return db.query(AuditLog).filter(AuditLog.event_type == EVENT, AuditLog.action == ACTION_DECISIONS).order_by(
        AuditLog.id.desc()).first()


def record_cycle_decisions(db: Session, results: Iterable[Mapping], strategies: Mapping[str, Any], *, now: datetime,
                           protections: Any = None, context: Optional[CycleContext] = None) -> Optional[dict]:
    """One ``setup_shadow`` row per cycle whose BUY decisions (or session) differ from the last row's."""
    context = context or CycleContext(db, now=now, protections=protections)
    decisions = cycle_decisions(results, strategies, context=context)
    if not decisions:
        return None
    session = context.session.isoformat()
    previous = latest_decision_row(db)
    if previous is not None:
        before = previous.payload or {}
        if _picture(before.get("session"), before.get("decisions") or []) == _picture(session, decisions):
            return None
    payload = {"observed_at": _utc(now).isoformat(), **context.summary(), "decisions": decisions,
               "templates": [template.name for template in GATE_TEMPLATES], "paper_only": True}
    write_audit_log(
        db, event_type=EVENT, action=ACTION_DECISIONS, status="recorded",
        message=f"setup shadow: {len(decisions)} BUY decisions evaluated under every candidate gate value",
        payload=payload,
    )
    db.commit()
    return payload


# ------------------------------------------------------------------ scoring

def _decision_rows(db: Session, start: datetime, end: datetime) -> list[AuditLog]:
    return db.query(AuditLog).filter(
        AuditLog.event_type == EVENT, AuditLog.action == ACTION_DECISIONS,
        AuditLog.created_at >= start.astimezone(UTC).replace(tzinfo=None),
        AuditLog.created_at <= end.astimezone(UTC).replace(tzinfo=None),
    ).order_by(AuditLog.id).all()


def recorded_decisions(rows: Iterable[AuditLog]) -> list[dict]:
    """One decision per (symbol, strategy, session), the first observation of the session, oldest first."""
    seen: set[tuple] = set()
    out = []
    for row in rows:
        payload = row.payload or {}
        session = payload.get("session")
        if not session:
            observed = payload.get("observed_at")
            try:
                stamp = datetime.fromisoformat(str(observed)) if observed else _utc(row.created_at)
            except ValueError:
                stamp = _utc(row.created_at)
            session = gate_cost.last_completed_session(_utc(stamp)).isoformat()
        for decision in payload.get("decisions") or []:
            if not decision.get("symbol") or not decision.get("strategy") or not decision.get("templates"):
                continue
            key = (str(decision["symbol"]).upper(), str(decision["strategy"]), str(session))
            if key in seen:
                continue
            seen.add(key)
            out.append({"symbol": key[0], "strategy": key[1], "session": key[2], "price": decision.get("price"),
                        "mode": decision.get("mode"), "live_status": decision.get("live_status"),
                        "templates": decision["templates"]})
    return out


SCORE_KEYS = ("status", "why", "entry_date", "exit_date", "entry_price", "exit_price", "barrier", "holding_sessions",
              "gross_return", "net_return", "win", "sessions_elapsed", "entry_source", "exit_plan")


def score_decisions(db: Session, decisions: list[dict], **kwargs) -> list[dict]:
    """Score each decision's hypothetical trade once (``gate_cost.score_trades``) and attach the result."""
    trades = [{"symbol": d["symbol"], "strategy": d["strategy"], "session": d["session"]} for d in decisions]
    scored = gate_cost.score_trades(db, trades, **kwargs) if trades else []
    out = []
    for decision, trade in zip(decisions, scored):
        out.append({**decision, **{key: trade[key] for key in SCORE_KEYS if key in trade}})
    return out


# ------------------------------------------------------------ track record

def _stats(returns: list[float]) -> dict:
    wins = [value for value in returns if value > 0]
    mean = sum(returns) / len(returns) if returns else None
    return {"trades": len(returns), "expectancy_bps": _bps(mean),
            "win_rate": round(len(wins) / len(returns), 4) if returns else None,
            "total_profit_bps": _bps(sum(returns))}


def candidate_record(decisions: Iterable[Mapping], template: str, label: str, live_label: str, *,
                     min_trades: int, margin_bps: float = 0.0) -> dict:
    """One candidate's forward record against the live value, over the decisions where both were evaluated."""
    cand_returns, live_returns, overlap_cand, overlap_live = [], [], [], []
    counts = {"decisions": 0, "allowed": 0, "open": 0, "unknown": 0, "differing": 0}
    for decision in decisions:
        view = (decision.get("templates") or {}).get(template)
        if not view or view.get("live") != live_label:
            continue
        allowed = (view.get("candidates") or {}).get(label)
        live_allowed = view.get("live_allowed")
        if allowed is None:
            counts["unknown"] += 1
            continue
        counts["decisions"] += 1
        status = decision.get("status")
        if allowed:
            counts["allowed"] += 1
            if status == "open":
                counts["open"] += 1
        if status != "scored" or live_allowed is None:
            continue
        net = float(decision.get("net_return") or 0.0)
        if allowed:
            cand_returns.append(net)
        if live_allowed:
            live_returns.append(net)
        if allowed or live_allowed:
            overlap_cand.append(net if allowed else 0.0)
            overlap_live.append(net if live_allowed else 0.0)
            if bool(allowed) != bool(live_allowed):
                counts["differing"] += 1
    candidate = _stats(cand_returns)
    live = _stats(live_returns)
    overlap = len(overlap_cand)
    # A side that took no trade over the overlap earned 0 bps per decision.
    delta_expectancy = None
    if overlap:
        cand_exp = (sum(cand_returns) / len(cand_returns)) if cand_returns else 0.0
        live_exp = (sum(live_returns) / len(live_returns)) if live_returns else 0.0
        delta_expectancy = _bps(cand_exp - live_exp)
    delta_total = _bps(sum(overlap_cand) - sum(overlap_live)) if overlap else None
    beats = bool(overlap >= min_trades and candidate["trades"] >= min_trades and delta_expectancy is not None
                 and delta_expectancy >= margin_bps and (delta_total or 0.0) > 0)
    return {
        "template": template, "candidate": label, "live": live_label, "is_live": label == live_label,
        **counts, **candidate, "live_record": live, "overlap": overlap,
        "delta_expectancy_bps": delta_expectancy, "delta_total_bps": delta_total,
        "beats_live": beats, "min_trades": min_trades, "margin_bps": margin_bps,
        "evidence": ("enough overlapping decisions to compare" if overlap >= min_trades and candidate["trades"] >= min_trades
                     else f"{max(min_trades - overlap, min_trades - candidate['trades'], 0)} more scored decisions before comparing"),
    }


def _current_live(decisions: Iterable[Mapping], template: str) -> Optional[str]:
    """The live label of the latest decision that evaluated ``template``."""
    latest = None
    for decision in decisions:
        view = (decision.get("templates") or {}).get(template)
        if view and view.get("live") is not None:
            if latest is None or str(decision.get("session")) >= str(latest[0]):
                latest = (decision.get("session"), view["live"])
    return latest[1] if latest else None


def track_records(decisions: list[dict], *, min_trades: int, margin_bps: float = 0.0,
                  strategy_type: Optional[str] = None, template: Optional[str] = None) -> list[dict]:
    """Per (strategy, template, candidate) records against the current live value, most promising first."""
    by_strategy: dict[str, list[dict]] = defaultdict(list)
    for decision in decisions:
        if strategy_type is None or decision.get("strategy") == strategy_type:
            by_strategy[decision["strategy"]].append(decision)
    out = []
    for strategy, members in sorted(by_strategy.items()):
        for name in ([template] if template else [t.name for t in GATE_TEMPLATES]):
            live_label = _current_live(members, name)
            if live_label is None:
                continue
            labels: list[str] = []
            for decision in members:
                for label in ((decision.get("templates") or {}).get(name) or {}).get("candidates") or {}:
                    if label not in labels:
                        labels.append(label)
            for label in labels:
                record = candidate_record(members, name, label, live_label, min_trades=min_trades, margin_bps=margin_bps)
                out.append({"strategy_type": strategy, **record})
    out.sort(key=lambda item: (item["strategy_type"], item["template"], item["is_live"],
                               -(item["delta_expectancy_bps"] if item["delta_expectancy_bps"] is not None else -1e9),
                               item["candidate"]))
    return out


# ------------------------------------------------------------------- report

def build_scores(db: Session, *, now: Optional[datetime] = None, sessions: Optional[int] = None,
                 min_trades: Optional[int] = None, **kwargs) -> dict:
    now = _utc(now or datetime.now(UTC))
    sessions = int(sessions if sessions is not None else settings.setup_shadow_lookback_sessions)
    min_trades = int(min_trades if min_trades is not None else settings.experiment_verdict_min_trades)
    as_of = gate_cost.last_completed_session(now)
    first = gate_cost.sessions_back(as_of, max(sessions - 1, 0))
    start = datetime.combine(first, datetime.min.time(), ET)
    decisions = recorded_decisions(_decision_rows(db, start, now))
    scored = score_decisions(db, decisions, **kwargs)
    records = track_records(scored, min_trades=min_trades)
    stored = scored[-MAX_STORED_DECISIONS:]
    return {
        "as_of": as_of.isoformat(), "generated_at": now.isoformat(),
        "window": {"sessions": sessions, "first_session": first.isoformat(), "last_session": as_of.isoformat()},
        "min_trades": min_trades,
        "totals": {"decisions": len(scored), "scored": sum(d.get("status") == "scored" for d in scored),
                   "open": sum(d.get("status") == "open" for d in scored),
                   "unscorable": sum(d.get("status") == "unscorable" for d in scored)},
        "records": records, "decisions": stored, "decisions_truncated": len(scored) - len(stored),
        "method": ("Every BUY decision of the window is one hypothetical trade per (symbol, strategy, session): "
                   "entry at the next session's open, the strategy's tested exits over stored daily bars, costs on "
                   "both sides (gate_cost.score_trades). Each candidate value's record counts the trades it would "
                   "have allowed; the live value is scored on the same trades, so the delta is the gate decision "
                   "alone. Trades still inside their horizon are open, not scored."),
        "paper_only": True, "mutates_strategy_state": False,
    }


def latest_scores(db: Session) -> Optional[dict]:
    row = db.query(AuditLog).filter(AuditLog.event_type == EVENT, AuditLog.action == ACTION_SCORE).order_by(
        AuditLog.id.desc()).first()
    return (row.payload or {}).get("report") if row else None


def record_scores(db: Session, *, now: Optional[datetime] = None, force: bool = False, **kwargs) -> dict:
    """Store one ``setup_shadow_score`` row per completed session (paper only)."""
    now = _utc(now or datetime.now(UTC))
    as_of = gate_cost.last_completed_session(now).isoformat()
    latest = latest_scores(db)
    if latest and latest.get("as_of") == as_of and not force:
        return {"status": "complete", "recorded": False, "as_of": as_of,
                "reason": "setup shadow scores already recorded for this session", "paper_only": True}
    report = build_scores(db, now=now, **kwargs)
    beating = [f"{r['strategy_type']}/{r['template']}={r['candidate']}" for r in report["records"] if r["beats_live"]]
    totals = report["totals"]
    write_audit_log(
        db, event_type=EVENT, action=ACTION_SCORE, status="recorded",
        message=(f"setup shadow through {as_of}: {totals['decisions']} BUY decisions, {totals['scored']} scored, "
                 f"{len(beating)} candidates beating the live setup"),
        payload={"report": report, "paper_only": True},
    )
    db.commit()
    return {"status": "complete", "recorded": True, "as_of": as_of, **totals, "beating": beating, "paper_only": True}


# ---------------------------------------------------------------- the engine

def shadow_track_record(db: Session, strategy_type: str, template: str, *, since: Optional[datetime] = None,
                        min_trades: Optional[int] = None, margin_bps: float = 0.0) -> dict:
    """The stored forward record of every candidate of ``template`` on ``strategy_type`` (decisions since ``since``)."""
    min_trades = int(min_trades if min_trades is not None else settings.experiment_verdict_min_trades)
    latest = latest_scores(db) or {}
    decisions = [d for d in latest.get("decisions") or [] if d.get("strategy") == strategy_type]
    if since is not None:
        first = gate_cost.last_completed_session(_utc(since)).isoformat()
        # The decision on the session of the change may predate it; count from the next session.
        decisions = [d for d in decisions if str(d.get("session")) > first]
    records = track_records(decisions, min_trades=min_trades, margin_bps=margin_bps,
                            strategy_type=strategy_type, template=template)
    live = next((r["live"] for r in records), _current_live(decisions, template))
    return {"strategy_type": strategy_type, "template": template, "live": live, "as_of": latest.get("as_of"),
            "since": _utc(since).isoformat() if since else None, "decisions": len(decisions),
            "min_trades": min_trades, "margin_bps": margin_bps,
            "candidates": [{k: v for k, v in r.items() if k != "strategy_type"} for r in records],
            "beating": [r["candidate"] for r in records if r["beats_live"] and not r["is_live"]]}


def shadow_lessons(db: Session, *, report: Optional[Mapping] = None, min_trades: Optional[int] = None,
                   margin_bps: float = 0.0) -> list[dict]:
    """Lessons for the experiment engine: one per (strategy, template) whose best shadow candidate beats the live value.

    Shaped like ``loss_diagnosis.repeating_failures`` rows with the template
    named, so ``plan_experiments`` replays exactly that template under the
    engine's own rule; the count is the overlapping decisions, so the
    cooldown's "grown by 3" test means three more shared decisions.
    """
    report = report if report is not None else latest_scores(db)
    if not report:
        return []
    min_trades = int(min_trades if min_trades is not None else settings.experiment_verdict_min_trades)
    records = track_records(report.get("decisions") or [], min_trades=min_trades, margin_bps=margin_bps)
    best: dict[tuple[str, str], dict] = {}
    for record in records:
        if not record["beats_live"] or record["is_live"]:
            continue
        key = (record["strategy_type"], record["template"])
        if key not in best or (record["delta_expectancy_bps"] or 0) > (best[key]["delta_expectancy_bps"] or 0):
            best[key] = record
    out = []
    for (strategy_type, template), record in sorted(best.items()):
        out.append({
            "strategy_type": strategy_type, "cause": TRIGGER, "family": "setup_shadow", "template": template,
            "candidate": record["candidate"], "live": record["live"], "count": int(record["overlap"]),
            "trades": int(record["trades"]), "expectancy_bps": record["expectancy_bps"],
            "delta_expectancy_bps": record["delta_expectancy_bps"], "delta_total_bps": record["delta_total_bps"],
            "lesson": LESSON_TEXT.format(template=template, candidate=record["candidate"],
                                         delta=float(record["delta_expectancy_bps"] or 0.0), live=record["live"],
                                         overlap=record["overlap"], trades=record["trades"],
                                         expectancy=float(record["expectancy_bps"] or 0.0)),
            "proposal": PROPOSAL_TEXT, "contributing": {}, "source": "setup_shadow", "as_of": report.get("as_of"),
            "auto_apply": False, "requires_fresh_holdout_and_forward_trial": True,
        })
    out.sort(key=lambda item: (-item["count"], item["strategy_type"], item["template"]))
    return out


# -------------------------------------------------------------- projections

RECORD_KEYS = ("strategy_type", "template", "candidate", "live", "is_live", "decisions", "allowed", "trades", "open",
               "unknown", "differing", "expectancy_bps", "win_rate", "total_profit_bps", "overlap",
               "delta_expectancy_bps", "delta_total_bps", "beats_live", "evidence")


def _compact(records: Iterable[Mapping]) -> list[dict]:
    return [{key: record.get(key) for key in RECORD_KEYS} for record in records]


def setup_shadow_view(db: Session) -> dict:
    """The per-candidate table for ``GET /stock-paper/setup-shadow``."""
    latest = latest_scores(db)
    table = _compact(latest.get("records") or []) if latest else []
    return {
        "as_of": latest.get("as_of") if latest else None, "window": latest.get("window") if latest else None,
        "totals": latest.get("totals") if latest else None, "min_trades": int(settings.experiment_verdict_min_trades),
        "revert_margin_bps": REVERT_EXPECTANCY_BPS, "lookback_sessions": int(settings.setup_shadow_lookback_sessions),
        "records": table, "beating": [r for r in table if r["beats_live"] and not r["is_live"]],
        "templates": [{"name": t.name, "key": t.key, "candidates": [t.label(c) for c in t.candidates]}
                      for t in GATE_TEMPLATES],
        "method": latest.get("method") if latest else None,
        "how_it_feeds_the_engine": (
            "A verdict on an adopted change includes these records; the change is not helping when an alternative "
            f"beats the adopted value by {REVERT_EXPECTANCY_BPS:g} bps per trade over min_trades overlapping "
            "decisions. A candidate that beats the live value becomes a shadow_evidence lesson, replayed and adopted "
            "only under the engine's pre-registered rule."),
        "paper_only": True,
    }


def scoreboard_setup_shadow(db: Session) -> dict:
    """The ``setup_shadow`` section of the learning scoreboard."""
    latest = latest_scores(db)
    if not latest:
        return {"as_of": None, "status": "waiting",
                "detail": "No setup-shadow scores yet; the first run is after the next session's close.",
                "records": [], "beating": []}
    table = _compact(latest.get("records") or [])
    beating = [r for r in table if r["beats_live"] and not r["is_live"]]
    totals = latest.get("totals") or {}
    return {
        "as_of": latest.get("as_of"), "window": latest.get("window"), "min_trades": latest.get("min_trades"),
        "status": "ok",
        "detail": (f"{totals.get('decisions', 0)} BUY decisions in the window, {totals.get('scored', 0)} scored, "
                   f"{totals.get('open', 0)} still open; {len(beating)} candidate values beat the live setup."),
        "totals": totals, "records": table, "beating": beating,
    }
