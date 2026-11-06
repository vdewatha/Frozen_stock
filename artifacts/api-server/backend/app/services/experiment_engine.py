"""Lessons become experiments, experiments become governed changes, and every change gets a verdict.

Until now the loop from a repeating loss cause to a behaviour change ran by
hand: ``loss_diagnosis.repeating_failures`` named the lesson, a person wrote a
pre-registered backtest (``reports/market-gate-20261106.md``,
``vix-gate-20261106.md``, ``slot-allocation-20261106.md``), and a seed step
applied the result.  This module lets the bot run that loop itself.

1. **Lesson -> experiment.**  Each repeating cause or contributing flag maps
   to a template (``TEMPLATES``): a knob the bot can turn and the candidate
   values to try.  ``missed_market_move`` tests the SPY-trend gate,
   ``high_vix_entry`` the VIX gate, ``missed_earnings`` the earnings
   blackout.  A lesson without a template is reported, not acted on.
2. **Pre-registered decision rule** (``decide``; fixed here, before any run,
   the same rule the hand-run reports used under the profit goal).  Against
   the strategy's *current* setup, a candidate is adopted only if, in every
   window (all history, 2015+, 2020+): it keeps at least
   ``MIN_TRADES_PER_WINDOW`` trades, its net expectancy per trade is not
   lower, its total net profit (trades x expectancy) is not lower, and its
   portfolio max drawdown is at most ``DRAWDOWN_TOLERANCE`` x the current
   one.  Among eligible candidates the one with the largest minimum
   expectancy gain wins; none eligible = keep the current setup.  Turning a
   gate *off* is always one of the candidates, so a gate that costs profit can
   be removed by the same rule that added it.
3. **Governed adoption.**  An adopted candidate is written to the strategy
   row's parameters with the same ``strategy_governance`` audit action the
   one-time seed steps check, so the seed never overrides the bot's decision.
   Every experiment, adoption and verdict is an ``experiment_engine`` audit
   row: the ledger needs no migration and is the only state.
4. **Forward verdict** (``review_changes``).  After ``min_trades`` closed
   paper trades on the changed strategy, the realised expectancy and the share
   of losses with the triggering cause are compared with the 20 trades before
   the change.  ``working`` keeps it; ``not_helping`` (expectancy at least
   ``REVERT_EXPECTANCY_BPS`` worse and the cause no rarer) reverts it, with an
   audit trail; otherwise it keeps watching until ``max_trades`` and closes as
   ``inconclusive``.

Paper only.  Nothing here places, sizes or blocks an order directly; it only
changes the entry-gate parameters the autotrader already honours.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Optional

import numpy as np
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import AuditLog, Strategy
from app.services.audit import write_audit_log

logger = logging.getLogger(__name__)
UTC = timezone.utc
EVENT = "experiment_engine"
ACTION_RUN = "run"
ACTION_EXPERIMENT = "experiment"
ACTION_ADOPTED = "change_adopted"
ACTION_VERDICT = "change_verdict"
GOVERNANCE_EVENT = "strategy_governance"
ACTIVE_STATUSES = ("paper_trading_active", "paper_trading_candidate")
SOURCES = ("scheduled_round_trip", "exploration_round_trip")
WINDOWS = ("all", "2015+", "2020+")
MIN_TRADES_PER_WINDOW = 30
DRAWDOWN_TOLERANCE = 1.10
REVERT_EXPECTANCY_BPS = 10.0
RECENT_TRADES = 20
BASELINE = "baseline"
RULE = ("Adopt a candidate only if, in every window (all, 2015+, 2020+), it keeps >= 30 trades, net expectancy per "
        "trade is not lower, total net profit is not lower and portfolio max drawdown is <= 1.10x the current "
        "setup's; among eligible candidates take the largest minimum expectancy gain; none eligible = no change.")


# ----------------------------------------------------------------- templates

@dataclass(frozen=True)
class Template:
    name: str
    triggers: frozenset
    key: str
    governance_action: str
    candidates: tuple
    label: Callable[[dict], str]
    describe: Callable[[dict], str]


def _market_label(candidate: dict) -> str:
    return "off" if not candidate["market_gate_sma"] else f"sma{candidate['market_gate_sma']}"


def _market_describe(candidate: dict) -> str:
    n = candidate["market_gate_sma"]
    return "no market gate" if not n else f"no new longs while SPY is at or below its {n}-day average"


def _vix_label(candidate: dict) -> str:
    return "off" if not candidate["vix_gate"] else f"{candidate['vix_gate']}{candidate['vix_level']:g}"


def _vix_describe(candidate: dict) -> str:
    if not candidate["vix_gate"]:
        return "no VIX gate"
    when = "below" if candidate["vix_gate"] == "below" else "at or above"
    return f"BUY only when the VIX close is {when} {candidate['vix_level']:g}"


def _earnings_label(candidate: dict) -> str:
    return "off" if not candidate["earnings_blackout_sessions"] else f"blackout{candidate['earnings_blackout_sessions']}"


def _earnings_describe(candidate: dict) -> str:
    n = candidate["earnings_blackout_sessions"]
    return "no earnings blackout" if not n else f"no entry when an earnings report falls within the next {n} sessions"


TEMPLATES = (
    Template("market_gate", frozenset({"missed_market_move", "market_down"}), "market_gate_sma", "market_gate_update",
             ({"market_gate_sma": 0}, {"market_gate_sma": 10}, {"market_gate_sma": 20}, {"market_gate_sma": 50}),
             _market_label, _market_describe),
    Template("vix_gate", frozenset({"high_vix_entry"}), "vix_gate", "vix_gate_update",
             ({"vix_gate": None, "vix_level": None}, {"vix_gate": "below", "vix_level": 20.0},
              {"vix_gate": "below", "vix_level": 25.0}, {"vix_gate": "below", "vix_level": 30.0}),
             _vix_label, _vix_describe),
    Template("earnings_blackout", frozenset({"missed_earnings"}), "earnings_blackout_sessions",
             "earnings_blackout_update",
             ({"earnings_blackout_sessions": 0}, {"earnings_blackout_sessions": 10}, {"earnings_blackout_sessions": 20}),
             _earnings_label, _earnings_describe),
)
TEMPLATE_BY_NAME = {template.name: template for template in TEMPLATES}


def templates_for(triggers: Iterable[str]) -> list[Template]:
    wanted = set(triggers)
    return [template for template in TEMPLATES if template.triggers & wanted]


def current_setup(parameters: Optional[Mapping]) -> dict:
    """The strategy's live gate setup; the earnings blackout falls back to the account-wide setting."""
    from app.services.gate_replay import setup_from_parameters

    setup = setup_from_parameters(parameters)
    if (parameters or {}).get("earnings_blackout_sessions") is None:
        setup["earnings_blackout_sessions"] = int(settings.paper_earnings_blackout_sessions or 0)
    return setup


