"""The six frozen forward experiments of the 2026-11-10 local sweep, wired into the paper bot.

``reports/local-sweep-20261110/forward-experiments.md`` pre-registered six
candidates (rules, universes, exits, gates, cost brackets, benchmarks, sample
sizes, review schedule, promotion and stop rules) before any forward sample
existed.  The JSON beside it (``forward-experiments.json``, SHA-256
``SPEC_SHA256``) is the machine-readable half; a byte-identical copy ships in
``app/services/data`` and is verified against the hash on every load, so a
registration can never drift from what was frozen.

1. **Registration** (``register_forward_experiments``; idempotent, run by
   every seed pass).  One ``forward_experiment`` / ``register`` audit row per
   candidate, carrying the candidate verbatim plus the common terms, and the
   strategy row it is bound to:

   * candidates 1, 2, 3 and 5 are ``rule_<id>`` paper rows with status
     ``paper_trading_candidate`` (exploration sizing, exactly as
     ``strategy_discovery.admit_survivors`` creates them), the rule, exits and
     symbol scope copied from the JSON, candidate 3 with ``market_gate_sma:
     50`` and candidate 5 with ``vix_gate: "above", vix_level: 20``.  A row
     that already exists (candidate 1 is a discovery explore candidate) is
     reused after its rule, exits and scope are checked against the JSON;
     a mismatch is reported and nothing is written;
   * candidate 4 is the gated form of discovery's ``rule_4ec6c68526``.  A
     gated row's trades are exactly the ungated row's trades decided with
     VIX >= 20, so when the ungated row exists the candidate is a
     *tag-and-compare* on it (its sample is the trades whose recorded
     ``vix_above_20`` reading is true) rather than a twin row that would
     collide with it on the same symbols; without that row a gated row is
     created;
   * candidate 6 is a tag-and-compare on the bot's own ``ibs_mean_reversion``
     row, which is not changed in any way.

   Each bound row carries ``parameters["forward_experiment"]`` so
   ``strategy_discovery.admit_survivors`` never retires it on a backtest
   re-grade: a registered candidate is governed by its review alone.

2. **Gate readings** (``forward_gate_readings.py``): every paper signal
   records ``vix_above_20`` and ``spy_sma50`` at decision time; the review
   reads them off the entry signal of each trade.

3. **Forward sample** (``candidate_trades``).  A row that places paper
   entries contributes its closed round trips (``trade_attribution``, since
   registration).  A row in ``shadow`` status (signals recorded, no entries:
   IBS since the short-horizon re-verdict) contributes its BUY decisions
   scored as the paper path would have traded them (``gate_cost.score_trade``:
   next open, the row's tested exits over stored daily bars, costs both
   sides), one per symbol and decision bar.  Tag-and-compare candidates keep
   only the trades whose recorded reading is true.

4. **Review** (``build_review`` / ``record_review``).  Per candidate: trades,
   net expectancy and month-clustered t at 10 / 22 / 25 / 50 bps a round
   trip, the realised paper net, and the measured class cost once it has 20
   priced fills; excess over the symbol's causal drift (mean daily return of
   the stored bars up to the decision bar x holding sessions) with its t;
   win rate, mean hold, mean dollars a trade; the equal-weight daily series'
   return, max drawdown and capital in use with the yardstick's passive-arm
   excess and SPY residual; the e-process judge in shadow; progress against
   the checkpoint and the verdict sample; the promotion criteria and the
   stop rule.  Candidates 1-3 are also pooled as one hypothesis (three
   trials).  Candidate 6 shows the VIX >= 20 and VIX < 20 buckets side by
   side with the month-paired difference.  The acting cost is 25 bps a
   round trip unless the measured class cost is higher.

   ``record_review`` appends one ``forward_experiment`` / ``review`` audit row
   (never edits an earlier one); ``review_due`` says when the schedule calls
   for one: quarterly from the first paper trade (never before 2027-02-10),
   at the checkpoint and at the verdict sample.  The markdown table of each
   recorded review is what gets appended to the report's "Reviews" section.

5. **Closure** (``close_candidate`` / ``closures``).  A candidate whose stop
   rule has triggered is closed by the review that records it; an operator
   can close one with a stated reason (``close_candidate_by_number``).  A
   closure is one append-only ``forward_experiment`` / ``close`` audit row,
   and that row is the state: not the strategy's status (a resume rewrites a
   pause) and not the marker in its parameters (an overlay replaces
   parameters wholesale).  It survives a restart and a reseed because nothing
   rewrites audit rows.  There is no reopen.

   * a ``rule_row`` candidate (the row exists to run the experiment) takes no
     new entries from then on: the ledger refuses every buy bound to the row
     (``entry_block_for``, the authority, on every reservation and again at
     dispatch) and the signal cycle records its BUYs as observed
     (``entry_blocks``, one read a cycle).  Nothing is sold: the row stays in
     the cycle so its open lots keep their tested exits, and the weekly
     ``settle_closures`` retires it once it holds none;
   * a ``tag_row`` candidate (a reading on a row that trades on its own
     account) closes its sample only.  The host row is not the experiment's
     to change, and it is released to its normal governance.

   A closed candidate's sample stops at the closure: trades decided before
   it still count when they close, nothing decided after it does.

Paper only.  Nothing here sizes, places or authorises an order; the rows
trade exactly as exploration candidates already do, and a verdict only makes a
candidate eligible for the normal forward-trial promotion gates.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, Strategy, StrategySignal
from app.services.audit import write_audit_log
from app.services.forward_gate_readings import FEATURE_KEY as GATE_READINGS_KEY, readings_of

logger = logging.getLogger(__name__)
UTC = timezone.utc
EVENT = "forward_experiment"
ACTION_REGISTER = "register"
ACTION_REVIEW = "review"
ACTION_CLOSE = "close"
ACTION_ANNUL = "annul"  # reserved: the last close / annul row of a candidate wins; nothing writes one today
CLOSE_KINDS = ("stop_rule", "verdict_failed", "operator_closure")
OPERATOR_CLOSE_KINDS = ("verdict_failed", "operator_closure")
GOVERNANCE_EVENT = "strategy_governance"
RETIRE_ACTION = "retire_closed_forward_experiment"
MARKER_CLOSURE_KEYS = ("closed_at", "closure_kind", "entries_blocked", "released_at")
PARAMETER_KEY = "forward_experiment"
SPEC_FILE = Path(__file__).with_name("data") / "forward-experiments-20261110.json"
SPEC_SHA256 = "0a87603dfa1ba05343d47a000484c2b3ee310d1203c9f0cde814038e1a84b439"
SPEC_REPORT = "reports/local-sweep-20261110/forward-experiments.md"
SPEC_JSON = "reports/local-sweep-20261110/forward-experiments.json"
FROZEN_AT = "2026-11-10"
IBS_SLUG = "ibs_mean_reversion"
RULE_PREFIX = "rule_"
ROUND_TRIP_SOURCES = ("scheduled_round_trip", "exploration_round_trip")
SHADOW_STATUS = "shadow"
HISTORY_BARS = 800

# ---- the terms fixed in forward-experiments.md (verbatim where quoted)
ACTING_COST_BPS = 25.0
COST_BRACKETS_BPS = (10.0, 22.0, 25.0, 50.0)
MEASURED_MIN_FILLS = 20
CHECKPOINT = 100
VERDICT_SAMPLE = {1: 800, 2: 800, 3: 800, 4: 500, 5: 800, 6: 800}
DESIGN_EFFECT = {1: 2.5, 2: 2.5, 3: 2.5, 4: 1.5, 5: 2.5, 6: 2.5}
POOLED = {"name": "weekly_reversal_pooled_1_3", "candidates": (1, 2, 3), "trials_charged": 3,
          "rule": "Pooling 1-3 (one family, three variants) into one hypothesis was decided before data: the pooled "
                  "verdict uses the pooled trades and counts as three trials."}
FIRST_REVIEW_NOT_BEFORE = date(2027, 2, 10)
REVIEW_INTERVAL_DAYS = 91
PROMOTION_T = 2.0
PROMOTION_MIN_POSITIVE_QUARTERS = 0.60
PROMOTION_MAX_DRAWDOWN = 0.15
VETO_FLOOR_BPS = 30.0
VETO_COST_MULTIPLE = 3.0
STOP_MIN_TRADES = 60
STOP_T = -2.0
STOP_MAX_DRAWDOWN = 0.15
CANDIDATE6_MARGIN_BPS = 20.0
CANDIDATE6_YEARS = 2
TERMS = {
    "acting_cost": ("25 bps a round trip (the yardstick's stress bracket), replaced by the measured class cost only "
                    "if the measured cost is higher"),
    "cost_brackets": "10, 22, 25 and 50 bps a round trip, plus the published model, plus the host's measured class "
                     "cost once a class has 20 priced fills",
    "benchmarks": ("(a) the same symbol's drift over the same hold, measured causally (mean daily return up to the "
                   "decision bar x holding days); (b) the volatility-matched SPY/T-bill arm of yardstick.passive_scores "
                   "on the candidate's daily equal-weight return; (c) for the book, the passive VOO/SGOV arm"),
    "statistics": ("Month-clustered t of per-trade net return and of excess over drift; the e-process judge of "
                   "verdict_stats.py recorded in shadow; portfolio max drawdown and capital in use on the equal-weight "
                   "daily series"),
    "review_schedule": ("Quarterly from the first paper trade (first review no earlier than 2027-02-10), and at the "
                        "checkpoint and the verdict sample. Each review is a table appended to this file, never an "
                        "edit of what is above it"),
    "promotion": ("All must hold at the acting cost, at the verdict sample: net expectancy > 0 with clustered t >= 2; "
                  "excess over drift > 0 with t >= 2; passes the yardstick's excess check (t >= 2 on both the "
                  "passive-arm excess and the SPY residual); positive in >= 60% of completed quarters; max drawdown "
                  "of the equal-weight series <= 15%; the short-hold veto's gross >= max(30 bps, 3 x measured round "
                  "trip). A candidate that meets these is eligible for the normal forward-trial promotion gates, not "
                  "for live"),
    "stop_rule": ("A candidate is closed early if, after at least 60 trades, its clustered t of net return at the "
                  "acting cost is <= -2, or the equal-weight series draws down more than 15%"),
    "candidate_6": ("At 800 gated trades (or 2 years, whichever first): promote the gate for a paper forward trial "
                    "only if the gated bucket passes every promotion criterion and beats the ungated bucket by >= 20 "
                    "bps with the paired clustered t >= 2. Otherwise record the result and close"),
    "search_status": ("Strategy discovery's weekly mutation step stays paused while these run "
                      "(strategy_discovery_mutation_enabled off); the library's weekly re-grading may continue. No new "
                      "rule, gate, lookback or universe is to be tested on history against these candidates; the "
                      "trial count the sweep charged them (3,104 configured, 3,086 with trades) is the count they "
                      "carry forward"),
}
GATE_PARAMETERS = {"none": {}, "spy_sma50": {"market_gate_sma": 50}, "vix_above_20": {"vix_gate": "above", "vix_level": 20}}
GATE_READING = {"spy_sma50": "spy_sma50", "vix_above_20": "vix_above_20"}


# ------------------------------------------------------------------ the spec

def spec_sha256(path: Path = SPEC_FILE) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_spec(path: Path = SPEC_FILE, *, expected_sha256: str = SPEC_SHA256) -> dict:
    """The frozen JSON, refused unless its SHA-256 is the pre-registered one."""
    digest = spec_sha256(path)
    if digest != expected_sha256:
        raise ValueError(f"forward-experiments.json SHA-256 {digest} is not the frozen {expected_sha256}")
    spec = json.loads(path.read_text())
    if tuple(float(c) for c in spec.get("cost_brackets_bps_round_trip") or ()) != COST_BRACKETS_BPS:
        raise ValueError("the frozen cost brackets differ from the review's")
    if spec.get("frozen_at") != FROZEN_AT:
        raise ValueError("the frozen date differs from the registration's")
    return spec


def candidate_slug(candidate: Mapping) -> str:
    if candidate.get("hand_written"):
        return str(candidate["hand_written"])
    return RULE_PREFIX + str(candidate["sweep_id"]).split(":", 1)[0]


def rule_parameters(candidate: Mapping) -> dict:
    """The strategy-row parameters a candidate's own rule row carries (``admit_survivors`` layout plus the gate)."""
    exits = candidate["exits"]
    return {"rule": json.loads(json.dumps(candidate["rule"])), "stop_loss_pct": exits["stop_loss_pct"],
            "take_profit_pct": exits["take_profit_pct"], "max_holding_sessions": exits["max_holding_sessions"],
            "symbol_scope": list(candidate["symbols"]), **GATE_PARAMETERS[candidate["gate"]]}


