"""One view of what the research has established.

The verdicts of the hand-run studies used to live only in ``reports/*.md``
(about thirty of them), the forward experiments in their own audit rows, the
discovery library and the experiment engine in theirs.  Nothing showed, in one
place, what was tested and on which data, whether it passed, why it failed,
when it is decided next, whether it can place a paper order today, and what
new evidence would justify looking again.

``app/services/data/hypothesis-register.json`` holds one entry per hand-run
study (a test, ``tests/test_hypothesis_register.py``, fails when a report in
``reports/`` is neither in an entry nor on the file's ``not_hypotheses``
list, so a new study cannot be written up without being registered).
``hypothesis_register`` merges that file with the live sources:

* the frozen forward experiments (``forward_experiments.registrations`` and
  ``closures``): ``collecting`` until closed; a stop-rule or failed-verdict
  closure reads ``failed``, an operator closure ``inconclusive`` (closed
  without a verdict);
* the discovery library's latest run (one entry);
* the experiment engine's adopted changes (one entry).

Each entry carries ``can_place_paper_orders``, computed from the strategy
rows it names: the row's status, whether it is active, whether a closed
forward experiment blocks its entries, and (for a scheduled entry) whether
approved forward evidence exists.  An entry that names no row cannot place
an order: it is research only.

``closed_families`` is what a new proposal is checked against: a family in
which every registered study failed.  Read-only; paper only.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import AuditLog, Strategy

logger = logging.getLogger(__name__)
UTC = timezone.utc
REGISTER_FILE = Path(__file__).with_name("data") / "hypothesis-register.json"
STATUSES = ("passed", "failed", "inconclusive", "collecting")
REQUIRED = ("id", "family", "title", "hypothesis", "data", "status", "result", "why", "decided_on", "revisit_if",
            "sources", "strategy_types")
EXPLORING_STATUSES = frozenset({"paper_trading_candidate", "research"})


def load_register(path: Path = REGISTER_FILE) -> dict:
    """The packaged register, checked: unique ids, known statuses, every required field present."""
    register = json.loads(path.read_text())
    seen: set[str] = set()
    for entry in register.get("entries") or []:
        missing = [key for key in REQUIRED if key not in entry]
        if missing:
            raise ValueError(f"hypothesis register entry {entry.get('id')!r} is missing {', '.join(missing)}")
        if entry["status"] not in STATUSES:
            raise ValueError(f"hypothesis register entry {entry['id']!r} has unknown status {entry['status']!r}")
        if entry["id"] in seen:
            raise ValueError(f"hypothesis register id {entry['id']!r} is used twice")
        if not (entry["sources"] or {}).get("reports"):
            raise ValueError(f"hypothesis register entry {entry['id']!r} names no report")
        seen.add(entry["id"])
    return register


def closed_families(register: Optional[Mapping] = None) -> dict[str, list[str]]:
    """``{family: [entry ids]}`` for every family in which each registered study failed."""
    register = register or load_register()
    by_family: dict[str, list[Mapping]] = {}
    for entry in register["entries"]:
        by_family.setdefault(entry["family"], []).append(entry)
    return {family: [entry["id"] for entry in entries] for family, entries in sorted(by_family.items())
            if all(entry["status"] == "failed" for entry in entries)}


# ------------------------------------------------------------- paper orders

def _approved_evidence(db: Session, strategy_ids: list[int], now: datetime) -> set[int]:
    from app.models.stock_paper import StockPaperStrategyEvidence

    if not strategy_ids:
        return set()
    rows = db.execute(select(StockPaperStrategyEvidence.strategy_id, StockPaperStrategyEvidence.expires_at)
                      .where(StockPaperStrategyEvidence.strategy_id.in_(strategy_ids),
                             StockPaperStrategyEvidence.status == "approved")).all()
    out = set()
    for strategy_id, expires_at in rows:
        expires = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
        if expires >= now:
            out.add(strategy_id)
    return out


def row_ability(row: Optional[Strategy], *, blocked: Mapping[int, str], evidence: set[int]) -> dict:
    """Whether one strategy row can place a paper order now, and why."""
    if row is None:
        return {"can_place": False, "status": None, "detail": "no strategy row"}
    status = row.current_status
    out = {"strategy_type": row.strategy_type, "status": status}
    if not row.is_active:
        return {**out, "can_place": False, "detail": "inactive"}
    if row.id in blocked:
        return {**out, "can_place": False, "detail": blocked[row.id]}
    if status == "shadow":
        return {**out, "can_place": False, "detail": "shadow: signals recorded, no orders"}
    if status == "paper_trading_active":
        if row.id in evidence:
            return {**out, "can_place": True, "detail": "scheduled paper entries (approved forward evidence)"}
        return {**out, "can_place": False, "detail": "active, but no approved forward evidence: no scheduled entries"}
    if status in EXPLORING_STATUSES:
        if settings.paper_exploration_enabled:
            return {**out, "can_place": True, "detail": "learning-sized exploration entries only"}
        return {**out, "can_place": False, "detail": "exploration is off"}
    if status == "target_weight_active":
        return {**out, "can_place": True, "detail": "target-weight sleeve of the slow book"}
    return {**out, "can_place": False, "detail": f"status {status}: no entries"}


def _orders(types: list[str], rows: Mapping[str, Strategy], *, blocked: Mapping[int, str], evidence: set[int]) -> dict:
    if not types:
        return {"can_place": False, "detail": "research only: no strategy row", "rows": []}
    abilities = [{"strategy_type": slug, **row_ability(rows.get(slug), blocked=blocked, evidence=evidence)}
                 for slug in types]
    able = [a for a in abilities if a["can_place"]]
    detail = "; ".join(f"{a['strategy_type']}: {a['detail']}" for a in abilities)
    return {"can_place": bool(able), "detail": detail, "rows": abilities}


# ------------------------------------------------------------- live sources

def _forward_entries(db: Session, rows_by_id: Mapping[int, Strategy], *, blocked: Mapping[int, str],
                     evidence: set[int]) -> list[dict]:
    from app.services import forward_experiments as fx

    closed = fx.closures(db)
    last = (fx.recorded_reviews(db, limit=1) or [None])[0]
    observed = {int(c["candidate"]): c.get("trades") for c in (last or {}).get("candidates") or []}
    out = []
    for registration in fx.registrations(db):
        number = int(registration["candidate"])
        frozen = registration.get("frozen") or {}
        sample = registration.get("sample") or {}
        closure = closed.get(str(registration["sweep_id"]))
        row = rows_by_id.get(int(registration["strategy_id"])) if registration.get("strategy_id") is not None else None
        required, yearly = sample.get("verdict_sample"), sample.get("expected_trades_per_year")
        years = round(float(required) / float(yearly), 1) if required and yearly else None
        if closure is None:
            status, result = "collecting", (f"{observed.get(number)} of {required} trades at the last recorded review"
                                           if number in observed else f"no review recorded yet; needs {required} trades")
            why = ("Pre-registered and frozen; judged only at a scheduled review."
                   + (f" At about {yearly} trades a year the verdict sample takes about {years} years." if years else ""))
            next_decision = {"date": fx.FIRST_REVIEW_NOT_BEFORE.isoformat(),
                             "note": "first review no earlier than this date, then quarterly, at the checkpoint and "
                                     "at the verdict sample"}
        else:
            kind = closure.get("reason_kind")
            status = "failed" if kind in ("stop_rule", "verdict_failed") else "inconclusive"
            result = f"closed {str(closure.get('closed_at'))[:10]} ({fx._kind_text(kind)})"
            why = "; ".join(closure.get("reasons") or [])
            next_decision = None
        ability = row_ability(row, blocked=blocked, evidence=evidence)
        if registration.get("mechanism") == "tag_row":
            ability = {**ability, "detail": f"tag-and-compare on {registration.get('strategy_type')} "
                                            f"(the host row's own entries): {ability['detail']}"}
        out.append({
            "id": f"forward-experiment-{number}", "source": "forward_experiment",
            "family": frozen.get("family") or "forward_experiment",
            "title": f"Forward experiment {number}: {registration.get('strategy_type')}",
            "hypothesis": frozen.get("description"),
            "data": "the bot's own paper trades since registration (forward only; never re-tested on history)",
            "status": status, "result": result, "why": why,
            "decided_on": str(closure.get("closed_at"))[:10] if closure else None, "next_decision": next_decision,
            "sample": {"required": required, "observed": observed.get(number), "expected_trades_per_year": yearly,
                       "years_to_verdict": years},
            "strategy_types": [registration.get("strategy_type")],
            "revisit_if": "Nothing on history: the frozen terms forbid re-testing these rules on past data.",
            "sources": {"reports": [fx.SPEC_REPORT], "notes": None, "pr": None},
            "can_place_paper_orders": ability["can_place"],
            "paper_orders": {"can_place": ability["can_place"], "detail": ability["detail"], "rows": [ability]},
        })
    return out


def _discovery_entry(db: Session) -> Optional[dict]:
    row = db.scalars(select(AuditLog).where(AuditLog.event_type == "strategy_discovery", AuditLog.action == "run")
                     .order_by(AuditLog.id.desc()).limit(1)).first()
    if row is None:
        return None
    payload = row.payload or {}
    proven, explore = int(payload.get("proven") or 0), int(payload.get("explore") or 0)
    return {
        "id": "discovery-library", "source": "strategy_discovery", "family": "rule_library",
        "title": "Weekly strategy discovery library",
        "hypothesis": "Some rule in the discovery library passes the fixed gate after its trials are charged.",
        "data": f"the discovery replay universe, data to {payload.get('data_end')}",
        "status": "passed" if proven else "collecting",
        "result": (f"{proven} proven, {explore} explore of {payload.get('library')} candidates; "
                   f"{payload.get('trials')} trials ever"),
        "why": "A proven rule is still only eligible for a paper forward trial.",
        "decided_on": row.created_at.date().isoformat() if row.created_at else None,
        "next_decision": {"date": None, "note": "re-graded weekly"}, "strategy_types": [],
        "revisit_if": "The weekly re-grade is the revisit; mutation stays paused while the forward experiments run.",
        "sources": {"reports": ["reports/strategy-discovery-20261102.md", "reports/rule-mutation-20261106.md"],
                    "notes": None, "pr": None},
        "can_place_paper_orders": False,
        "paper_orders": {"can_place": False, "rows": [],
                         "detail": "an admitted rule gets its own row; the library itself places nothing"},
    }


def _engine_entry(db: Session) -> Optional[dict]:
    from app.services.experiment_engine import adopted_changes

    changes = adopted_changes(db)
    if not changes:
        return None
    states: dict[str, int] = {}
    for change in changes:
        state = str(change.get("state") or "open")
        states[state] = states.get(state, 0) + 1
    confirmed = states.get("confirmed", 0)
    return {
        "id": "learning-engine-changes", "source": "experiment_engine", "family": "learning_loop",
        "title": "Changes the experiment engine adopted",
        "hypothesis": "A change the engine adopts from a diagnosed loss pattern improves the strategy going forward.",
        "data": "the bot's own paper trades before and after each change",
        "status": "passed" if confirmed else "collecting",
        "result": ", ".join(f"{count} {state}" for state, count in sorted(states.items())) + f" of {len(changes)} adopted",
        "why": "An adopted parameter is not evidence; only a forward verdict is.",
        "decided_on": None, "next_decision": {"date": None, "note": "each change is judged on its forward trades"},
        "strategy_types": sorted({str(c.get("strategy_type")) for c in changes if c.get("strategy_type")}),
        "revisit_if": "See GET /stock-paper/learning-experiments for each change's verdict.",
        "sources": {"reports": ["reports/learning-loop-20261111.md"], "notes": None, "pr": None},
        "can_place_paper_orders": False,
        "paper_orders": {"can_place": False, "rows": [],
                         "detail": "a change edits a row's parameters; the row's own entry shows whether it trades"},
    }


def _live(name: str, build) -> tuple[list[dict], Optional[dict]]:
    """A live source that fails never empties the register: it is reported as unavailable."""
    try:
        built = build()
    except Exception as exc:
        logger.warning("hypothesis register: %s unavailable: %s", name, exc.__class__.__name__)
        return [], {"source": name, "error": f"{exc.__class__.__name__}: {exc}"[:200]}
    if built is None:
        return [], None
    return (built if isinstance(built, list) else [built]), None


# ------------------------------------------------------------------ the view

def hypothesis_register(db: Session, *, now: Optional[datetime] = None) -> dict:
    """``GET /stock-paper/hypothesis-register``: every hypothesis, its verdict and whether it can trade (read-only)."""
    from app.services import forward_experiments as fx

    now = now or datetime.now(UTC)
    register = load_register()
    strategies = db.scalars(select(Strategy).order_by(Strategy.id)).all()
    rows_by_type: dict[str, Strategy] = {}
    for row in strategies:
        rows_by_type.setdefault(row.strategy_type, row)
    rows_by_id = {row.id: row for row in strategies}
    unavailable = []
    try:
        blocked: Mapping[int, str] = fx.entry_blocks(db)
    except Exception as exc:  # shown, never guessed: an unreadable closure is not "can trade"
        blocked = {}
        unavailable.append({"source": "forward_experiment_closures", "error": f"{exc.__class__.__name__}: {exc}"[:200]})
    evidence = _approved_evidence(db, [row.id for row in strategies if row.current_status == "paper_trading_active"], now)
    entries = []
    for entry in register["entries"]:
        orders = _orders(entry["strategy_types"], rows_by_type, blocked=blocked, evidence=evidence)
        entries.append({**entry, "source": "report", "can_place_paper_orders": orders["can_place"],
                        "paper_orders": orders})
    for name, build in (
        ("forward_experiments", lambda: _forward_entries(db, rows_by_id, blocked=blocked, evidence=evidence)),
        ("strategy_discovery", lambda: _discovery_entry(db)),
        ("experiment_engine", lambda: _engine_entry(db)),
    ):
        built, problem = _live(name, build)
        entries.extend(built)
        if problem:
            unavailable.append(problem)
    counts = {status: sum(1 for entry in entries if entry["status"] == status) for status in STATUSES}
    return {
        "as_of": now.isoformat(), "version": register.get("version"), "updated": register.get("updated"),
        "statuses": list(STATUSES), "counts": counts,
        "can_place_paper_orders": sum(1 for entry in entries if entry["can_place_paper_orders"]),
        "closed_families": closed_families(register), "entries": entries, "unavailable": unavailable,
        "note": ("passed means a pre-set rule was met on the data named; it is not a claim of profit. A study on the "
                 "45 current symbols is survivorship-biased, and its caveat says so."),
        "paper_only": True, "live_authorized": False,
    }