# ------------------------------------------------------------ decision rule

def decide(rows: Mapping[str, Mapping[str, dict]], baseline: str = BASELINE, *,
           min_trades: int = MIN_TRADES_PER_WINDOW, drawdown_tolerance: float = DRAWDOWN_TOLERANCE) -> dict:
    """The pre-registered rule: ``rows[label][window]`` -> adopted label (or None) and why."""
    base = rows[baseline]
    eligible: dict[str, float] = {}
    failed: dict[str, str] = {}
    for label, candidate in rows.items():
        if label == baseline:
            continue
        gains = []
        reason = None
        for window in WINDOWS:
            b, c = base.get(window) or {}, candidate.get(window) or {}
            if (c.get("trades") or 0) < min_trades:
                reason = f"{window}: only {c.get('trades') or 0} trades (< {min_trades})"
                break
            if c.get("expectancy_bps") is None or b.get("expectancy_bps") is None:
                reason = f"{window}: expectancy unavailable"
                break
            if c["expectancy_bps"] < b["expectancy_bps"]:
                reason = f"{window}: expectancy {c['expectancy_bps']:+.1f} < {b['expectancy_bps']:+.1f} bps"
                break
            if (c.get("total_profit_bps") or 0.0) < (b.get("total_profit_bps") or 0.0):
                reason = f"{window}: total profit {c.get('total_profit_bps', 0):+.0f} < {b.get('total_profit_bps', 0):+.0f} bps"
                break
            if (c.get("portfolio_max_drawdown") or 0.0) > drawdown_tolerance * (b.get("portfolio_max_drawdown") or 0.0):
                reason = (f"{window}: drawdown {c.get('portfolio_max_drawdown', 0):.1%} > "
                          f"{drawdown_tolerance:.2f}x {b.get('portfolio_max_drawdown', 0):.1%}")
                break
            gains.append(c["expectancy_bps"] - b["expectancy_bps"])
        if reason is None:
            eligible[label] = round(min(gains), 2)
        else:
            failed[label] = reason
    adopted = max(eligible, key=eligible.get) if eligible else None
    return {"adopted": adopted, "eligible": eligible, "failed": failed, "rule": RULE}