def _exits_of(parameters: Mapping) -> dict:
    return {"stop_loss_pct": parameters.get("stop_loss_pct"), "take_profit_pct": parameters.get("take_profit_pct"),
            "max_holding_sessions": parameters.get("max_holding_sessions")}


def row_mismatch(row: Strategy, candidate: Mapping, *, require_gate: bool) -> Optional[str]:
    """Why an existing row is not the frozen candidate (rule, exits, scope; the gate when ``require_gate``), or None."""
    params = row.parameters or {}
    if candidate.get("rule") is not None and params.get("rule") != candidate["rule"]:
        return "rule differs from the frozen rule"
    exits = _exits_of(params)
    frozen = candidate["exits"]
    for key in ("stop_loss_pct", "take_profit_pct", "max_holding_sessions"):
        if exits[key] is None or not math.isclose(float(exits[key]), float(frozen[key]), abs_tol=1e-9):
            return f"{key} differs from the frozen exit plan"
    if candidate.get("rule") is not None:
        scope = params.get("symbol_scope")
        scope = [s.strip().upper() for s in (scope.split(",") if isinstance(scope, str) else (scope or []))]
        if set(scope) != set(candidate["symbols"]):
            return "symbol scope differs from the frozen universe"
    gate = candidate["gate"]
    if require_gate:
        for key, value in GATE_PARAMETERS[gate].items():
            if params.get(key) != value:
                return f"{key} differs from the frozen gate"
        if gate != "vix_above_20" and params.get("vix_gate"):
            return "row carries a VIX gate the frozen candidate does not"
        if gate != "spy_sma50" and int(params.get("market_gate_sma") or 0) > 0:
            return "row carries a market gate the frozen candidate does not"
    else:  # tag-and-compare: the row must be the ungated rule
        if params.get("vix_gate"):
            return "the row is already VIX-gated; a tag on its trades would be no comparison"
    return None


# ---------------------------------------------------------------- registration

def registrations(db: Session) -> list[dict]:
    """Every ``forward_experiment`` / ``register`` payload, oldest first (one per candidate)."""
    rows = db.scalars(select(AuditLog).where(AuditLog.event_type == EVENT, AuditLog.action == ACTION_REGISTER)
                      .order_by(AuditLog.id)).all()
    out = []
    for row in rows:
        payload = dict(row.payload or {})
        payload.setdefault("audit_id", row.id)
        out.append(payload)
    return out


def _registered_at(payload: Mapping) -> Optional[datetime]:
    value = payload.get("registered_at")
    if not value:
        return None
    stamp = datetime.fromisoformat(str(value))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _bind(db: Session, candidate: Mapping, *, now: datetime) -> tuple[Optional[Strategy], str, bool, Optional[str]]:
    """``(row, mechanism, created, mismatch)`` for a candidate: ``rule_row`` or ``tag_row``."""
    slug = candidate_slug(candidate)
    existing = db.scalars(select(Strategy).where(Strategy.strategy_type == slug).order_by(Strategy.id).limit(1)).first()
    if candidate.get("hand_written"):
        if existing is None:
            return None, "tag_row", False, f"{slug} row is missing; the candidate tags that row's own entries"
        return existing, "tag_row", False, row_mismatch(existing, candidate, require_gate=False)
    if existing is not None:
        if candidate["gate"] == "vix_above_20" and not (existing.parameters or {}).get("vix_gate"):
            # The ungated discovery row: its trades decided with VIX >= 20 are the gated candidate's trades.
            return existing, "tag_row", False, row_mismatch(existing, candidate, require_gate=False)
        return existing, "rule_row", False, row_mismatch(existing, candidate, require_gate=True)
    row = Strategy(
        name=f"Forward experiment {candidate['candidate']}: {candidate['family']} ({candidate['universe']}) "
             f"{candidate['sweep_id'].split(':', 1)[0]}"[:128],
        strategy_type=slug,
        description=f"{candidate['description']}. Frozen forward experiment {candidate['candidate']} of {SPEC_REPORT}; "
                    f"{candidate['note']}."[:1000],
        parameters=rule_parameters(candidate), is_active=True, current_status="paper_trading_candidate")
    db.add(row)
    db.flush()
    return row, "rule_row", True, None


def registration_payload(candidate: Mapping, spec: Mapping, *, row: Strategy, mechanism: str, created: bool,
                         now: datetime, code_commit: Optional[str]) -> dict:
    number = int(candidate["candidate"])
    gate = candidate["gate"]
    return {
        "candidate": number, "sweep_id": candidate["sweep_id"], "strategy_type": row.strategy_type,
        "strategy_id": row.id, "mechanism": mechanism, "row_created": created,
        "row_status_at_registration": row.current_status,
        "tag_reading": GATE_READING.get(gate) if mechanism == "tag_row" and gate != "none" else None,
        "registered_at": now.isoformat(), "code_commit": code_commit,
        "spec": {"report": SPEC_REPORT, "json": SPEC_JSON, "sha256": SPEC_SHA256, "frozen_at": spec["frozen_at"],
                 "sweep_commit": spec.get("sweep_commit"), "source_report": spec.get("source_report")},
        "frozen": json.loads(json.dumps(candidate)),  # the candidate verbatim: rule, exits, gate, symbols, record
        "common": {"execution": spec["execution"], "gate_definitions": spec["gate_definitions"],
                   "benchmark": spec["benchmark"], "cost_brackets_bps_round_trip": list(COST_BRACKETS_BPS),
                   "acting_cost_bps": ACTING_COST_BPS, **TERMS},
        "sample": {"checkpoint": CHECKPOINT, "verdict_sample": VERDICT_SAMPLE[number],
                   "design_effect": DESIGN_EFFECT[number],
                   "expected_trades_per_year": candidate.get("expected_trades_per_year"),
                   "pooled_hypothesis": POOLED["name"] if number in POOLED["candidates"] else None,
                   "trials_charged": POOLED["trials_charged"] if number in POOLED["candidates"] else 1},
        "paper_only": True, "live_authorized": False,
    }


def register_forward_experiments(db: Session, *, now: Optional[datetime] = None,
                                 code_commit: Optional[str] = None) -> dict:
    """Idempotent: bind each frozen candidate to its row once and record the registration verbatim."""
    now = now or datetime.now(UTC)
    spec = load_spec()
    done = {payload["sweep_id"]: payload for payload in registrations(db)}
    registered, created, reused, already, mismatched = [], [], [], [], []
    for candidate in spec["candidates"]:
        sweep_id = candidate["sweep_id"]
        if sweep_id in done:
            already.append(sweep_id)
            continue
        row, mechanism, was_created, mismatch = _bind(db, candidate, now=now)
        if mismatch is not None or row is None:
            mismatched.append({"sweep_id": sweep_id, "candidate": candidate["candidate"], "reason": mismatch})
            logger.warning("forward experiment %s not registered: %s", sweep_id, mismatch)
            continue
        params = dict(row.parameters or {})
        params[PARAMETER_KEY] = {"candidate": candidate["candidate"], "sweep_id": sweep_id, "spec_sha256": SPEC_SHA256,
                                 "registered_at": now.isoformat(), "mechanism": mechanism}
        row.parameters = params
        payload = registration_payload(candidate, spec, row=row, mechanism=mechanism, created=was_created, now=now,
                                       code_commit=code_commit)
        write_audit_log(db, event_type=EVENT, entity_type="strategy", entity_id=row.id, action=ACTION_REGISTER,
                        status="registered",
                        message=(f"forward experiment {candidate['candidate']} ({sweep_id}) registered on "
                                 f"{row.strategy_type} as {mechanism}; verdict sample {VERDICT_SAMPLE[int(candidate['candidate'])]}"),
                        payload=payload)
        registered.append(sweep_id)
        (created if was_created else reused).append(row.strategy_type)
    if registered:
        db.flush()
    return {"registered": registered, "rows_created": created, "rows_reused": reused, "already_registered": already,
            "mismatched": mismatched, "spec_sha256": SPEC_SHA256, "paper_only": True}


