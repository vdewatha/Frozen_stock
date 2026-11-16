"""Admission requirement for new experiments: can it reach a decision in a useful time?

The six frozen forward experiments were registered with their time to a
verdict written beside them: about 45, 18, 65, 55, 10 and 2 years.  They
collect honest observations, but five of them cannot answer the project's
question soon enough.  Nothing checked that before they started.

From here on an experiment is admitted only if, before it starts,

1. **it can reach its verdict in time.**  ``estimate`` works out the
   independent trades a one-sided t of ``T_REQUIRED`` needs to see an effect of
   ``EFFECT_FLOOR_BPS`` (the yardstick's floor) at the per-trade standard
   deviation given, the design effect ``1 + (m - 1) * rho`` of trades that
   share a decision date, the verdict sample that makes, and the years it
   takes at the expected trade rate.  The inputs are chosen on the favourable
   side (the smaller measured standard deviation, the measured same-date
   correlation), so a refusal means the experiment cannot decide in time even
   then.  The limit is ``experiment_admission_max_years`` (2 by default);
2. **its family is not closed.**  A family in which every registered study
   failed (``hypothesis_register.family_closure``) takes no new experiment
   unless the register records the new evidence that justifies one
   (``admission_overrides`` in the register file, reviewed like any change);
3. **the run has compute left.**  Each job that starts experiments has
   ``experiment_admission_compute_minutes`` to spend; an experiment whose
   estimated minutes do not fit is deferred to a later run.

The checks are used where an experiment can start:

* ``strategy_discovery.admit_survivors``: a rule is not given a paper row;
* ``experiment_engine.run_experiment_engine``: a planned experiment is
  skipped (its forward verdict needs ``experiment_verdict_min_trades`` trades
  at the strategy's own recent rate);
* ``forward_experiments.register_forward_experiments``: a candidate of any
  future frozen spec is not registered.  The 2026-11-10 spec is grandfathered:
  its six candidates keep their original terms, and closing one is an explicit
  operator action, never something this module does.

Every decision is one ``experiment_admission`` audit row (``admit`` /
``refuse``) carrying the numbers it was made on, written when the decision
for that proposal changes.  Nothing here places, sizes or authorises an order.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import AuditLog

logger = logging.getLogger(__name__)
UTC = timezone.utc
EVENT = "experiment_admission"
ACTION_ADMIT = "admit"
ACTION_REFUSE = "refuse"
T_REQUIRED = 2.0
EFFECT_FLOOR_BPS = 30.0       # the yardstick's short-hold floor: the smallest net effect worth a verdict
SAME_DATE_CAP = 11            # trades assumed to share a decision date, at most (the frozen spec's convention)
# The 2026-11-10 frozen spec's own inputs, kept so its table can be reproduced.
FROZEN_SPEC_STD_BPS = 270.0
FROZEN_SPEC_RHO = 0.15
FROZEN_DEADLINE_YEARS = {6: 2.0}  # candidate 6's own terms: 800 gated trades or 2 years, whichever first
RATE_WINDOW_DAYS = 180
RATE_MIN_SPAN_DAYS = 30.0


def estimate(*, trades_per_year: Optional[float], symbols: int, per_trade_std_bps: float, rho: float,
             effect_bps: float = EFFECT_FLOOR_BPS, t_required: float = T_REQUIRED,
             same_date_trades: Optional[float] = None, sample_required: Optional[int] = None,
             decision_deadline_years: Optional[float] = None) -> dict:
    """Independent samples, verdict sample and years to a verdict for one proposal.

    ``sample_required`` fixes the verdict sample instead of deriving it (an
    experiment whose own rule names the trades it needs).
    ``decision_deadline_years`` is a clock the experiment's own terms set (it
    is decided then whatever the sample), so the decision comes no later.
    """
    shared = float(same_date_trades) if same_date_trades else float(max(1, min(int(symbols or 1), SAME_DATE_CAP)))
    design_effect = 1.0 + (shared - 1.0) * max(float(rho), 0.0)
    independent = (t_required * per_trade_std_bps / effect_bps) ** 2
    if sample_required is not None:
        sample = int(sample_required)
        independent = sample / design_effect
    else:
        sample = int(math.ceil(independent * design_effect))
    rate = float(trades_per_year) if trades_per_year and trades_per_year > 0 else None
    years = round(sample / rate, 1) if rate else None
    sample_years = years
    if decision_deadline_years is not None:
        years = float(decision_deadline_years) if years is None else min(years, float(decision_deadline_years))
    return {
        "effect_bps": effect_bps, "t_required": t_required, "per_trade_std_bps": per_trade_std_bps, "rho": rho,
        "same_date_trades": shared, "design_effect": round(design_effect, 3),
        "independent_samples_required": int(math.ceil(independent)), "verdict_sample": sample,
        "trades_per_year": round(rate, 1) if rate else None,
        "independent_samples_per_year": round(rate / design_effect, 1) if rate else None,
        "years_to_verdict": years, "years_to_verdict_sample": sample_years,
        "decision_deadline_years": decision_deadline_years,
    }


def budget() -> dict:
    return {"max_years_to_verdict": float(settings.experiment_admission_max_years),
            "compute_minutes_per_run": float(settings.experiment_admission_compute_minutes),
            "minutes_per_experiment": float(settings.experiment_admission_minutes_per_experiment)}


def decide(proposal: Mapping, numbers: Mapping, *, compute_spent_minutes: float = 0.0,
           register: Optional[Mapping] = None) -> dict:
    """Admit or refuse one proposal: ``{"admitted", "decision", "reasons", "estimate", "budget", ...}``."""
    from app.services import hypothesis_register

    limits = budget()
    minutes = float(proposal.get("compute_minutes") or limits["minutes_per_experiment"])
    reasons, deferred = [], False
    years = numbers.get("years_to_verdict")
    if years is None:
        note = "trade rate unknown: time to a verdict could not be estimated"
    else:
        note = None
        if years > limits["max_years_to_verdict"]:
            reasons.append(f"about {years:g} years to a verdict ({numbers['verdict_sample']} trades at "
                           f"{numbers['trades_per_year']:g} a year); the limit is "
                           f"{limits['max_years_to_verdict']:g}")
    if years is None and numbers.get("decision_deadline_years") is None:
        note = "trade rate unknown: time to a verdict could not be estimated"
    closure = hypothesis_register.family_closure(proposal.get("family"), register=register)
    if closure and closure["closed"] and not closure.get("new_evidence"):
        reasons.append(f"family {closure['family']} is closed by {', '.join(closure['entries'])}; "
                       f"a new experiment must name its new evidence in the hypothesis register")
    if not reasons and compute_spent_minutes + minutes > limits["compute_minutes_per_run"]:
        reasons.append(f"over this run's research compute budget ({compute_spent_minutes:g} + {minutes:g} of "
                       f"{limits['compute_minutes_per_run']:g} minutes)")
        deferred = True
    admitted = not reasons
    return {
        "kind": proposal["kind"], "key": proposal["key"], "family": proposal.get("family"),
        "admitted": admitted, "decision": "admitted" if admitted else ("deferred" if deferred else "refused"),
        "reasons": reasons, "note": note, "estimate": dict(numbers), "budget": limits,
        "compute_minutes": minutes, "family_closure": closure, "paper_only": True,
    }


def enabled() -> bool:
    return bool(settings.experiment_admission_enabled)


# ----------------------------------------------------------------- the record

def latest_decisions(db: Session) -> dict[tuple[str, str], dict]:
    """The latest recorded decision per proposal, keyed ``(kind, key)``."""
    rows = db.execute(select(AuditLog.id, AuditLog.action, AuditLog.payload, AuditLog.created_at)
                      .where(AuditLog.event_type == EVENT).order_by(AuditLog.id)).all()
    out: dict[tuple[str, str], dict] = {}
    for audit_id, action, payload, created_at in rows:
        payload = dict(payload or {})
        payload["audit_id"], payload["action"] = audit_id, action
        payload.setdefault("recorded_at", created_at.isoformat() if created_at else None)
        out[(str(payload.get("kind")), str(payload.get("key")))] = payload
    return out


def record(db: Session, decision: Mapping, *, known: Optional[Mapping] = None, now: Optional[datetime] = None) -> bool:
    """Write the decision when it differs from the last one recorded for the proposal; True when written.

    A deferral is this run's budget, not a verdict on the proposal, and is not
    recorded.  A failure to write never changes the decision.
    """
    from app.services.audit import write_audit_log

    if decision["decision"] == "deferred":
        return False
    try:
        known = latest_decisions(db) if known is None else known
        last = known.get((str(decision["kind"]), str(decision["key"])))
        if last is not None and last.get("decision") == decision["decision"] and last.get("reasons") == decision["reasons"]:
            return False
        write_audit_log(
            db, event_type=EVENT, entity_type="experiment_proposal",
            action=ACTION_ADMIT if decision["admitted"] else ACTION_REFUSE, status=decision["decision"],
            message=(f"{decision['kind']} {decision['key']}: {decision['decision']}"
                     + (f" ({'; '.join(decision['reasons'])})" if decision["reasons"] else ""))[:1000],
            payload={**decision, "decided_at": (now or datetime.now(UTC)).isoformat()})
        db.flush()
        return True
    except Exception as exc:
        logger.warning("experiment admission: decision for %s not recorded: %s", decision.get("key"),
                       exc.__class__.__name__)
        return False


# ---------------------------------------------------------------- the callers

def measured_inputs(db: Optional[Session]) -> dict:
    """The favourable-side inputs: the smaller measured per-trade deviation and the measured same-date correlation."""
    from app.services import verdict_stats

    inputs = verdict_stats.current_inputs(db)
    sigmas = [float(value) for value in (inputs.get("sigma_bps") or {}).values() if value]
    return {"per_trade_std_bps": min(sigmas) if sigmas else FROZEN_SPEC_STD_BPS,
            "rho": float(inputs.get("rho") if inputs.get("rho") is not None else FROZEN_SPEC_RHO),
            "as_of": inputs.get("as_of"), "source": inputs.get("source")}


def _years_between(start: Any, end: Any) -> Optional[float]:
    try:
        first, last = datetime.fromisoformat(str(start)[:10]), datetime.fromisoformat(str(end)[:10])
    except (TypeError, ValueError):
        return None
    span = (last - first).days / 365.25
    return span if span > 0 else None


def discovery_proposal(row: Mapping, result: Mapping) -> dict:
    """A discovery candidate as a proposal: its recent trade rate is the holdout's (a year or more), else the discovery window's."""
    holdout_years = _years_between(result.get("holdout_start"), result.get("data_end"))
    holdout_trades = (row.get("holdout") or {}).get("trades")
    discovery_trades = ((row.get("discovery") or {}).get("net") or {}).get("trades")
    discovery_years = _years_between(settings.strategy_discovery_history_start, result.get("holdout_start"))
    if holdout_years and holdout_years >= 1.0 and holdout_trades is not None:
        rate, basis = float(holdout_trades) / holdout_years, "holdout window"
    elif discovery_years and discovery_trades is not None:
        rate, basis = float(discovery_trades) / discovery_years, "discovery window"
    else:
        rate, basis = None, "unknown"
    return {"kind": "discovery_rule", "key": str(row["id"]), "family": row.get("family"),
            "symbols": len(row.get("symbols") or []), "trades_per_year": rate, "rate_basis": basis}


def check_discovery(db: Session, row: Mapping, result: Mapping, *, inputs: Optional[Mapping] = None,
                    compute_spent_minutes: float = 0.0) -> dict:
    proposal = discovery_proposal(row, result)
    inputs = inputs or measured_inputs(db)
    numbers = estimate(trades_per_year=proposal["trades_per_year"], symbols=proposal["symbols"],
                       per_trade_std_bps=inputs["per_trade_std_bps"], rho=inputs["rho"])
    numbers["rate_basis"] = proposal["rate_basis"]
    # Admitting a row replays nothing: the backtest that found it is already paid for.
    return decide({**proposal, "compute_minutes": 0.0}, numbers, compute_spent_minutes=compute_spent_minutes)


def trade_rates(attributions: Iterable, *, now: datetime, window_days: int = RATE_WINDOW_DAYS) -> dict[str, float]:
    """Closed paper trades a year per strategy type over the last ``window_days``.

    A book younger than the window is measured over its own age (30 days at
    least), so a bot that began trading a month ago is not read as slow.
    """
    since = now - timedelta(days=window_days)
    counts: dict[str, int] = {}
    first: Optional[datetime] = None
    for row in attributions:
        closed = getattr(row, "closed_at", None)
        if closed is None:
            continue
        closed = closed if closed.tzinfo else closed.replace(tzinfo=UTC)
        if closed >= since:
            counts[row.strategy_type] = counts.get(row.strategy_type, 0) + 1
            first = closed if first is None or closed < first else first
    if first is None:
        return {}
    span_days = min(float(window_days), max(RATE_MIN_SPAN_DAYS, (now - first).total_seconds() / 86400.0))
    return {slug: count * 365.25 / span_days for slug, count in counts.items()}


def check_engine(planned: Mapping, rates: Mapping[str, float], *, inputs: Mapping,
                 compute_spent_minutes: float = 0.0, book: str = "book") -> dict:
    """An engine experiment: its forward verdict needs ``experiment_verdict_min_trades`` trades after the change."""
    target = planned["strategy_type"]
    rate = sum(rates.values()) if target == book else rates.get(target)
    numbers = estimate(trades_per_year=rate, symbols=1, per_trade_std_bps=inputs["per_trade_std_bps"],
                       rho=inputs["rho"], same_date_trades=1.0,
                       sample_required=int(settings.experiment_verdict_min_trades))
    numbers["decision_cap_trades"] = int(settings.experiment_verdict_max_trades)
    numbers["rate_basis"] = f"closed paper trades of the last {RATE_WINDOW_DAYS} days"
    return decide({"kind": "engine_experiment", "key": f"{target}:{planned['template']}", "family": None},
                  numbers, compute_spent_minutes=compute_spent_minutes)


def check_forward_candidate(candidate: Mapping, *, per_trade_std_bps: Optional[float] = None,
                            rho: Optional[float] = None) -> dict:
    """A candidate of a frozen forward spec: its own verdict sample and expected trade rate."""
    symbols = len(candidate.get("symbols") or [])
    numbers = estimate(trades_per_year=candidate.get("expected_trades_per_year"), symbols=symbols,
                       per_trade_std_bps=float(per_trade_std_bps or candidate.get("per_trade_std_bps")
                                               or FROZEN_SPEC_STD_BPS),
                       rho=float(rho if rho is not None else candidate.get("rho", FROZEN_SPEC_RHO)),
                       sample_required=candidate.get("verdict_sample"),
                       decision_deadline_years=candidate.get("decision_deadline_years"))
    numbers["rate_basis"] = "the spec's expected trades a year"
    return decide({"kind": "forward_experiment", "key": str(candidate["sweep_id"]),
                   "family": candidate.get("family"), "compute_minutes": 0.0}, numbers)


# ------------------------------------------------------------------- the view

def admission_view(db: Session, *, limit: int = 100) -> dict:
    """``GET /stock-paper/experiment-admissions``: the rule, the budget and the recorded decisions (read-only)."""
    from app.services import forward_experiments as fx

    decisions = sorted(latest_decisions(db).values(), key=lambda item: -int(item.get("audit_id") or 0))[:limit]
    frozen = []
    try:
        spec = fx.load_spec()
        for candidate in spec["candidates"]:
            number = int(candidate["candidate"])
            check = check_forward_candidate({**candidate, "verdict_sample": fx.VERDICT_SAMPLE[number],
                                             "decision_deadline_years": FROZEN_DEADLINE_YEARS.get(number)})
            frozen.append({"candidate": number, "sweep_id": candidate["sweep_id"], "family": candidate.get("family"),
                           "estimate": check["estimate"], "would_be_admitted_today": check["admitted"],
                           "reasons": check["reasons"],
                           "status": "grandfathered: registered on 2026-11-10 under its original terms"})
    except Exception as exc:
        logger.warning("experiment admission: frozen spec unreadable: %s", exc.__class__.__name__)
    return {
        "enabled": enabled(), "budget": budget(),
        "rule": {"t_required": T_REQUIRED, "effect_floor_bps": EFFECT_FLOOR_BPS, "same_date_cap": SAME_DATE_CAP,
                 "inputs": measured_inputs(db),
                 "text": ("An experiment starts only if it can reach its verdict within the limit under favourable "
                          "inputs, its family is not closed in the hypothesis register (or the register records new "
                          "evidence), and the run has compute left.")},
        "decisions": decisions,
        "counts": {name: sum(1 for item in decisions if item.get("decision") == name)
                   for name in ("admitted", "refused")},
        "frozen_forward_experiments": frozen, "paper_only": True, "live_authorized": False,
    }