# ------------------------------------------------------------------ planning

def _history_rows(db: Session, action: str, limit: Optional[int] = None) -> list[AuditLog]:
    query = db.query(AuditLog).filter(AuditLog.event_type == EVENT, AuditLog.action == action).order_by(AuditLog.id.desc())
    if limit:
        query = query.limit(limit)
    return query.all()


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()
    return str(value)


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    stamp = datetime.fromisoformat(str(value))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def plan_experiments(lessons: Iterable[dict], strategies: Mapping[str, Any], *, now: datetime,
                     history: Iterable[dict], cooldown_days: int, max_experiments: int) -> dict:
    """Which (strategy, template) experiments the repeating failures call for.

    ``history`` is earlier experiment payloads (``experiment_history``).  A
    pair run within ``cooldown_days`` is skipped unless the lesson has grown
    by at least 3 losses since, so a run does not repeat itself weekly on the
    same evidence.
    """
    from app.services.gate_replay import replayable

    latest: dict[tuple[str, str], dict] = {}
    for item in history:
        key = (item.get("strategy_type"), item.get("template"))
        if key not in latest:
            latest[key] = item
    wanted: dict[tuple[str, str], dict] = {}
    untemplated, skipped = [], []
    for lesson in lessons:
        triggers = {lesson["cause"], *((lesson.get("contributing") or {}).keys())}
        matched = templates_for(triggers)
        if not matched:
            untemplated.append({"strategy_type": lesson["strategy_type"], "cause": lesson["cause"],
                                "count": lesson["count"], "proposal": lesson.get("proposal"),
                                "reason": "no experiment template for this cause yet"})
            continue
        targets = [lesson["strategy_type"]] if lesson["strategy_type"] != "all" else sorted(strategies)
        for template in matched:
            trigger = lesson["cause"] if lesson["cause"] in template.triggers else next(
                flag for flag in (lesson.get("contributing") or {}) if flag in template.triggers)
            for target in targets:
                if target not in strategies:
                    skipped.append({"strategy_type": target, "template": template.name, "reason": "not an active strategy"})
                    continue
                if not replayable(target):
                    skipped.append({"strategy_type": target, "template": template.name,
                                    "reason": "strategy cannot be replayed over daily history"})
                    continue
                key = (target, template.name)
                candidate = {"strategy_type": target, "template": template.name, "trigger": trigger,
                             "lesson_count": int(lesson["count"]), "lesson_scope": lesson["strategy_type"],
                             "lesson": lesson.get("lesson")}
                if key not in wanted or wanted[key]["lesson_count"] < candidate["lesson_count"]:
                    wanted[key] = candidate
    planned, deferred = [], []
    for key, candidate in sorted(wanted.items(), key=lambda pair: (-pair[1]["lesson_count"], pair[0])):
        previous = latest.get(key)
        if previous:
            ran = _parse(previous.get("ran_at"))
            grown = candidate["lesson_count"] - int(previous.get("lesson_count") or 0) >= 3
            if ran and now - ran < timedelta(days=cooldown_days) and not grown:
                skipped.append({**candidate, "reason": f"tested {ran.date().isoformat()}; lesson has not grown by 3"})
                continue
        if len(planned) < max_experiments:
            planned.append(candidate)
        else:
            deferred.append({**candidate, "reason": "over this run's experiment budget"})
    return {"planned": planned, "deferred": deferred, "skipped": skipped, "untemplated": untemplated}