def is_registered_row(parameters: Optional[Mapping]) -> bool:
    """A row bound to a frozen forward experiment (never retired by the discovery re-grade).

    A row whose candidates are all closed is released once it no longer needs
    the protection: a tag row at its closure, a rule row when it is retired
    flat (until then a re-grade must not take it out of the cycle while its
    open lots still need their exits).
    """
    marker = (parameters or {}).get(PARAMETER_KEY)
    return isinstance(marker, Mapping) and not marker.get("released_at")


# ------------------------------------------------------------------ closures

class ForwardExperimentError(ValueError):
    """A closure request that cannot be recorded (unknown candidate, bad reason)."""


def closures(db: Session) -> dict[str, dict]:
    """Every candidate that is closed now, by sweep id (the last close / annul row of a candidate wins)."""
    rows = db.execute(select(AuditLog.id, AuditLog.action, AuditLog.payload)
                      .where(AuditLog.event_type == EVENT, AuditLog.action.in_((ACTION_CLOSE, ACTION_ANNUL)))
                      .order_by(AuditLog.id)).all()
    return _closure_state(rows)


def _closure_state(rows: Iterable) -> dict[str, dict]:
    state: dict[str, dict] = {}
    for audit_id, action, payload in rows:
        payload = dict(payload or {})
        sweep_id = payload.get("sweep_id")
        if not sweep_id:
            continue
        if action == ACTION_CLOSE:
            payload.setdefault("audit_id", audit_id)
            state[str(sweep_id)] = payload
        else:
            state.pop(str(sweep_id), None)
    return state


def _closed_at(closure: Optional[Mapping]) -> Optional[datetime]:
    value = (closure or {}).get("closed_at")
    if not value:
        return None
    stamp = datetime.fromisoformat(str(value))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _kind_text(kind: Any) -> str:
    return {"stop_rule": "stop rule", "verdict_failed": "verdict failed", "operator_closure": "operator closure"}.get(
        str(kind), str(kind))


def block_reason(closure: Mapping) -> str:
    return (f"forward experiment {closure.get('candidate')} is closed ({_kind_text(closure.get('reason_kind'))}, "
            f"{str(closure.get('closed_at'))[:10]}): no new entries; open lots keep their exits")


def entry_blocks(db: Session) -> dict[int, str]:
    """``{strategy_id: reason}`` for every row a closure stops entering (one read; the signal cycle's view)."""
    out: dict[int, str] = {}
    for closure in closures(db).values():
        if closure.get("entries_blocked") and closure.get("strategy_id") is not None:
            out.setdefault(int(closure["strategy_id"]), block_reason(closure))
    return out


def entry_block_for(db: Session, strategy_id: Optional[int]) -> Optional[str]:
    """Why a buy bound to this strategy is refused, read fresh from the audit rows (the ledger's authority).

    Keyed on the strategy id of the close row, never on the row's status or
    parameters, so a resume, a re-admission or a parameter overlay cannot
    reopen a closed candidate.
    """
    if strategy_id is None:
        return None
    rows = db.execute(select(AuditLog.id, AuditLog.action, AuditLog.payload)
                      .where(AuditLog.event_type == EVENT, AuditLog.entity_type == "strategy",
                             AuditLog.entity_id == int(strategy_id),
                             AuditLog.action.in_((ACTION_CLOSE, ACTION_ANNUL)))
                      .order_by(AuditLog.id)).all()
    for closure in _closure_state(rows).values():
        if closure.get("entries_blocked"):
            return block_reason(closure)
    return None


def open_lots(db: Session, row: Strategy) -> dict:
    """The row's own open paper lots and entries still in flight: ``{"lots": [...], "pending_entries": n}``."""
    from app.models.stock_paper import StockPaperOrder, StockPaperPosition
    from app.services.stock_paper_autotrader import _strategy_lots
    from app.services.stock_paper_ledger import NONTERMINAL_ORDER_STATUSES

    pending = db.query(StockPaperOrder.id).filter(
        StockPaperOrder.strategy_id == row.id, StockPaperOrder.side == "buy",
        StockPaperOrder.status.in_(NONTERMINAL_ORDER_STATUSES | {"uncertain_submission"})).count()
    symbols = sorted({symbol for (symbol,) in db.query(StockPaperOrder.symbol).filter(
        StockPaperOrder.strategy_id == row.id, StockPaperOrder.side == "buy").distinct()})
    lots: list[dict] = []
    if symbols:
        type_by_id = {sid: slug for sid, slug in db.query(Strategy.id, Strategy.strategy_type).all()}
        held = db.query(StockPaperPosition).filter(StockPaperPosition.symbol.in_(symbols),
                                                   StockPaperPosition.quantity > 0).all()
        for position in held:
            quantity = _strategy_lots(db, position.account_id, position.symbol, type_by_id)["holdings"].get(
                row.strategy_type)
            if quantity is not None and quantity > 0:
                lots.append({"symbol": position.symbol, "quantity": str(quantity)})
    return {"lots": sorted(lots, key=lambda lot: lot["symbol"]), "pending_entries": int(pending)}


def _open_lots_or_unknown(db: Session, row: Optional[Strategy]) -> Optional[dict]:
    if row is None:
        return {"lots": [], "pending_entries": 0}
    try:
        return open_lots(db, row)
    except Exception as exc:  # a read that fails must never look like "flat"
        logger.warning("forward experiments: open lots of %s unreadable: %s", row.strategy_type, exc.__class__.__name__)
        return None


def closure_summary(closure: Optional[Mapping]) -> Optional[dict]:
    if not closure:
        return None
    return {key: closure.get(key) for key in ("candidate", "sweep_id", "strategy_id", "strategy_type", "mechanism",
                                              "reason_kind", "reasons", "closed_at", "closed_by", "entries_blocked",
                                              "review_number", "audit_id")}


def _closure_terms(registration: Mapping, *, reason_kind: str, reasons: Iterable[str], now: datetime, actor: str,
                   review_number: Optional[int]) -> dict:
    mechanism = registration.get("mechanism")
    blocked = mechanism == "rule_row"
    return {
        "candidate": int(registration["candidate"]), "sweep_id": registration["sweep_id"],
        "strategy_id": registration.get("strategy_id"), "strategy_type": registration.get("strategy_type"),
        "mechanism": mechanism, "tag_reading": registration.get("tag_reading"),
        "reason_kind": reason_kind, "reasons": [str(reason) for reason in reasons], "closed_at": now.isoformat(),
        "closed_by": actor, "review_number": review_number, "entries_blocked": blocked,
        "enforcement": ("no new entries on the row: the ledger refuses every buy bound to it and the signal cycle "
                        "records its BUYs as observed; open lots keep their tested exits and the row is retired once "
                        "it holds none" if blocked else
                        "tag-and-compare: the sample is closed; the host row trades on its own account and is not "
                        "changed"),
        "sample": "trades decided before the closure count when they close; nothing decided after it does",
        "reopen": "none", "spec_sha256": SPEC_SHA256, "paper_only": True, "live_authorized": False,
    }


def close_candidate(db: Session, registration: Mapping, *, reason_kind: str, reasons: Iterable[str],
                    now: Optional[datetime] = None, actor: str = "system", review_number: Optional[int] = None,
                    review_audit_id: Optional[int] = None) -> dict:
    """Close one registered candidate once: the audit row, the marker mirror and a notification."""
    from app.services.audit import lock_audit_chain
    from app.services.notifications import create_notification

    if reason_kind not in CLOSE_KINDS:
        raise ForwardExperimentError(f"reason_kind must be one of {', '.join(CLOSE_KINDS)}")
    reasons = [str(reason).strip() for reason in reasons if str(reason).strip()]
    if not reasons:
        raise ForwardExperimentError("a closure needs a stated reason")
    now = now or datetime.now(UTC)
    # Two closers (the weekly review and an operator) must not both write: take
    # the audit writer's lock first, then look.
    lock_audit_chain(db)
    db.flush()
    existing = closures(db).get(str(registration["sweep_id"]))
    if existing is not None:
        return {"status": "already_closed", "closure": closure_summary(existing)}
    payload = _closure_terms(registration, reason_kind=reason_kind, reasons=reasons, now=now, actor=actor,
                             review_number=review_number)
    strategy_id = payload["strategy_id"]
    row = db.get(Strategy, int(strategy_id)) if strategy_id is not None else None
    payload["review_audit_id"] = review_audit_id
    payload["row_status_at_closure"] = row.current_status if row is not None else None
    payload["open_at_closure"] = _open_lots_or_unknown(db, row) if payload["entries_blocked"] else None
    audit = write_audit_log(
        db, event_type=EVENT, entity_type="strategy", entity_id=int(strategy_id) if strategy_id is not None else None,
        action=ACTION_CLOSE, status="closed",
        message=(f"forward experiment {payload['candidate']} ({payload['sweep_id']}) closed: "
                 f"{_kind_text(reason_kind)}; {'; '.join(reasons)}"[:1000]),
        payload=payload)
    db.flush()
    sync_closure_markers(db)
    create_notification(
        db, category="strategy_governance", severity="warning", source="forward_experiments",
        title=f"Forward experiment {payload['candidate']} closed",
        message=(f"{payload['strategy_type']}: {_kind_text(reason_kind)} ({'; '.join(reasons)}). "
                 + ("New entries are off; open lots keep their exits." if payload["entries_blocked"]
                    else "The sample is closed; the host row is unchanged."))[:1000],
        entity_type="strategy", entity_id=int(strategy_id) if strategy_id is not None else None,
        payload=closure_summary({**payload, "audit_id": audit.id}))
    return {"status": "closed", "closure": closure_summary({**payload, "audit_id": audit.id})}


def close_candidate_by_number(db: Session, candidate: int, *, reason: str, reason_kind: str = "operator_closure",
                              actor: str = "operator", now: Optional[datetime] = None) -> dict:
    """An operator's closure of one candidate, with its reason on the record (the stop rule closes at the review)."""
    if reason_kind not in OPERATOR_CLOSE_KINDS:
        raise ForwardExperimentError(f"reason_kind must be one of {', '.join(OPERATOR_CLOSE_KINDS)}")
    if len((reason or "").strip()) < 10:
        raise ForwardExperimentError("a closure needs a stated reason of at least 10 characters")
    registration = next((r for r in registrations(db) if int(r["candidate"]) == int(candidate)), None)
    if registration is None:
        raise ForwardExperimentError(f"forward experiment {candidate} is not registered")
    return close_candidate(db, registration, reason_kind=reason_kind, reasons=[reason.strip()], now=now, actor=actor)


