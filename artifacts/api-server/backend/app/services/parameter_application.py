"""Loss-driven research agenda and governed paper parameter application.

Agenda: recent attributed paper losses are grouped by strategy type; the
types losing the most (net, costs-aware) are researched first, on the
symbols that lost the most, with the experiment proposals that address the
dominant loss causes ordered first.  Experiment gates are unchanged.

Governed application: an experiment that passes the existing improvement /
drawdown / multiple-testing gates on its untouched validation split does NOT
edit the strategy.  It becomes an immutable, versioned ``StockParameterSet``
whose lineage names the experiment and the attributed losses behind it.  The
set then has to clear a fresh governed learning cycle (fresh dataset holdout,
new challenger, forward paper trial, promotion gates) that carries the same
lineage.  Only after that cycle is promoted is the set ``applied``: the paper
autotrader overlays its parameters at signal time (the strategy row is never
edited in place).  A failed cycle rejects the set.  Paper only.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import StockLearningCycle, StockTrainingJob, Strategy
from app.models.loss_feedback import StockParameterApplication, StockParameterSet
from app.services.audit import write_audit_log

UTC = timezone.utc
ACTOR = "loss_feedback"
OPEN_STATES = {"proposed", "waiting", "cycle_scheduled", "cycle_running"}
TERMINAL_STATES = {"applied", "rejected", "superseded"}
FAILED_CYCLE_STATUSES = {"demoted", "rolled_back", "failed", "complete"}
AGENDA_SOURCES = ("scheduled_round_trip", "exploration_round_trip")
# Forward-trial (ML) strategies are retrained by the learning cycle itself;
# their training algorithm and features are out of scope for parameter research.
NON_PARAMETRIC_TYPES = {"stock_forward_trial", "unattributed"}


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def parameters_digest(parameters: dict) -> str:
    return hashlib.sha256(json.dumps(parameters, sort_keys=True, default=str).encode()).hexdigest()


# ----------------------------------------------------------------- agenda

def build_loss_agenda(db: Session, *, now: Optional[datetime] = None,
                      lookback_days: Optional[int] = None) -> dict:
    """Strategy types ranked by recent attributed losses (worst first)."""
    from app.services.trade_attribution import current_attributions

    now = now or datetime.now(UTC)
    days = lookback_days or settings.loss_feedback_lookback_days
    rows = current_attributions(db, since=now - timedelta(days=days), sources=AGENDA_SOURCES)
    known_types = set(db.scalars(select(Strategy.strategy_type)).all())
    groups: dict[str, list] = defaultdict(list)
    excluded: dict[str, int] = defaultdict(int)
    for row in rows:
        if row.strategy_type in NON_PARAMETRIC_TYPES or row.strategy_type not in known_types:
            excluded[row.strategy_type] += 1
            continue
        groups[row.strategy_type].append(row)
    items = []
    for strategy_type, members in groups.items():
        losses = [row for row in members if row.is_loss]
        if not losses:
            continue

        def ret(row) -> float:
            return float(row.net_return if row.net_return is not None else row.gross_return)

        loss_return = sum(ret(row) for row in losses)
        by_symbol: dict[str, float] = defaultdict(float)
        by_cause: dict[str, float] = defaultdict(float)
        marginal = 0
        for row in losses:
            by_symbol[row.symbol] += ret(row)
            by_cause[row.exit_cause] += ret(row)
            if ((row.detail or {}).get("signal_quality") or {}).get("bucket") == "marginal":
                marginal += 1
        causes = [cause for cause, _value in sorted(by_cause.items(), key=lambda pair: (pair[1], pair[0]))]
        focus = list(causes)
        if marginal * 2 >= len(losses):
            focus.insert(1 if focus else 0, "marginal_signal")
        items.append({
            "strategy_type": strategy_type,
            "trades": len(members),
            "losses": len(losses),
            "loss_return": round(loss_return, 8),
            "net_return": round(sum(ret(row) for row in members), 8),
            "symbols": [symbol for symbol, _value in sorted(by_symbol.items(), key=lambda pair: (pair[1], pair[0]))]
            [: settings.loss_agenda_max_symbols],
            "loss_by_exit_cause": {key: round(value, 8) for key, value in sorted(by_cause.items())},
            "marginal_signal_losses": marginal,
            "focus": focus,
            "attribution_ids": sorted(row.id for row in losses)[-100:],
        })
    items.sort(key=lambda item: (item["loss_return"], -item["losses"], item["strategy_type"]))
    items = items[: settings.loss_agenda_max_strategies]
    for rank, item in enumerate(items, start=1):
        item["rank"] = rank
    return {"as_of": now.isoformat(), "window_days": days, "items": items,
            "excluded": dict(sorted(excluded.items())), "attributions_considered": len(rows),
            "paper_only": True}


def agenda_strategy_order(db: Session, strategy_types: list[str], *, now: Optional[datetime] = None) -> list[str]:
    """Order strategy types with loss-implicated ones first (stable otherwise)."""
    ranked = [item["strategy_type"] for item in build_loss_agenda(db, now=now)["items"]]
    position = {key: index for index, key in enumerate(ranked)}
    return sorted(strategy_types, key=lambda key: (position.get(key, len(ranked)), strategy_types.index(key)))


# ----------------------------------------------------------------- overlay

class GovernedStrategyView:
    """Read-only view of a Strategy row with the applied parameter set overlaid."""

    def __init__(self, row: Any, parameters: dict, parameter_set_id: int, version: int):
        self._row = row
        self.parameters = parameters
        self.parameter_set_id = parameter_set_id
        self.parameter_set_version = version

    def __getattr__(self, name: str) -> Any:
        return getattr(self._row, name)


def applied_parameter_sets(db: Session) -> dict[str, StockParameterSet]:
    applications = db.scalars(select(StockParameterApplication).where(
        StockParameterApplication.status == "applied").order_by(StockParameterApplication.applied_at)).all()
    out: dict[str, StockParameterSet] = {}
    for application in applications:
        parameter_set = db.get(StockParameterSet, application.parameter_set_id)
        if parameter_set is not None:
            out[application.strategy_type] = parameter_set
    return out


def parameter_overrides(db: Session) -> dict[str, dict]:
    return {key: {"parameters": dict(value.parameters), "parameter_set_id": value.id, "version": value.version}
            for key, value in applied_parameter_sets(db).items()}


def overlay(row: Any, overrides: dict[str, dict]) -> Any:
    override = overrides.get(getattr(row, "strategy_type", None))
    if not override:
        return row
    return GovernedStrategyView(row, override["parameters"], override["parameter_set_id"], override["version"])


def effective_parameters(db: Session, strategy: Strategy) -> dict:
    override = parameter_overrides(db).get(strategy.strategy_type)
    return dict(override["parameters"]) if override else dict(strategy.parameters or {})


# ----------------------------------------------------------------- proposals

def _history(row: StockParameterApplication, event: str, now: datetime, **details) -> None:
    row.history = list(row.history or []) + [{"event": event, "at": now.isoformat(), **details}]


def propose_parameter_set(db: Session, *, strategy_type: str, candidate: dict, agenda_item: dict,
                          symbol: str, now: Optional[datetime] = None) -> dict:
    """Version a promoted experiment candidate and open its governed application."""
    now = now or datetime.now(UTC)
    strategy = db.scalar(select(Strategy).where(Strategy.strategy_type == strategy_type))
    if strategy is None:
        return {"status": "skipped", "reason": f"unknown strategy {strategy_type}"}
    open_row = db.scalar(select(StockParameterApplication).where(
        StockParameterApplication.strategy_type == strategy_type,
        StockParameterApplication.status.in_(OPEN_STATES)))
    if open_row is not None:
        return {"status": "deferred", "reason": "another parameter set for this strategy is still under governance",
                "parameter_set_id": open_row.parameter_set_id}
    base = dict(candidate.get("base_parameters") or effective_parameters(db, strategy))
    parameters = base | dict(candidate["parameters"])
    if parameters == base:
        return {"status": "skipped", "reason": "candidate equals the effective parameters"}
    sha = parameters_digest(parameters)
    existing = db.scalar(select(StockParameterSet).where(
        StockParameterSet.strategy_type == strategy_type, StockParameterSet.parameters_sha256 == sha))
    if existing is not None:
        return {"status": "skipped", "reason": "identical parameter set was already governed",
                "parameter_set_id": existing.id}
    version = int(db.scalar(select(func.max(StockParameterSet.version)).where(
        StockParameterSet.strategy_type == strategy_type)) or 0) + 1
    applied = applied_parameter_sets(db).get(strategy_type)
    lineage = {
        "experiment_id": candidate.get("experiment_id"),
        "experiment_name": candidate.get("experiment_name"),
        "experiment_score": candidate.get("score"),
        "symbol": symbol,
        "agenda": {key: agenda_item.get(key) for key in
                   ("rank", "trades", "losses", "loss_return", "loss_by_exit_cause", "focus")},
        "attribution_ids": list(agenda_item.get("attribution_ids") or []),
        "supersedes_parameter_set_id": applied.id if applied else None,
        "proposed_at": now.isoformat(),
    }
    parameter_set = StockParameterSet(
        strategy_type=strategy_type, strategy_id=strategy.id, version=version, parameters=parameters,
        parameters_sha256=sha, base_parameters=base, experiment_id=candidate.get("experiment_id"),
        lineage=lineage, created_by=ACTOR, paper_only=True,
    )
    db.add(parameter_set)
    db.flush()
    application = StockParameterApplication(
        parameter_set_id=parameter_set.id, strategy_type=strategy_type, status="proposed",
        next_attempt_at=now, last_reason="Awaiting a governed learning cycle", history=[],
    )
    _history(application, "proposed", now, experiment_id=candidate.get("experiment_id"))
    db.add(application)
    write_audit_log(db, event_type="parameter_application", action="propose", status="complete",
                    message=f"{strategy_type} parameter set v{version} proposed from experiment "
                            f"{candidate.get('experiment_name')}", entity_type="strategy", entity_id=strategy.id,
                    payload={"parameter_set_id": parameter_set.id, "version": version, "lineage": lineage,
                             "paper_only": True, "live_authorized": False})
    db.flush()
    return {"status": "proposed", "parameter_set_id": parameter_set.id, "version": version}


# ----------------------------------------------------------------- governance

def _cycle_lineage(parameter_set: StockParameterSet) -> dict:
    return {
        "kind": "paper_parameter_set",
        "parameter_set_id": parameter_set.id,
        "strategy_type": parameter_set.strategy_type,
        "version": parameter_set.version,
        "parameters_sha256": parameter_set.parameters_sha256,
        "experiment_id": parameter_set.experiment_id,
        "attribution_ids": list((parameter_set.lineage or {}).get("attribution_ids") or []),
    }


def _start_cycle(db: Session, row: StockParameterApplication, now: datetime) -> None:
    from app.services.stock_learning_cycle import StockTrainingError, create_learning_cycle
    from app.services.stock_training_jobs import enqueue_stock_training_job

    parameter_set = db.get(StockParameterSet, row.parameter_set_id)
    symbols = list(settings.stock_learning_default_symbols)
    try:
        cycle, duplicate = create_learning_cycle(
            db, symbols=symbols, cutoff_at=now.date(), horizon_days=5,
            provider=settings.stock_learning_default_provider, actor=ACTOR, trigger="scheduled",
            seed=int(parameter_set.parameters_sha256[:8], 16) % 1_000_000, lineage=_cycle_lineage(parameter_set),
        )
    except StockTrainingError as exc:
        row.status = "waiting"
        row.next_attempt_at = now + timedelta(hours=24)
        row.last_reason = f"Governed cycle could not be created: {exc}"
        _history(row, "create_blocked", now, reason=str(exc))
        return
    row.cycle_id = cycle.cycle_id
    row.status = "cycle_scheduled"
    row.next_attempt_at = None
    row.last_reason = "Governed cycle scheduled (fresh holdout, challenger, forward trial, promotion)"
    _history(row, "cycle_scheduled", now, cycle_id=cycle.cycle_id, deduplicated=duplicate)
    write_audit_log(db, event_type="parameter_application", action="schedule_cycle", status="complete",
                    message=row.last_reason, entity_type="strategy", entity_id=parameter_set.strategy_id,
                    payload={"cycle_id": cycle.cycle_id, "parameter_set_id": parameter_set.id, "paper_only": True})
    db.flush()
    if cycle.training_job_id and not duplicate and cycle.status == "queued":
        job = db.get(StockTrainingJob, cycle.training_job_id)
        if job:
            enqueue_stock_training_job(db, job)


def _apply(db: Session, row: StockParameterApplication, cycle: StockLearningCycle, now: datetime) -> None:
    for previous in db.scalars(select(StockParameterApplication).where(
            StockParameterApplication.strategy_type == row.strategy_type,
            StockParameterApplication.status == "applied")).all():
        previous.status = "superseded"
        previous.last_reason = f"Superseded by parameter set {row.parameter_set_id}"
        _history(previous, "superseded", now, by_parameter_set_id=row.parameter_set_id)
    row.status = "applied"
    row.applied_at = now
    row.last_reason = "Governed cycle passed every gate and was promoted; parameters active for paper signals"
    _history(row, "applied", now, cycle_id=cycle.cycle_id)
    parameter_set = db.get(StockParameterSet, row.parameter_set_id)
    write_audit_log(db, event_type="parameter_application", action="apply", status="complete",
                    message=f"{row.strategy_type} parameter set v{parameter_set.version} applied to paper",
                    entity_type="strategy", entity_id=parameter_set.strategy_id,
                    payload={"cycle_id": cycle.cycle_id, "parameter_set_id": parameter_set.id,
                             "lineage": parameter_set.lineage, "paper_only": True, "live_authorized": False})


def _advance(db: Session, row: StockParameterApplication, now: datetime) -> None:
    from app.services.stock_learning_cycle import _blocked_only_on_approval

    if row.cycle_id and row.status in {"cycle_scheduled", "cycle_running"}:
        cycle = db.get(StockLearningCycle, row.cycle_id)
        if cycle is None:
            row.status, row.next_attempt_at, row.cycle_id = "waiting", now, None
            return
        if cycle.status == "promoted":
            _apply(db, row, cycle, now)
            return
        if cycle.status in {"blocked", "deferred"} and not cycle.model_run_id:
            row.status = "waiting"
            row.next_attempt_at = now + timedelta(hours=24)
            row.last_reason = f"Cycle {cycle.status} before training: {cycle.last_reason or 'no reason'}"
            _history(row, "not_evaluated", now, cycle_id=cycle.cycle_id, cycle_status=cycle.status)
            row.cycle_id = None
            return
        if cycle.status in FAILED_CYCLE_STATUSES or (
            cycle.status == "blocked" and not _blocked_only_on_approval(cycle)
        ):
            row.status = "rejected"
            row.last_reason = f"Governed cycle ended {cycle.status} without promotion: {cycle.last_reason or 'no reason'}"
            _history(row, "rejected", now, cycle_id=cycle.cycle_id, cycle_status=cycle.status)
            write_audit_log(db, event_type="parameter_application", action="reject", status="complete",
                            message=row.last_reason, entity_type="strategy", entity_id=None,
                            payload={"cycle_id": cycle.cycle_id, "parameter_set_id": row.parameter_set_id,
                                     "paper_only": True})
            return
        row.status = "cycle_running"
        row.last_reason = f"Governed cycle is {cycle.status} at {cycle.stage}"
        return
    if row.status in {"proposed", "waiting"} and (row.next_attempt_at is None or _utc(row.next_attempt_at) <= now):
        _start_cycle(db, row, now)


def advance_parameter_applications(db: Session, *, now: Optional[datetime] = None) -> dict:
    from app.services.stock_learning_cycle import scheduled_learning_control_projection

    now = now or datetime.now(UTC)
    if not settings.paper_parameter_application_enabled:
        return {"status": "disabled"}
    control = scheduled_learning_control_projection(db)
    if control.get("paused"):
        return {"status": "paused", "reason": control.get("pause_reason")}
    rows = db.scalars(select(StockParameterApplication).where(
        StockParameterApplication.status.in_(OPEN_STATES)).order_by(StockParameterApplication.id)).all()
    for row in rows:
        _advance(db, row, now)
    db.flush()
    return {"status": "complete", "applications": [application_out(db, row) for row in rows]}


def application_out(db: Session, row: StockParameterApplication) -> dict:
    parameter_set = db.get(StockParameterSet, row.parameter_set_id)
    return {
        "id": row.id, "strategy_type": row.strategy_type, "status": row.status, "cycle_id": row.cycle_id,
        "applied_at": _utc(row.applied_at).isoformat() if row.applied_at else None,
        "next_attempt_at": _utc(row.next_attempt_at).isoformat() if row.next_attempt_at else None,
        "last_reason": row.last_reason, "history": row.history or [],
        "parameter_set": {
            "id": parameter_set.id, "version": parameter_set.version, "parameters": parameter_set.parameters,
            "base_parameters": parameter_set.base_parameters, "experiment_id": parameter_set.experiment_id,
            "parameters_sha256": parameter_set.parameters_sha256, "lineage": parameter_set.lineage,
        } if parameter_set else None,
    }


def parameter_application_projection(db: Session, *, limit: int = 25) -> dict:
    rows = db.scalars(select(StockParameterApplication).order_by(StockParameterApplication.id.desc()).limit(limit)).all()
    return {
        "enabled": settings.paper_parameter_application_enabled,
        "active": {key: value["version"] for key, value in parameter_overrides(db).items()},
        "applications": [application_out(db, row) for row in rows],
        "paper_only": True, "edits_strategy_rows": False,
    }


# ----------------------------------------------------------------- nightly research

def run_loss_driven_experiments(db: Session, *, now: Optional[datetime] = None, max_candidates: int = 3) -> dict:
    """Nightly experiment pass ordered by the loss agenda; may propose sets."""
    from app.services.experiments import run_strategy_experiments

    now = now or datetime.now(UTC)
    agenda = build_loss_agenda(db, now=now)
    runs = []
    for item in agenda["items"]:
        strategy = db.scalar(select(Strategy).where(Strategy.strategy_type == item["strategy_type"]))
        if strategy is None:
            continue
        base = effective_parameters(db, strategy)
        for symbol in item["symbols"]:
            entry = {"strategy_type": item["strategy_type"], "symbol": symbol}
            try:
                result = run_strategy_experiments(
                    db, symbol=symbol, strategy_slug=item["strategy_type"], max_candidates=max_candidates,
                    apply_promotions=settings.paper_parameter_application_enabled, governed=True,
                    loss_focus=item["focus"], current_parameters_override=base,
                )
            except Exception as exc:  # one symbol must not stop the agenda
                db.rollback()
                runs.append(entry | {"status": "failed", "error": f"{exc.__class__.__name__}: {exc}"})
                continue
            entry |= {"status": "complete", "experiments": len(result.get("experiments") or []),
                      "promoted_candidate": bool(result.get("promoted_candidate"))}
            candidate = result.get("promoted_candidate")
            if candidate:
                entry["application"] = propose_parameter_set(
                    db, strategy_type=item["strategy_type"], candidate=candidate, agenda_item=item,
                    symbol=symbol, now=now)
                db.commit()
            runs.append(entry)
    return {"agenda": agenda, "runs": runs}