# ---------------------------------------------------------------- experiments

def _clean(value: Any) -> Any:
    """JSON-safe copy: numpy scalars to Python, NaN to None."""
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, datetime):
        return _iso(value)
    return value


def run_experiment(strategy: Any, template: Template, *, frames: Mapping, spy, vix, replays, simulate) -> dict:
    """Replay the strategy under its current setup and each candidate; apply the rule."""
    from app.services.gate_replay import window_rows

    baseline = current_setup(getattr(strategy, "parameters", None))
    setups = {BASELINE: baseline}
    for candidate in template.candidates:
        setup = {**baseline, **candidate}
        if setup == baseline:
            continue
        setups[template.label(candidate)] = setup
    rows = {}
    for label, setup in setups.items():
        trades = simulate(strategy.strategy_type, frames, spy, setup, replays, vix=vix)
        rows[label] = window_rows(trades, frames)
    decision = decide(rows)
    adopted = decision["adopted"]
    return {"strategy_type": strategy.strategy_type, "strategy_id": strategy.id, "template": template.name,
            "key": template.key, "baseline_setup": baseline, "setups": setups, "rows": rows, "decision": decision,
            "adopted": adopted, "adopted_setup": setups.get(adopted),
            "adopted_description": template.describe(setups[adopted]) if adopted else None}


def _recent_performance(rows: list, trigger: str, *, limit: Optional[int] = None) -> dict:
    """Expectancy and the share of losses with ``trigger`` as cause or contributing flag."""
    rows = sorted(rows, key=lambda row: _parse(_iso(row.closed_at)))
    if limit:
        rows = rows[-limit:]
    if not rows:
        return {"trades": 0, "expectancy_bps": None, "cause_share": None, "losses": 0, "cause_losses": 0}
    returns = [float(row.net_return if row.net_return is not None else row.gross_return) for row in rows]
    losses = [row for row in rows if row.is_loss]
    hits = 0
    for row in losses:
        diagnosis = (row.detail or {}).get("diagnosis") or {}
        if diagnosis.get("cause") == trigger or trigger in (diagnosis.get("contributing") or []):
            hits += 1
    return {"trades": len(rows), "expectancy_bps": round(sum(returns) / len(rows) * 10_000, 1),
            "cause_share": round(hits / len(losses), 4) if losses else None,
            "losses": len(losses), "cause_losses": hits,
            "first_closed_at": _iso(rows[0].closed_at), "last_closed_at": _iso(rows[-1].closed_at)}


def _apply_parameters(row: Strategy, changes: Mapping[str, Any]) -> dict:
    """Set (or remove, for None) parameter keys; returns the previous values."""
    params = dict(row.parameters or {})
    before = {key: params.get(key) for key in changes}
    for key, value in changes.items():
        if value is None:
            params.pop(key, None)
        else:
            params[key] = value
    row.parameters = params
    return before


def adopt_change(db: Session, strategy: Strategy, experiment: dict, *, now: datetime, recent_rows: list) -> dict:
    template = TEMPLATE_BY_NAME[experiment["template"]]
    candidate = {key: experiment["adopted_setup"][key] for key in template.candidates[0]}
    before = _apply_parameters(strategy, candidate)
    after = {key: (strategy.parameters or {}).get(key) for key in candidate}
    change_id = hashlib.sha256(f"{strategy.id}:{template.name}:{now.isoformat()}".encode()).hexdigest()[:12]
    write_audit_log(
        db, event_type=GOVERNANCE_EVENT, entity_type="strategy", entity_id=strategy.id,
        action=template.governance_action, status="complete",
        message=(f"experiment {change_id}: {experiment['adopted_description']}; adopted by the experiment engine "
                 f"after the {experiment['trigger']} lesson repeated and the pre-registered backtest rule passed. "
                 "Sells and exits are unchanged."),
        payload=_clean({**candidate, "source": "experiment_engine", "change_id": change_id, "paper_only": True}),
    )
    recent = _recent_performance(recent_rows, experiment["trigger"], limit=RECENT_TRADES)
    change = {
        "change_id": change_id, "strategy_id": strategy.id, "strategy_type": strategy.strategy_type,
        "template": template.name, "key": template.key, "trigger": experiment["trigger"],
        "description": experiment["adopted_description"], "before": before, "after": after,
        "adopted_at": _iso(now), "state": "watching",
        "expected": {"baseline": experiment["rows"][BASELINE], "adopted": experiment["rows"][experiment["adopted"]],
                     "label": experiment["adopted"]},
        "recent_before": recent,
        "watch": {"min_trades": settings.experiment_verdict_min_trades,
                  "max_trades": settings.experiment_verdict_max_trades, "cause": experiment["trigger"],
                  "revert_if_expectancy_worse_by_bps": REVERT_EXPECTANCY_BPS},
        "paper_only": True,
    }
    write_audit_log(
        db, event_type=EVENT, entity_type="strategy", entity_id=strategy.id, action=ACTION_ADOPTED, status="watching",
        message=f"{strategy.strategy_type}: {experiment['adopted_description']} (change {change_id})",
        payload=_clean(change),
    )
    return change