def sync_closure_markers(db: Session) -> list[str]:
    """Mirror the closures onto the bound rows' markers; the audit rows stay the state.  Idempotent."""
    closed = closures(db)
    by_row: dict[int, list[dict]] = {}
    for registration in registrations(db):
        if registration.get("strategy_id") is not None:
            by_row.setdefault(int(registration["strategy_id"]), []).append(registration)
    changed = []
    for strategy_id, bound in by_row.items():
        row = db.get(Strategy, strategy_id)
        marker = (row.parameters or {}).get(PARAMETER_KEY) if row is not None else None
        if not isinstance(marker, Mapping):
            continue
        want = {key: value for key, value in marker.items() if key not in MARKER_CLOSURE_KEYS}
        mine = [closed.get(str(registration.get("sweep_id"))) for registration in bound]
        if all(mine):  # a row is closed only when every candidate bound to it is
            last = max(mine, key=lambda closure: str(closure.get("closed_at")))
            blocked = any(closure.get("entries_blocked") for closure in mine)
            want.update({"closed_at": last.get("closed_at"), "closure_kind": last.get("reason_kind"),
                         "entries_blocked": blocked})
            if not blocked or row.current_status == "retired":
                want["released_at"] = last.get("closed_at")
        if want != dict(marker):
            params = dict(row.parameters or {})
            params[PARAMETER_KEY] = want
            row.parameters = params
            changed.append(row.strategy_type)
    if changed:
        db.flush()
    return changed


def settle_closures(db: Session, *, now: Optional[datetime] = None) -> dict:
    """Retire each closed rule row once it holds no lot and has no entry in flight (audited, once)."""
    now = now or datetime.now(UTC)
    closed = closures(db)
    open_candidates: dict[int, int] = {}
    for registration in registrations(db):
        if registration.get("strategy_id") is not None and str(registration.get("sweep_id")) not in closed:
            sid = int(registration["strategy_id"])
            open_candidates[sid] = open_candidates.get(sid, 0) + 1
    retired, winding_down, seen = [], [], set()
    for closure in closed.values():
        strategy_id = closure.get("strategy_id")
        if not closure.get("entries_blocked") or strategy_id is None or int(strategy_id) in seen:
            continue
        seen.add(int(strategy_id))
        row = db.get(Strategy, int(strategy_id))
        if row is None or row.current_status == "retired" or open_candidates.get(int(strategy_id)):
            continue
        held = _open_lots_or_unknown(db, row)
        if held is None or held["lots"] or held["pending_entries"]:
            winding_down.append({"candidate": closure.get("candidate"), "strategy_type": row.strategy_type,
                                 "open": held})
            continue
        old_status, row.current_status = row.current_status, "retired"
        write_audit_log(
            db, event_type=GOVERNANCE_EVENT, entity_type="strategy", entity_id=row.id, action=RETIRE_ACTION,
            status="complete",
            message=(f"forward experiment {closure.get('candidate')} was closed on "
                     f"{str(closure.get('closed_at'))[:10]} and {row.strategy_type} holds no lot; retired."),
            payload={"old_status": old_status, "new_status": "retired", "candidate": closure.get("candidate"),
                     "sweep_id": closure.get("sweep_id"), "closure_audit_id": closure.get("audit_id"),
                     "retired_at": now.isoformat(), "paper_only": True})
        retired.append(row.strategy_type)
    if retired:
        db.flush()
    return {"retired": retired, "winding_down": winding_down, "markers": sync_closure_markers(db)}


# ------------------------------------------------------------- forward sample

def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _f(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _session(moment: datetime) -> date:
    from app.services.trade_attribution import session_date

    return session_date(moment)


def _decision_date_of(entry_session: date) -> str:
    from app.services.gate_cost import sessions_back

    return sessions_back(entry_session, 1).isoformat()


def load_frame(db: Session, symbol: str, bars: int = HISTORY_BARS) -> Optional[pd.DataFrame]:
    """Stored completed daily bars (``trusted_history``), normalised; None when unavailable."""
    from app.services.gate_cost import normalise_frame
    from app.services.trusted_data import UntrustedMarketData, trusted_history

    try:
        prices, _source = trusted_history(db, symbol, limit=bars, require_active=False)
    except UntrustedMarketData:
        return None
    except Exception as exc:
        db.rollback()
        logger.warning("forward experiments: daily bars unavailable for %s: %s", symbol, exc)
        return None
    return normalise_frame(prices)


def _frame_cache(db: Session, loader: Optional[Callable]) -> Callable[[str], Optional[pd.DataFrame]]:
    cache: dict[str, Optional[pd.DataFrame]] = {}
    load = loader or (lambda symbol: load_frame(db, symbol))

    def get(symbol: str) -> Optional[pd.DataFrame]:
        if symbol not in cache:
            cache[symbol] = load(symbol)
        return cache[symbol]

    return get


def _signal_readings(db: Session, signal_ids: Iterable[int]) -> dict[int, Optional[dict]]:
    ids = sorted({int(i) for i in signal_ids if i})
    if not ids:
        return {}
    rows = db.scalars(select(StrategySignal).where(StrategySignal.id.in_(ids))).all()
    return {row.id: readings_of(row.features) for row in rows}


def _signal_times(db: Session, signal_ids: Iterable[int]) -> dict[int, datetime]:
    ids = sorted({int(i) for i in signal_ids if i})
    if not ids:
        return {}
    return {signal_id: _utc(when) for signal_id, when in db.execute(
        select(StrategySignal.id, StrategySignal.signal_time).where(StrategySignal.id.in_(ids))).all() if when}


def round_trip_trades(db: Session, strategy_type: str, *, since: datetime,
                      until: Optional[datetime] = None) -> list[dict]:
    """Closed paper round trips of the row since ``since`` (entry decision on or after it), as trade records.

    ``until`` is a candidate's closure: a trip decided before it counts when
    it closes (an entry already at the broker fills and is the candidate's),
    one decided after it is not the candidate's trade.
    """
    from app.services.trade_attribution import current_attributions

    rows = [row for row in current_attributions(db, since=since, sources=ROUND_TRIP_SOURCES,
                                                strategy_types=[strategy_type])
            if _utc(row.opened_at) >= since]
    lineage = {row.id: ((row.detail or {}).get("lineage") or {}).get("signal_id") for row in rows}
    if until is not None:
        decided = _signal_times(db, lineage.values())
        rows = [row for row in rows if decided.get(lineage[row.id] or 0, _utc(row.opened_at)) < until]
    readings = _signal_readings(db, lineage.values())
    out = []
    for row in sorted(rows, key=lambda r: (_utc(r.opened_at), r.id)):
        detail = row.detail or {}
        entry_session, exit_session = _session(_utc(row.opened_at)), _session(_utc(row.closed_at))
        slippage = detail.get("slippage") or {}
        holding = detail.get("holding") or {}
        net = _f(row.net_return) if row.costs_known else None
        pnl = _f(row.net_pnl)
        reading = readings.get(lineage[row.id]) or {}
        out.append({
            "source": "round_trip", "attribution_id": row.id, "symbol": row.symbol, "signal_id": lineage[row.id],
            "decision_date": _decision_date_of(entry_session), "entry_date": entry_session.isoformat(),
            "exit_date": exit_session.isoformat(), "hold_sessions": int(holding.get("sessions") or 0),
            "gross_return": _f(row.gross_return), "net_return": net, "costs_known": bool(row.costs_known),
            "exit_cause": row.exit_cause, "entry_price": _f(slippage.get("entry_price")),
            "exit_price": _f(slippage.get("exit_price")),
            "notional": abs(pnl / net) if pnl is not None and net not in (None, 0.0) else None,
            "vix_above_20": reading.get("vix_above_20"), "spy_sma50": reading.get("spy_sma50"),
            "readings_recorded": bool(reading),
        })
    return out


def _is_buy_decision(signal: StrategySignal) -> bool:
    features = signal.features or {}
    if signal.action == "BUY":
        return True
    timing = features.get("entry_timing")
    return isinstance(timing, Mapping) and timing.get("withheld_action") == "BUY"


def shadow_signal_trades(db: Session, row: Strategy, *, since: datetime, now: datetime,
                         loader: Optional[Callable] = None, until: Optional[datetime] = None) -> list[dict]:
    """The row's BUY decisions since ``since``, scored as the paper path would have traded them.

    One hypothetical trade per (symbol, decision bar): entry at the next
    open, the row's tested exits over stored daily bars, the cost model on
    both sides (``gate_cost.score_trade``).  A decision whose horizon has not
    passed is ``open`` and not a trade yet.  A BUY the row's own gate turned
    into a HOLD is not a decision.
    """
    from app.services import meta_label_primary as primary
    from app.services.gate_cost import last_completed_session, score_trade
    from app.services.strategies import rules as rule_language
    from app.services.strategy_execution_policy import exit_policy_for

    query = select(StrategySignal).where(StrategySignal.strategy_id == row.id,
                                         StrategySignal.signal_time >= since.replace(tzinfo=None))
    if until is not None:  # a closed candidate's sample ends at its closure
        query = query.where(StrategySignal.signal_time < until.astimezone(UTC).replace(tzinfo=None))
    signals = db.scalars(query.order_by(StrategySignal.signal_time, StrategySignal.id)).all()
    decisions: dict[tuple[str, str], StrategySignal] = {}
    for signal in signals:
        if not _is_buy_decision(signal):
            continue
        reading = readings_of(signal.features) or {}
        bar = reading.get("decision_bar") or last_completed_session(_utc(signal.signal_time)).isoformat()
        decisions.setdefault((signal.symbol.upper(), str(bar)), signal)
    if not decisions:
        return []
    policy = exit_policy_for(row)
    rule = (row.parameters or {}).get("rule") if row.strategy_type.startswith(RULE_PREFIX) else None
    frames = _frame_cache(db, loader)
    exits: dict[str, Optional[np.ndarray]] = {}
    out, open_, unscorable = [], 0, 0
    for (symbol, bar), signal in sorted(decisions.items(), key=lambda item: (item[0][1], item[0][0])):
        frame = frames(symbol)
        if frame is not None and symbol not in exits:
            try:
                if rule is not None:
                    exits[symbol] = np.asarray(rule_language.exit_signals(frame, rule), dtype=bool)
                else:
                    replayed = primary.replay_exit_signals(frame, row.strategy_type)
                    exits[symbol] = None if replayed is None else np.asarray(replayed, dtype=bool)
            except Exception as exc:
                logger.info("forward experiments: indicator exit unavailable for %s: %s", symbol, exc)
                exits[symbol] = None
        scored = score_trade({"symbol": symbol, "strategy": row.strategy_type, "session": bar}, frame, policy,
                             exit_signals=exits.get(symbol))
        if scored.get("status") != "scored":
            open_ += scored.get("status") == "open"
            unscorable += scored.get("status") == "unscorable"
            continue
        reading = readings_of(signal.features) or {}
        entry, exit_ = date.fromisoformat(scored["entry_date"]), date.fromisoformat(scored["exit_date"])
        if exit_ > now.astimezone(UTC).date():
            open_ += 1
            continue
        out.append({
            "source": "shadow_signal", "signal_id": signal.id, "symbol": symbol, "decision_date": bar,
            "entry_date": entry.isoformat(), "exit_date": exit_.isoformat(),
            "hold_sessions": int(scored["holding_sessions"]), "gross_return": float(scored["gross_return"]),
            "net_return": float(scored["net_return"]), "costs_known": False, "exit_cause": scored["barrier"],
            "entry_price": float(scored["entry_price"]), "exit_price": float(scored["exit_price"]), "notional": None,
            "vix_above_20": reading.get("vix_above_20"), "spy_sma50": reading.get("spy_sma50"),
            "readings_recorded": bool(reading),
        })
    out.append({"_decisions": len(decisions), "_open": open_, "_unscorable": unscorable})
    return out


def _split_meta(trades: list[dict]) -> tuple[list[dict], dict]:
    meta = {"decisions": None, "open": 0, "unscorable": 0}
    clean = []
    for trade in trades:
        if "_decisions" in trade:
            meta = {"decisions": trade["_decisions"], "open": trade["_open"], "unscorable": trade["_unscorable"]}
        else:
            clean.append(trade)
    return clean, meta


def history_vix_tag(decision_date: str) -> Optional[bool]:
    """``vix_above_20`` from the VIX history for a decision bar (None when unavailable)."""
    from app.services.forward_gate_readings import vix_above_20
    from app.services.strategies.vix_gate import vix_reading

    try:
        return vix_above_20(vix_reading(decision_date))["value"]
    except Exception:
        return None


def candidate_trades(db: Session, registration: Mapping, *, now: datetime,
                     loader: Optional[Callable] = None, closure: Optional[Mapping] = None) -> dict:
    """The candidate's forward sample: ``{"trades", "excluded", "sample_source", "coverage"}``."""
    row = db.get(Strategy, int(registration["strategy_id"]))
    since = _registered_at(registration) or now
    until = _closed_at(closure)
    if row is None:
        return {"trades": [], "excluded": [], "sample_source": "missing_row", "coverage": {}, "row_status": None}
    if row.current_status == SHADOW_STATUS:
        trades, meta = _split_meta(shadow_signal_trades(db, row, since=since, now=now, loader=loader, until=until))
        source = "shadow_signals_scored"
    else:
        trades, meta = (round_trip_trades(db, row.strategy_type, since=since, until=until),
                        {"decisions": None, "open": 0, "unscorable": 0})
        source = "closed_round_trips"
    tag = registration.get("tag_reading")
    excluded: list[dict] = []
    if tag:
        kept = []
        disagreements = 0
        for trade in trades:
            recorded = trade.get(tag)
            if tag == "vix_above_20":
                history = history_vix_tag(trade["decision_date"])
                if recorded is None:
                    recorded = history  # a signal recorded before the readings existed
                elif history is not None and history != recorded:
                    disagreements += 1
                trade["vix_history_tag"] = history
            if recorded is True:
                kept.append(trade)
            else:
                excluded.append({**trade, "excluded_because": f"{tag} is {recorded}"})
        meta["reading_disagreements"] = disagreements
        trades = kept
    return {"trades": trades, "excluded": excluded, "sample_source": source, "coverage": meta,
            "row_status": row.current_status, "strategy_type": row.strategy_type}


# ------------------------------------------------------------- review maths

def _months(trades: Iterable[Mapping]) -> list[str]:
    return [str(t["decision_date"])[:7] for t in trades]


def net_at(trade: Mapping, cost_bps: Optional[float]) -> Optional[float]:
    """Net return at a round-trip cost bracket (gross minus the bracket); ``None`` cost = the realised paper net."""
    if cost_bps is None:
        return trade.get("net_return") if trade.get("costs_known") else None
    gross = trade.get("gross_return")
    return None if gross is None else float(gross) - float(cost_bps) / 10_000.0


def drift_per_trade(trade: Mapping, frame: Optional[pd.DataFrame]) -> Optional[float]:
    """The symbol's causal drift over the hold: mean daily return of the stored bars up to the decision bar x sessions."""
    if frame is None or not len(frame):
        return None
    dates = frame["date"].astype(str).to_numpy()
    upto = np.flatnonzero(dates <= str(trade["decision_date"]))
    if len(upto) < 21:
        return None
    close = pd.to_numeric(frame["close"], errors="coerce").to_numpy(float)[: int(upto[-1]) + 1]
    daily = close[1:] / close[:-1] - 1.0
    daily = daily[np.isfinite(daily)]
    if not len(daily):
        return None
    return float(np.mean(daily)) * max(int(trade.get("hold_sessions") or 0), 1)


def clustered(values: list[float], clusters: list[str]) -> dict:
    """Mean (bps) and month-clustered t (CR0, ``verdict_stats.paired_stats``) of per-trade fractions."""
    from app.services.yardstick import clustered_t

    if not values:
        return {"trades": 0, "mean_bps": None, "t_stat": None, "clusters": 0}
    stats = clustered_t(np.asarray(values, dtype=float), clusters)
    return {"trades": int(stats.get("observations") or 0), "mean_bps": stats.get("mean_bps"),
            "t_stat": stats.get("t_stat"), "clusters": stats.get("clusters")}


def bucket_stats(trades: list[dict], *, cost_bps: Optional[float], frames: Optional[Callable] = None) -> dict:
    """Expectancy, clustered t, excess over drift, win rate and hold of a bucket at one cost."""
    values, months, excess, excess_months, wins, holds = [], [], [], [], 0, []
    priced = 0
    for trade in trades:
        net = net_at(trade, cost_bps)
        if net is None:
            continue
        priced += 1
        values.append(net)
        months.append(str(trade["decision_date"])[:7])
        wins += net > 0
        holds.append(int(trade.get("hold_sessions") or 0))
        drift = drift_per_trade(trade, frames(trade["symbol"])) if frames is not None else None
        if drift is not None:
            excess.append(net - drift)
            excess_months.append(str(trade["decision_date"])[:7])
    net_stats = clustered(values, months)
    excess_stats = clustered(excess, excess_months)
    return {"cost_bps": cost_bps, "trades": priced, "expectancy_bps": net_stats["mean_bps"],
            "clustered_t": net_stats["t_stat"], "months": net_stats["clusters"],
            "excess_over_drift_bps": excess_stats["mean_bps"], "excess_t": excess_stats["t_stat"],
            "excess_trades": excess_stats["trades"],
            "win_rate": round(wins / priced, 4) if priced else None,
            "mean_hold_sessions": round(float(np.mean(holds)), 2) if holds else None,
            "total_net_bps": round(float(np.sum(values)) * 10_000, 1) if values else None}


def quarter_record(trades: list[dict], *, cost_bps: float, now: datetime) -> dict:
    """Net per calendar quarter of the decision date at the acting cost; a quarter counts once it has ended."""
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for trade in trades:
        net = net_at(trade, cost_bps)
        if net is None:
            continue
        stamp = pd.Timestamp(str(trade["decision_date"]))
        key = f"{stamp.year}Q{(stamp.month - 1) // 3 + 1}"
        sums[key] = sums.get(key, 0.0) + net
        counts[key] = counts.get(key, 0) + 1
    today = pd.Timestamp(now.astimezone(UTC).date())
    quarters = []
    for key in sorted(sums):
        year, q = int(key[:4]), int(key[-1])
        end = pd.Timestamp(year=year, month=3 * q, day=1) + pd.offsets.MonthEnd(0)
        quarters.append({"quarter": key, "trades": counts[key], "net_bps": round(sums[key] * 10_000, 1),
                         "completed": bool(end < today), "positive": bool(sums[key] > 0)})
    completed = [q for q in quarters if q["completed"]]
    positive = sum(1 for q in completed if q["positive"])
    return {"quarters": quarters, "completed": len(completed), "positive": positive,
            "positive_fraction": round(positive / len(completed), 4) if completed else None}


def equal_weight_series(trades: list[dict], frames: Callable, symbols: Iterable[str], *, cost_bps: float) -> dict:
    """Equal-weight daily return across the candidate's universe (0 when flat), its max drawdown and capital in use.

    Entry day: close over the entry price; held days: close to close; exit
    day: the cost-inclusive exit price over the prior close.  The round-trip
    cost is charged on the exit day.
    """
    names = sorted(set(str(s).upper() for s in symbols) | {str(t["symbol"]).upper() for t in trades})
    series, held, used = [], [], 0
    for name in names:
        frame = frames(name)
        if frame is None or not len(frame):
            continue
        dates = frame["date"].astype(str).to_numpy()
        close = pd.to_numeric(frame["close"], errors="coerce").to_numpy(float)
        returns, holding = np.zeros(len(dates)), np.zeros(len(dates), dtype=bool)
        for trade in trades:
            if str(trade["symbol"]).upper() != name or not trade.get("entry_price") or not trade.get("exit_price"):
                continue
            i = np.searchsorted(dates, str(trade["entry_date"]))
            j = np.searchsorted(dates, str(trade["exit_date"]))
            if i >= len(dates) or j >= len(dates) or dates[i] != str(trade["entry_date"]) or j < i:
                continue
            entry, exit_ = float(trade["entry_price"]), float(trade["exit_price"]) * (1.0 - cost_bps / 10_000.0)
            used += 1
            holding[i:j + 1] = True
            if i == j:
                returns[i] += exit_ / entry - 1.0
                continue
            returns[i] += close[i] / entry - 1.0
            if j - i > 1:
                returns[i + 1:j] += close[i + 1:j] / close[i:j - 1] - 1.0
            returns[j] += exit_ / close[j - 1] - 1.0
        index = pd.to_datetime(dates)
        series.append(pd.Series(returns, index=index))
        held.append(pd.Series(holding.astype(float), index=index))
    if not series:
        return {"series": pd.Series(dtype=float, index=pd.DatetimeIndex([])), "sessions": 0, "trades_marked": 0,
                "return_pct": None, "max_drawdown": None, "capital_in_use": None}
    portfolio = pd.concat(series, axis=1).fillna(0.0).mean(axis=1).sort_index()
    in_use = pd.concat(held, axis=1).fillna(0.0).mean(axis=1).sort_index()
    equity = np.cumprod(1.0 + portfolio.to_numpy(float))
    peak = np.maximum.accumulate(equity)
    drawdown = float(np.max(1.0 - equity / peak)) if len(equity) else 0.0
    return {"series": portfolio, "sessions": int(len(portfolio)), "trades_marked": used,
            "return_pct": round((float(equity[-1]) - 1.0) * 100, 3) if len(equity) else None,
            "max_drawdown": round(drawdown, 4), "capital_in_use": round(float(in_use.mean()), 4)}


def passive_check(trades: list[dict], portfolio: pd.Series, frames: Callable, spy: Optional[pd.DataFrame],
                  irx: Optional[pd.DataFrame], *, cost_bps: float) -> dict:
    """The yardstick's passive-arm excess and SPY residual on the forward sample (``yardstick.passive_scores``)."""
    from app.services import yardstick

    priced = [t for t in trades if net_at(t, cost_bps) is not None]
    if spy is None or not priced:
        return {"status": "unavailable", "reason": "no trades or no SPY history"}
    frame = pd.DataFrame({"symbol": [t["symbol"] for t in priced], "decision_date": [t["decision_date"] for t in priced],
                          "entry_date": [t["entry_date"] for t in priced], "exit_date": [t["exit_date"] for t in priced],
                          "net_return": [net_at(t, cost_bps) for t in priced],
                          "barrier": [t.get("exit_cause") or "" for t in priced]})
    try:
        scores = yardstick.passive_scores(frame, {s: frames(s) for s in set(frame["symbol"])}, spy,
                                          windows={"forward": None}, irx=irx, portfolio=portfolio)
    except Exception as exc:
        return {"status": "unavailable", "reason": f"{exc.__class__.__name__}: {exc}"[:200]}
    window = (scores.get("windows") or {}).get("forward") or {}
    excess, residual = window.get("excess_vs_passive") or {}, window.get("spy_residual") or {}
    return {"status": scores.get("status"), "passes": window.get("passes"),
            "passive_excess_t": excess.get("t_stat_month_clustered"), "passive_excess_bps_per_day": excess.get("mean_active_bps_per_day"),
            "passive_sessions": excess.get("sessions"), "spy_weight": excess.get("spy_weight"),
            "residual_t": residual.get("t_stat_date_clustered"), "residual_bps": residual.get("mean_bps"),
            "residual_trades": residual.get("trades"), "tbill": scores.get("tbill")}


def e_process_reading(trades: list[dict], *, cost_bps: float, inputs: Mapping, index: int) -> dict:
    """The e-process judge in shadow on the per-trade net at the acting cost (null: 0)."""
    from app.services import verdict_stats as vs

    try:
        observations = [vs.Observation(net_at(t, cost_bps) * 10_000, str(t["decision_date"]), str(t["exit_date"]))
                        for t in trades if net_at(t, cost_bps) is not None]
        if not observations:
            return {"method": vs.METHOD_E_PROCESS, "verdict": "watching", "trades": 0, "reason": "no trades yet"}
        clusters = vs.cluster_by_entry_date(observations)
        cls = vs.REVERSION_CLASS
        verdict = vs.e_process_verdict(clusters, bound_bps=float(inputs["loss_bound_bps"][cls]),
                                       sigma_bps=float(inputs["sigma_bps"][cls]),
                                       improvement_level=vs.elond_level(max(index, 1), 0),
                                       rho=float(inputs.get("rho") or 0.0))
        keys = ("method", "verdict", "reason", "trades", "observations", "effective_trades", "mean_difference_bps",
                "e_harm", "e_improvement", "harm_threshold", "improvement_level", "improvement_threshold",
                "decided_at_trades", "bound_bps", "sigma_bps", "rho", "lcb_bps", "kept_at_cap", "cap_trades")
        return {k: verdict.get(k) for k in keys if k in verdict} | {"shadow": True, "class": cls}
    except Exception as exc:
        return {"method": "e_process", "verdict": "unavailable", "reason": f"{exc.__class__.__name__}: {exc}"[:200]}


def acting_cost(measured: Mapping, classes: Iterable[str]) -> dict:
    """25 bps a round trip, or the measured class cost when it is higher (the larger measured class)."""
    values = {cls: _f(measured.get(cls)) for cls in classes}
    known = {cls: v for cls, v in values.items() if v is not None}
    if known and max(known.values()) > ACTING_COST_BPS:
        cls = max(known, key=known.get)
        return {"cost_bps": round(known[cls], 2), "source": f"measured:{cls}", "measured_class_bps": known,
                "changed_by_measurement": True}
    return {"cost_bps": ACTING_COST_BPS, "source": "frozen_25bps", "measured_class_bps": known,
            "changed_by_measurement": False}


def stop_rule(acting_stats: Mapping, series: Mapping) -> dict:
    trades = int(acting_stats.get("trades") or 0)
    t = acting_stats.get("clustered_t")
    dd = series.get("max_drawdown")
    reasons = []
    if trades >= STOP_MIN_TRADES and t is not None and t <= STOP_T:
        reasons.append(f"clustered t {t:+.2f} <= {STOP_T:g} after {trades} trades")
    if dd is not None and dd > STOP_MAX_DRAWDOWN:
        reasons.append(f"equal-weight drawdown {dd:.1%} > {STOP_MAX_DRAWDOWN:.0%}")
    return {"triggered": bool(reasons), "reasons": reasons, "rule": TERMS["stop_rule"],
            "min_trades": STOP_MIN_TRADES, "t_threshold": STOP_T, "max_drawdown": STOP_MAX_DRAWDOWN}


def promotion_criteria(acting_stats: Mapping, quarters: Mapping, series: Mapping, passive: Mapping,
                       gross_bps: Optional[float], veto_cost_bps: float, *, verdict_sample: int) -> dict:
    t_net, t_ex = acting_stats.get("clustered_t"), acting_stats.get("excess_t")
    veto_threshold = max(VETO_FLOOR_BPS, VETO_COST_MULTIPLE * float(veto_cost_bps))
    checks = {
        "net_expectancy_positive_t2": {"expectancy_bps": acting_stats.get("expectancy_bps"), "t": t_net,
                                       "pass": bool((acting_stats.get("expectancy_bps") or 0) > 0 and (t_net or 0) >= PROMOTION_T)},
        "excess_over_drift_positive_t2": {"excess_bps": acting_stats.get("excess_over_drift_bps"), "t": t_ex,
                                          "pass": bool((acting_stats.get("excess_over_drift_bps") or 0) > 0 and (t_ex or 0) >= PROMOTION_T)},
        "yardstick_excess": {"passive_excess_t": passive.get("passive_excess_t"), "residual_t": passive.get("residual_t"),
                             "pass": bool(passive.get("passes"))},
        "positive_quarters_60pct": {"fraction": quarters.get("positive_fraction"), "completed": quarters.get("completed"),
                                    "pass": bool(quarters.get("completed") and (quarters.get("positive_fraction") or 0) >= PROMOTION_MIN_POSITIVE_QUARTERS)},
        "max_drawdown_15pct": {"max_drawdown": series.get("max_drawdown"),
                               "pass": bool(series.get("max_drawdown") is not None and series["max_drawdown"] <= PROMOTION_MAX_DRAWDOWN)},
        "short_hold_veto": {"gross_bps": gross_bps, "threshold_bps": round(veto_threshold, 2),
                            "pass": bool(gross_bps is not None and gross_bps >= veto_threshold)},
    }
    reached = int(acting_stats.get("trades") or 0) >= verdict_sample
    return {"checks": checks, "all_pass": all(c["pass"] for c in checks.values()), "verdict_sample_reached": reached,
            "eligible": bool(reached and all(c["pass"] for c in checks.values())), "rule": TERMS["promotion"]}


def review_block(trades: list[dict], *, frames: Callable, spy, irx, acting: Mapping, measured: Mapping,
                 symbols: Iterable[str], inputs: Mapping, now: datetime, index: int, verdict_sample: int) -> dict:
    """Everything the spec reports for one bucket of trades."""
    from app.services import yardstick

    cost = float(acting["cost_bps"])
    classes = yardstick.classes_of(symbols)
    brackets = {f"{c:g}bps": bucket_stats(trades, cost_bps=c, frames=frames) for c in COST_BRACKETS_BPS}
    brackets["realized_paper_net"] = bucket_stats(trades, cost_bps=None, frames=frames)
    for cls in classes:
        value = _f(measured.get(cls))
        if value is not None:
            brackets[f"measured_{cls}"] = bucket_stats(trades, cost_bps=value, frames=frames)
    acting_stats = brackets[f"{cost:g}bps"] if f"{cost:g}bps" in brackets else bucket_stats(trades, cost_bps=cost, frames=frames)
    gross = bucket_stats(trades, cost_bps=0.0)
    quarters = quarter_record(trades, cost_bps=cost, now=now)
    series = equal_weight_series(trades, frames, symbols, cost_bps=cost)
    passive = passive_check(trades, series["series"], frames, spy, irx, cost_bps=cost)
    veto = yardstick.veto_cost_bps(measured, classes)
    notionals = [t["notional"] for t in trades if t.get("notional")]
    n = int(acting_stats["trades"])
    return {
        "trades": n, "first_decision": trades[0]["decision_date"] if trades else None,
        "last_decision": trades[-1]["decision_date"] if trades else None,
        "sources": {"round_trip": sum(t["source"] == "round_trip" for t in trades),
                    "shadow_signal": sum(t["source"] == "shadow_signal" for t in trades)},
        "readings_recorded": sum(bool(t.get("readings_recorded")) for t in trades),
        "mean_notional_usd": round(float(np.mean(notionals)), 2) if notionals else None,
        "acting": {**acting, **{k: acting_stats[k] for k in ("expectancy_bps", "clustered_t", "excess_over_drift_bps",
                                                           "excess_t", "win_rate", "mean_hold_sessions")}},
        "gross_expectancy_bps": gross["expectancy_bps"],
        "brackets": brackets, "quarters": quarters,
        "equal_weight": {k: series[k] for k in ("sessions", "trades_marked", "return_pct", "max_drawdown", "capital_in_use")},
        "passive": passive, "e_process": e_process_reading(trades, cost_bps=cost, inputs=inputs, index=index),
        "progress": {"trades": n, "checkpoint": CHECKPOINT, "checkpoint_reached": n >= CHECKPOINT,
                     "verdict_sample": verdict_sample, "verdict_sample_reached": n >= verdict_sample,
                     "fraction_of_verdict_sample": round(n / verdict_sample, 4) if verdict_sample else None},
        "stop_rule": stop_rule(acting_stats, series),
        "promotion": promotion_criteria(acting_stats, quarters, series, passive, gross["expectancy_bps"],
                                        veto["cost_bps"], verdict_sample=verdict_sample),
        "status": ("verdict sample reached" if n >= verdict_sample else f"inconclusive, n = {n} of {verdict_sample}"),
    }


def _paired_month_difference(gated: list[dict], ungated: list[dict], *, cost_bps: float) -> dict:
    """Difference of monthly mean net returns (gated minus ungated) over months with both, and its t."""
    def by_month(trades):
        sums: dict[str, list[float]] = {}
        for t in trades:
            net = net_at(t, cost_bps)
            if net is not None:
                sums.setdefault(str(t["decision_date"])[:7], []).append(net)
        return {m: float(np.mean(v)) for m, v in sums.items()}

    g, u = by_month(gated), by_month(ungated)
    months = sorted(set(g) & set(u))
    diffs = np.asarray([g[m] - u[m] for m in months], dtype=float) * 10_000
    if len(diffs) < 2:
        return {"months_paired": len(months), "mean_difference_bps": round(float(diffs.mean()), 2) if len(diffs) else None,
                "t_stat": None}
    se = float(diffs.std(ddof=1)) / math.sqrt(len(diffs))
    return {"months_paired": len(months), "mean_difference_bps": round(float(diffs.mean()), 2),
            "t_stat": round(float(diffs.mean()) / se, 3) if se > 0 else None}


def candidate6_verdict(gated: Mapping, difference: Mapping, *, first_gated: Optional[str], now: datetime) -> dict:
    """The pre-stated decision for candidate 6 (800 gated trades or 2 years, whichever first)."""
    clock_done = False
    if first_gated:
        clock_done = now.astimezone(UTC).date() >= date.fromisoformat(first_gated) + timedelta(days=365 * CANDIDATE6_YEARS)
    due = bool(gated["progress"]["verdict_sample_reached"] or clock_done)
    beats = bool((difference.get("mean_difference_bps") or 0) >= CANDIDATE6_MARGIN_BPS
                 and (difference.get("t_stat") or 0) >= PROMOTION_T)
    promote = bool(due and gated["promotion"]["all_pass"] and beats)
    return {"due": due, "two_year_clock_elapsed": clock_done, "gated_passes_all_criteria": gated["promotion"]["all_pass"],
            "beats_ungated_by_20bps_t2": beats, "margin_bps": CANDIDATE6_MARGIN_BPS,
            "decision": ("promote the gate for a paper forward trial" if promote else
                         ("record the result and close" if due else "watching")),
            "rule": TERMS["candidate_6"]}


# ---------------------------------------------------------------- the review

def _spy_and_irx(frames: Callable) -> tuple[Optional[pd.DataFrame], Optional[pd.DataFrame]]:
    from app.services import yardstick

    spy = frames("SPY")
    try:
        irx = yardstick.load_irx(download=False)
    except Exception:
        irx = None
    return spy, irx


def _closed_status(closure: Mapping, trades: int) -> str:
    return f"closed {str(closure.get('closed_at'))[:10]} ({_kind_text(closure.get('reason_kind'))}), final n = {trades}"


def build_review(db: Session, *, now: Optional[datetime] = None, loader: Optional[Callable] = None,
                 measured: Optional[Mapping] = None, inputs: Optional[Mapping] = None) -> dict:
    """The live reading of every registered candidate (nothing is written)."""
    from app.services import verdict_stats as vs
    from app.services import yardstick

    now = now or datetime.now(UTC)
    frames = _frame_cache(db, loader)
    spy, irx = _spy_and_irx(frames)
    if measured is None:
        measured = yardstick.measured_costs(db)
    if inputs is None:
        inputs = vs.current_inputs(db)
    candidates = []
    pooled_trades: list[dict] = []
    closed = closures(db)
    for registration in registrations(db):
        number = int(registration["candidate"])
        frozen = registration.get("frozen") or {}
        symbols = list(frozen.get("symbols") or [])
        closure = closed.get(str(registration["sweep_id"]))
        sample = candidate_trades(db, registration, now=now, loader=loader, closure=closure)
        trades = sample["trades"]
        acting = acting_cost(measured, yardstick.classes_of(symbols))
        verdict_sample = int((registration.get("sample") or {}).get("verdict_sample") or VERDICT_SAMPLE.get(number, 800))
        block = review_block(trades, frames=frames, spy=spy, irx=irx, acting=acting, measured=measured, symbols=symbols,
                             inputs=inputs, now=now, index=number, verdict_sample=verdict_sample)
        entry = {"candidate": number, "sweep_id": registration["sweep_id"], "strategy_type": sample.get("strategy_type"),
                 "strategy_id": registration.get("strategy_id"), "mechanism": registration.get("mechanism"),
                 "tag_reading": registration.get("tag_reading"), "row_status": sample.get("row_status"),
                 "sample_source": sample["sample_source"], "coverage": sample["coverage"],
                 "excluded_by_tag": len(sample["excluded"]), "registered_at": registration.get("registered_at"),
                 "description": frozen.get("description"), "gate": frozen.get("gate"), "universe": frozen.get("universe"),
                 "symbols_covered": sorted({t["symbol"] for t in trades}), **block,
                 "closure": closure_summary(closure), "open_lots": None}
        if closure is not None:
            entry["status"] = _closed_status(closure, entry["trades"])
            if closure.get("entries_blocked"):
                strategy_id = registration.get("strategy_id")
                entry["open_lots"] = _open_lots_or_unknown(
                    db, db.get(Strategy, int(strategy_id)) if strategy_id is not None else None)
        if number in POOLED["candidates"]:
            pooled_trades.extend(trades)
        if registration.get("tag_reading") == "vix_above_20" and number == 6:
            ungated = [t for t in sample["excluded"] if t.get("excluded_because") == "vix_above_20 is False"]
            unknown = [t for t in sample["excluded"] if t.get("excluded_because") == "vix_above_20 is None"]
            ungated_block = review_block(ungated, frames=frames, spy=spy, irx=irx, acting=acting, measured=measured,
                                         symbols=symbols, inputs=inputs, now=now, index=number, verdict_sample=verdict_sample)
            difference = _paired_month_difference(trades, ungated, cost_bps=float(acting["cost_bps"]))
            entry["buckets"] = {"vix_above_20": {k: block[k] for k in ("trades", "acting", "brackets", "quarters",
                                                                       "equal_weight", "passive", "e_process")},
                                "vix_below_20": {k: ungated_block[k] for k in ("trades", "acting", "brackets", "quarters",
                                                                               "equal_weight", "passive", "e_process")},
                                "vix_unknown": {"trades": len(unknown)}}
            entry["paired_difference"] = difference
            entry["verdict"] = candidate6_verdict(block, difference, first_gated=block["first_decision"], now=now)
        candidates.append(entry)
    pooled = None
    if any(c["candidate"] in POOLED["candidates"] for c in candidates):
        symbols = sorted({t["symbol"] for t in pooled_trades} | set().union(
            *[set((r.get("frozen") or {}).get("symbols") or []) for r in registrations(db)
              if int(r["candidate"]) in POOLED["candidates"]]))
        pooled_trades.sort(key=lambda t: (t["decision_date"], t["symbol"]))
        acting = acting_cost(measured, yardstick.classes_of(symbols))
        pooled = {**POOLED, "candidates": list(POOLED["candidates"]),
                  **review_block(pooled_trades, frames=frames, spy=spy, irx=irx, acting=acting, measured=measured,
                                 symbols=symbols, inputs=inputs, now=now, index=1, verdict_sample=VERDICT_SAMPLE[1])}
    review = {"reviewed_at": now.isoformat(), "spec_sha256": SPEC_SHA256, "report": SPEC_REPORT,
              "acting_cost_rule": TERMS["acting_cost"], "measured_round_trip_bps": dict(measured),
              "e_process_inputs": {k: inputs.get(k) for k in ("as_of", "source", "recorded", "rho")},
              "book_benchmark": "the passive VOO/SGOV arm of the target-weight book (GET /stock-paper/target-weight-book)",
              "candidates": candidates, "pooled": pooled, "paper_only": True, "live_authorized": False}
    return _json_safe(review)


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=_default))