def experiment_history(db: Session, *, limit: Optional[int] = None) -> list[dict]:
    return [row.payload or {} for row in _history_rows(db, ACTION_EXPERIMENT, limit)]


def _load_vix(start: str):
    from app.services.strategies.vix_gate import download_vix_history

    return download_vix_history(start)


def run_experiment_engine(db: Session, *, now: Optional[datetime] = None, loader=None, earnings_loader=None,
                          vix_loader=None, simulate=None) -> dict:
    """Weekly pass: plan from the repeating failures, replay, decide, adopt (paper only)."""
    from app.services.loss_diagnosis import repeating_failures
    from app.services.trade_attribution import current_attributions

    now = now or datetime.now(UTC)
    base = {"paper_only": True, "live_authorized": False, "rule": RULE}
    if not settings.experiment_engine_enabled:
        return base | {"status": "disabled"}
    attributions = current_attributions(db, sources=SOURCES)
    lessons = repeating_failures(attributions)
    strategies = {row.strategy_type: row for row in db.query(Strategy).filter(
        Strategy.current_status.in_(ACTIVE_STATUSES)).order_by(Strategy.id).all()}
    plan = plan_experiments(lessons, strategies, now=now, history=experiment_history(db),
                            cooldown_days=settings.experiment_engine_cooldown_days,
                            max_experiments=settings.experiment_engine_max_experiments)
    experiments, adopted, errors = [], [], []
    if plan["planned"]:
        from app.services import gate_replay
        from app.services import meta_label_primary as primary
        from app.services.strategy_discovery import load_frames

        simulate = simulate or gate_replay.simulate
        symbols = sorted({s.upper() for s in settings.paper_execution_symbols})
        frames, missing = load_frames(symbols, loader=loader, start=settings.experiment_engine_history_start,
                                      earnings_loader=earnings_loader)
        spy = frames.get("SPY")
        try:
            vix = (vix_loader or _load_vix)(settings.experiment_engine_history_start)
        except Exception as exc:  # the VIX gate fails open live; replay the same way
            logger.warning("experiment engine: VIX history unavailable: %s", exc)
            vix = None
        replays = {symbol: primary.replay_signals(frame) for symbol, frame in frames.items()}
        for item in plan["planned"]:
            strategy = strategies[item["strategy_type"]]
            template = TEMPLATE_BY_NAME[item["template"]]
            try:
                if not frames or spy is None:
                    raise RuntimeError("no price history to replay")
                experiment = run_experiment(strategy, template, frames=frames, spy=spy, vix=vix, replays=replays,
                                            simulate=simulate)
            except Exception as exc:  # one experiment must not stop the run
                db.rollback()
                errors.append({**item, "error": f"{exc.__class__.__name__}: {exc}"})
                continue
            experiment.update({"trigger": item["trigger"], "lesson_count": item["lesson_count"],
                               "lesson_scope": item["lesson_scope"], "ran_at": _iso(now),
                               "symbols": len(frames), "missing_symbols": missing,
                               "applied": bool(experiment["adopted"]) and settings.experiment_engine_adoption_enabled})
            change = None
            if experiment["adopted"] and settings.experiment_engine_adoption_enabled:
                recent = [row for row in attributions if row.strategy_type == strategy.strategy_type]
                change = adopt_change(db, strategy, experiment, now=now, recent_rows=recent)
                experiment["change_id"] = change["change_id"]
                adopted.append(change)
            write_audit_log(
                db, event_type=EVENT, entity_type="strategy", entity_id=strategy.id, action=ACTION_EXPERIMENT,
                status="adopted" if change else ("eligible" if experiment["adopted"] else "rejected"),
                message=(f"{strategy.strategy_type} / {template.name} ({item['trigger']} x{item['lesson_count']}): "
                         + (f"adopt {experiment['adopted']}: {experiment['adopted_description']}" if experiment["adopted"]
                            else "no candidate beat the current setup")),
                payload=_clean(experiment),
            )
            experiments.append(experiment)
            db.commit()
    summary = {"status": "complete", "ran_at": _iso(now), "lessons": len(lessons), "planned": len(plan["planned"]),
               "experiments": [{k: e.get(k) for k in ("strategy_type", "template", "trigger", "adopted",
                                                       "adopted_description", "change_id", "applied")}
                               for e in experiments],
               "adopted": [c["change_id"] for c in adopted], "errors": errors, "deferred": plan["deferred"],
               "skipped": plan["skipped"], "untemplated": plan["untemplated"],
               "adoption_enabled": settings.experiment_engine_adoption_enabled}
    write_audit_log(db, event_type=EVENT, entity_type="experiment_engine", action=ACTION_RUN, status="complete",
                    message=(f"experiment engine: {len(lessons)} lessons, {len(experiments)} experiments, "
                             f"{len(adopted)} adopted, {len(plan['untemplated'])} without a template"),
                    payload=_clean(summary))
    return base | summary