def _default(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if not math.isfinite(float(value)) else float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (pd.Series, pd.DataFrame)):
        return None
    return str(value)


# ------------------------------------------------------- recorded reviews

def recorded_reviews(db: Session, *, limit: Optional[int] = None) -> list[dict]:
    """Every recorded review's payload, newest first (append-only rows)."""
    query = select(AuditLog).where(AuditLog.event_type == EVENT, AuditLog.action == ACTION_REVIEW).order_by(AuditLog.id.desc())
    if limit:
        query = query.limit(limit)
    out = []
    for row in db.scalars(query).all():
        payload = dict(row.payload or {})
        payload.setdefault("audit_id", row.id)
        out.append(payload)
    return out


def review_due(review: Mapping, last: Optional[Mapping], *, now: datetime) -> dict:
    """Whether the schedule calls for a recorded review now, and why.

    Quarterly from the first paper trade and never before 2027-02-10; then
    every 91 days after the last recorded review; and at the checkpoint and
    the verdict sample of any candidate (a crossing since the last review);
    and when candidate 6's two-year clock elapses; and whenever a candidate's
    stop rule is triggered and the candidate is not closed yet (the review
    that records it closes it).  A closed candidate calls for nothing.
    """
    today = now.astimezone(UTC).date()
    reasons = []
    first_dates = [c["first_decision"] for c in review.get("candidates") or [] if c.get("first_decision")]
    first = min(first_dates) if first_dates else None
    if last is None:
        if first is not None:
            quarterly = max(FIRST_REVIEW_NOT_BEFORE, date.fromisoformat(first) + timedelta(days=REVIEW_INTERVAL_DAYS))
            if today >= quarterly:
                reasons.append(f"first quarterly review (first paper trade {first})")
    else:
        last_at = datetime.fromisoformat(str(last["reviewed_at"])).astimezone(UTC).date()
        if today >= last_at + timedelta(days=REVIEW_INTERVAL_DAYS):
            reasons.append(f"quarterly review ({REVIEW_INTERVAL_DAYS} days since {last_at.isoformat()})")
    previous = {int(c["candidate"]): c for c in (last or {}).get("candidates") or []}
    for candidate in review.get("candidates") or []:
        number = int(candidate["candidate"])
        if candidate.get("closure"):
            continue
        progress, before = candidate["progress"], (previous.get(number) or {}).get("progress") or {}
        if progress["checkpoint_reached"] and not before.get("checkpoint_reached"):
            reasons.append(f"candidate {number} reached its checkpoint ({CHECKPOINT} trades)")
        if progress["verdict_sample_reached"] and not before.get("verdict_sample_reached"):
            reasons.append(f"candidate {number} reached its verdict sample ({progress['verdict_sample']} trades)")
        verdict = candidate.get("verdict") or {}
        if verdict.get("two_year_clock_elapsed") and not ((previous.get(number) or {}).get("verdict") or {}).get("two_year_clock_elapsed"):
            reasons.append(f"candidate {number}: the two-year clock elapsed")
        if candidate["stop_rule"]["triggered"]:
            reasons.append(f"candidate {number}: the stop rule triggered")
    return {"due": bool(reasons), "reasons": reasons, "first_paper_trade": first,
            "next_quarterly": (max(FIRST_REVIEW_NOT_BEFORE, date.fromisoformat(first) + timedelta(days=REVIEW_INTERVAL_DAYS)).isoformat()
                               if last is None and first else
                               (datetime.fromisoformat(str(last["reviewed_at"])).astimezone(UTC).date() + timedelta(days=REVIEW_INTERVAL_DAYS)).isoformat()
                               if last else FIRST_REVIEW_NOT_BEFORE.isoformat())}


def record_review(db: Session, *, now: Optional[datetime] = None, reason: str = "scheduled", force: bool = False,
                  loader: Optional[Callable] = None) -> dict:
    """Append one review row when the schedule calls for it (or ``force``); never edits an earlier row."""
    now = now or datetime.now(UTC)
    if not registrations(db):
        return {"status": "skipped", "reason": "no registered forward experiments"}
    review = build_review(db, now=now, loader=loader)
    last = (recorded_reviews(db, limit=1) or [None])[0]
    due = review_due(review, last, now=now)
    if not due["due"] and not force:
        return {"status": "not_due", **due}
    number = len(recorded_reviews(db)) + 1
    # The stop rule is pre-registered: the review that records a trigger closes
    # the candidate, in the same transaction.  The review row carries the
    # closure it causes, and the close row cites the review.
    registered = {str(r["sweep_id"]): r for r in registrations(db)}
    closing = []
    for candidate in review["candidates"]:
        registration = registered.get(str(candidate["sweep_id"]))
        if registration is None or candidate.get("closure") or not candidate["stop_rule"]["triggered"]:
            continue
        terms = _closure_terms(registration, reason_kind="stop_rule", reasons=candidate["stop_rule"]["reasons"],
                               now=now, actor="forward_experiments_review", review_number=number)
        candidate["closure"] = closure_summary(terms)
        candidate["status"] = _closed_status(terms, candidate["trades"])
        closing.append((registration, candidate))
    payload = {**review, "review_number": number, "reason": reason if force and not due["due"] else "; ".join(due["reasons"]) or reason,
               "forced": bool(force and not due["due"]), "markdown": render_markdown(review, number=number)}
    audit = write_audit_log(db, event_type=EVENT, entity_type="forward_experiments", action=ACTION_REVIEW,
                            status="recorded",
                            message=(f"forward experiments review {number}: "
                                     + "; ".join(f"c{c['candidate']} n={c['trades']}" for c in review["candidates"])),
                            payload=payload)
    db.flush()
    closed = []
    for registration, candidate in closing:
        outcome = close_candidate(db, registration, reason_kind="stop_rule", reasons=candidate["stop_rule"]["reasons"],
                                  now=now, actor="forward_experiments_review", review_number=number,
                                  review_audit_id=audit.id)
        if outcome["status"] == "closed":
            closed.append(int(candidate["candidate"]))
    return {"status": "recorded", "review_number": number, "reasons": due["reasons"], "forced": payload["forced"],
            "closed": closed}