# ------------------------------------------------------------------ verdicts

def adopted_changes(db: Session) -> list[dict]:
    """Every adopted change with its latest state and verdicts (newest adoption first)."""
    verdicts: dict[str, list[dict]] = {}
    for row in _history_rows(db, ACTION_VERDICT):
        payload = row.payload or {}
        verdicts.setdefault(payload.get("change_id"), []).append(payload)
    out = []
    for row in _history_rows(db, ACTION_ADOPTED):
        change = dict(row.payload or {})
        history = list(reversed(verdicts.get(change["change_id"], [])))
        change["verdicts"] = history
        if history:
            change["state"] = history[-1]["state"]
            change["verdict"] = history[-1]["verdict"]
        out.append(change)
    return out


def judge(before: Mapping, after: Mapping, *, max_trades: int) -> str:
    """``working`` / ``not_helping`` / ``inconclusive`` / ``watching``."""
    exp_b, exp_a = before.get("expectancy_bps"), after.get("expectancy_bps")
    share_b, share_a = before.get("cause_share"), after.get("cause_share")
    if exp_b is None or exp_a is None:
        return "inconclusive"
    cause_rarer = share_b is not None and (share_a is None or share_a <= share_b / 2)
    cause_no_rarer = share_b is None or (share_a is not None and share_a >= share_b)
    if exp_a < exp_b - REVERT_EXPECTANCY_BPS and cause_no_rarer:
        return "not_helping"
    if exp_a >= exp_b or cause_rarer:
        return "working"
    return "inconclusive" if after.get("trades", 0) >= max_trades else "watching"


def revert_change(db: Session, change: Mapping, *, now: datetime, reason: str) -> bool:
    strategy = db.get(Strategy, change["strategy_id"])
    if strategy is None:
        return False
    template = TEMPLATE_BY_NAME[change["template"]]
    _apply_parameters(strategy, change["before"])
    write_audit_log(
        db, event_type=GOVERNANCE_EVENT, entity_type="strategy", entity_id=strategy.id,
        action=template.governance_action, status="complete",
        message=f"experiment {change['change_id']} reverted: {reason}. Parameters restored to their values before the change.",
        payload=_clean({**change["before"], "source": "experiment_engine", "reverted_change": change["change_id"],
                        "paper_only": True}),
    )
    return True