# --------------------------------------------------------------- rendering

def _fmt(value: Any, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, np.integer)):
        return str(int(value))
    return f"{float(value):+.{digits}f}"


def _bracket_cell(block: Mapping, key: str) -> str:
    row = (block.get("brackets") or {}).get(key) or {}
    if not row.get("trades"):
        return "n/a"
    return f"{_fmt(row.get('expectancy_bps'))} (t {_fmt(row.get('clustered_t'), 2)})"


def render_markdown(review: Mapping, *, number: int) -> str:
    """The table a recorded review appends to the report's "Reviews" section."""
    stamp = str(review.get("reviewed_at", ""))[:10]
    lines = [f"### Review {number}: {stamp}", "",
             f"Acting cost rule: {review.get('acting_cost_rule')}. Measured round trips (bps): "
             f"{json.dumps(review.get('measured_round_trip_bps') or {})}. Spec SHA-256 `{review.get('spec_sha256')}`.", "",
             "| Candidate | Row / sample | Trades | Net @10 | @22 | @25 | @50 | Realised | Excess over drift | Win | Hold | "
             "Quarters + | Max DD | Passive t / resid t | e-process | Progress | Stop | Status |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]

    def row(label: str, block: Mapping, extra: str = "") -> str:
        acting = block.get("acting") or {}
        quarters = block.get("quarters") or {}
        ew = block.get("equal_weight") or {}
        passive = block.get("passive") or {}
        e = block.get("e_process") or {}
        progress = block.get("progress") or {}
        q = (f"{quarters.get('positive')}/{quarters.get('completed')}" if quarters.get("completed") else "none completed")
        return (f"| {label} | {extra} | {block.get('trades')} | {_bracket_cell(block, '10bps')} | {_bracket_cell(block, '22bps')} | "
                f"{_bracket_cell(block, '25bps')} | {_bracket_cell(block, '50bps')} | {_bracket_cell(block, 'realized_paper_net')} | "
                f"{_fmt(acting.get('excess_over_drift_bps'))} (t {_fmt(acting.get('excess_t'), 2)}) | "
                f"{_fmt(acting.get('win_rate'), 2)} | {_fmt(acting.get('mean_hold_sessions'), 1)} | {q} | "
                f"{_fmt(ew.get('max_drawdown'), 3)} | {_fmt(passive.get('passive_excess_t'), 2)} / {_fmt(passive.get('residual_t'), 2)} | "
                f"{e.get('verdict')} | {progress.get('trades')}/{progress.get('verdict_sample')} (checkpoint {progress.get('checkpoint')}) | "
                f"{'CLOSED' if block.get('closure') else 'TRIGGERED' if (block.get('stop_rule') or {}).get('triggered') else 'no'} | "
                f"{block.get('status')} |")

    for candidate in review.get("candidates") or []:
        extra = f"{candidate.get('strategy_type')} / {candidate.get('sample_source')} ({candidate.get('mechanism')})"
        lines.append(row(f"{candidate['candidate']} `{candidate['sweep_id']}`", candidate, extra))
        if candidate.get("buckets"):
            below = candidate["buckets"]["vix_below_20"]
            lines.append(row(f"{candidate['candidate']} ungated (VIX < 20)", {**below, "stop_rule": {}, "progress": {},
                                                                              "status": "comparison bucket"}, extra))
    if review.get("pooled"):
        lines.append(row(f"pooled 1-3 ({review['pooled']['trials_charged']} trials)", review["pooled"], "pooled trades"))
    lines.append("")
    for candidate in review.get("candidates") or []:
        if candidate.get("verdict"):
            v, d = candidate["verdict"], candidate.get("paired_difference") or {}
            lines.append(f"Candidate {candidate['candidate']}: gated minus ungated {_fmt(d.get('mean_difference_bps'))} bps a trade "
                         f"over {d.get('months_paired')} paired months (t {_fmt(d.get('t_stat'), 2)}); "
                         f"unknown-reading trades {candidate['buckets']['vix_unknown']['trades']}; "
                         f"reading disagreements with the VIX history {candidate.get('coverage', {}).get('reading_disagreements')}; "
                         f"decision: {v['decision']}.")
        closure = candidate.get("closure")
        if closure:
            held = candidate.get("open_lots")
            lots = "open lots not read" if held is None else f"{len(held['lots'])} open lot(s) keep their exits"
            lines.append(f"Candidate {candidate['candidate']}: CLOSED {str(closure.get('closed_at'))[:10]} "
                         f"({_kind_text(closure.get('reason_kind'))}: {'; '.join(closure.get('reasons') or [])}); "
                         + (f"new entries disabled; {lots}." if closure.get("entries_blocked")
                            else "the sample is closed; the host row is unchanged."))
        elif candidate["stop_rule"]["triggered"]:
            lines.append(f"Candidate {candidate['candidate']}: STOP RULE TRIGGERED ({'; '.join(candidate['stop_rule']['reasons'])}); "
                         f"the next recorded review closes it.")
    lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------- the view

def forward_experiments_view(db: Session, *, now: Optional[datetime] = None) -> dict:
    """``GET /stock-paper/forward-experiments``: registrations, the live review, the recorded reviews."""
    now = now or datetime.now(UTC)
    registered = registrations(db)
    if not registered:
        return {"status": "unregistered", "spec": {"report": SPEC_REPORT, "sha256": SPEC_SHA256}, "registrations": [],
                "review": None, "recorded_reviews": [], "paper_only": True, "live_authorized": False}
    review = build_review(db, now=now)
    recorded = recorded_reviews(db)
    last = recorded[0] if recorded else None
    closed = closures(db)
    return {"status": "registered", "spec": {"report": SPEC_REPORT, "json": SPEC_JSON, "sha256": SPEC_SHA256,
                                              "frozen_at": FROZEN_AT},
            "terms": TERMS, "mutation_paused": True,
            "registrations": [{**{k: r.get(k) for k in ("candidate", "sweep_id", "strategy_type", "strategy_id",
                                                          "mechanism", "tag_reading", "row_created", "registered_at",
                                                          "code_commit", "sample")},
                               "closure": closure_summary(closed.get(str(r["sweep_id"])))} for r in registered],
            "closures": [closure_summary(c) for c in closed.values()],
            "review": review, "schedule": review_due(review, last, now=now),
            "recorded_reviews": [{k: r.get(k) for k in ("audit_id", "review_number", "reviewed_at", "reason", "forced")}
                                 for r in recorded],
            "latest_recorded_markdown": last.get("markdown") if last else None,
            "paper_only": True, "live_authorized": False}