def review_changes(db: Session, *, now: Optional[datetime] = None) -> dict:
    """Daily: give each watched change a verdict once enough paper trades closed after it."""
    from app.services.trade_attribution import current_attributions

    now = now or datetime.now(UTC)
    reviewed, pending = [], []
    for change in adopted_changes(db):
        if change.get("state") != "watching":
            continue
        adopted_at = _parse(change["adopted_at"])
        rows = [row for row in current_attributions(db, since=adopted_at, sources=SOURCES,
                                                    strategy_types=[change["strategy_type"]])
                if _parse(_iso(row.opened_at)) >= adopted_at]
        watch = change.get("watch") or {}
        min_trades = int(watch.get("min_trades") or settings.experiment_verdict_min_trades)
        max_trades = int(watch.get("max_trades") or settings.experiment_verdict_max_trades)
        after = _recent_performance(rows, change["trigger"])
        if after["trades"] < min_trades:
            pending.append({"change_id": change["change_id"], "strategy_type": change["strategy_type"],
                            "trades": after["trades"], "needed": min_trades - after["trades"]})
            continue
        verdict = judge(change.get("recent_before") or {}, after, max_trades=max_trades)
        if verdict == "watching":
            pending.append({"change_id": change["change_id"], "strategy_type": change["strategy_type"],
                            "trades": after["trades"], "needed": max_trades - after["trades"], "so_far": "mixed"})
            continue
        state = {"working": "confirmed", "not_helping": "reverted", "inconclusive": "closed"}[verdict]
        reverted = False
        if verdict == "not_helping":
            reverted = revert_change(db, change, now=now, reason=(
                f"after {after['trades']} trades expectancy {after['expectancy_bps']:+.1f} bps vs "
                f"{(change.get('recent_before') or {}).get('expectancy_bps'):+.1f} before, and {change['trigger']} "
                f"losses no rarer"))
            if not reverted:
                state = "closed"
        payload = {"change_id": change["change_id"], "strategy_id": change["strategy_id"],
                   "strategy_type": change["strategy_type"], "template": change["template"],
                   "trigger": change["trigger"], "verdict": verdict, "state": state, "reverted": reverted,
                   "before": change.get("recent_before"), "after": after, "judged_at": _iso(now), "paper_only": True}
        write_audit_log(db, event_type=EVENT, entity_type="strategy", entity_id=change["strategy_id"],
                        action=ACTION_VERDICT, status=state,
                        message=(f"{change['strategy_type']}: {change['description']} -> {verdict} after "
                                 f"{after['trades']} trades ({after['expectancy_bps']:+.1f} bps vs "
                                 f"{(change.get('recent_before') or {}).get('expectancy_bps')} before)"),
                        payload=_clean(payload))
        reviewed.append(payload)
    if reviewed:
        db.commit()
    return {"status": "complete", "reviewed": reviewed, "pending": pending, "paper_only": True}


# ---------------------------------------------------------------- projection

def scoreboard_changes(db: Session) -> list[dict]:
    """Adopted changes in the learning scoreboard's ``changes`` shape (per strategy)."""
    out = []
    for change in adopted_changes(db):
        verdict = change.get("verdict")
        out.append({"key": f"experiment:{change['change_id']}", "effective": change["adopted_at"][:10],
                    "title": f"{change['strategy_type']}: {change['description']}",
                    "watch": (f"Adopted by the experiment engine after {change['trigger']} repeated. Verdict after "
                              f"{(change.get('watch') or {}).get('min_trades')} trades: "
                              f"{verdict or 'watching'}."),
                    "strategy_type": change["strategy_type"], "state": change.get("state"), "verdict": verdict,
                    "source": "experiment_engine"})
    return out


def experiment_ledger(db: Session, *, limit: int = 30) -> dict:
    runs = _history_rows(db, ACTION_RUN, 1)
    return {"rule": RULE, "templates": [{"name": t.name, "triggers": sorted(t.triggers), "key": t.key,
                                         "candidates": [t.label(c) for c in t.candidates]} for t in TEMPLATES],
            "changes": adopted_changes(db), "experiments": experiment_history(db, limit=limit),
            "latest_run": (runs[0].payload if runs else None), "paper_only": True}
