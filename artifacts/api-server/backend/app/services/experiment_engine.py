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
   Exit templates (``stop_width``, ``time_stop``; triggered by the exit flags
   ``premature_stop``, ``gave_back_gains`` and ``held_too_long``) write the
   row's ``stop_loss_pct`` / ``max_holding_sessions``, which the strategy's
   exit policy reads (``strategy_execution_policy.exit_policy_for``); the
   broker legs of lots already open are left as they were opened, the new
   stop applies to entries after the change.
   The ``scope`` template (triggered by ``signal_wrong``) has no fixed
   candidates: they come from the strategy's latest signal autopsy
   (``signal_autopsy.scope_candidates``, a ``Template.dynamic`` callable),
   one-condition entry rules replayed on the decision bar and written as
   ``parameters["scope"]`` under ``scope_update``; the live
   ``strategies/scope.py`` wrapper turns a BUY into a HOLD when the decision
   bar fails the condition.
   Every experiment, adoption and verdict is an ``experiment_engine`` audit
   row: the ledger needs no migration and is the only state.
4. **Forward verdict** (``review_changes``).  After ``min_trades`` closed
   paper trades on the changed strategy, the realised expectancy and the share
   of losses with the triggering cause are compared with the 20 trades before
   the change.  ``working`` keeps it; ``not_helping`` (expectancy at least
   ``REVERT_EXPECTANCY_BPS`` worse and the cause no rarer) reverts it, with an
   audit trail; otherwise it keeps watching until ``max_trades`` and closes as
   ``inconclusive``.
   Every candidate value also earns a forward record in shadow from the same
   BUY decisions (``setup_shadow.py``): the verdict carries it, an
   alternative that beats the adopted value by ``REVERT_EXPECTANCY_BPS`` over
   ``min_trades`` overlapping decisions reverts the change early, and a
   candidate that beats the live setup is a ``shadow_evidence`` lesson for
   the next weekly run, replayed under the same rule.
   That is the ``fixed_20`` judge.  Beside it, every review also runs the
   always-valid judge of ``verdict_stats.py`` (``e_process_judgement``:
   betting e-processes on SPY-adjusted paired shadow differences, a harm
   test at alpha 0.10, confirmation at the e-LOND level of the change's place
   in the adoption stream, a 120-trade cap) and records it as
   ``verdict_e_process`` in each verdict and pending entry; only the judge
   named by ``experiment_verdict_method`` acts (``fixed_20`` by default).
5. **Multiplicity hurdle** (``apply_multiplicity_hurdle``).  After ``RULE``
   picks a candidate, its paired backtest gain over the live setup must
   clear t >= 3 or a deflated Sharpe > 0.95 counting every candidate ever
   tried for the strategy (``experiment_multiplicity_hurdle``); ``RULE``'s
   checks are unchanged and the hurdle is reported in the decision payload.
   The hurdle's trial count also charges the strategy family's trials made
   outside the engine (discovery library and research; ``yardstick``).
6. **The yardstick** (``yardstick.py``, step 1 of the feasibility report).
   Every experiment records each label's cost bracket (5 / 10 / 25 bps a
   round trip, the published model, a calibrated cell with 20+ fills),
   turnover, excess over a volatility-matched SPY/T-bill arm and SPY
   residual, and which label ``RULE`` would adopt at each cost; ``RULE`` keeps
   acting on the replay's own cost.  Its last sentence is the short-hold veto
   (``yardstick.short_hold_veto``), applied in ``decide``.  Every forward
   verdict carries the same bracket on the trades before and after
   (``forward_bracket``).  A change on a monthly sleeve
   (``is_monthly_sleeve``: ``parameters["sleeve_cadence"] == "monthly"``)
   skips ``fixed_20``: the sleeve's harm guardrail on daily active P&L
   against the passive arm acts instead (``sleeve_guardrail.py``), and the
   e-process stays in shadow either way.

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
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping, Optional

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import AuditLog, Strategy
from app.services.audit import write_audit_log
from app.services.sleeve_guardrail import is_monthly_sleeve  # noqa: F401  (re-exported: monthly sleeves skip fixed_20)

logger = logging.getLogger(__name__)
UTC = timezone.utc
EVENT = "experiment_engine"
ACTION_RUN = "run"
ACTION_EXPERIMENT = "experiment"
ACTION_ADOPTED = "change_adopted"
ACTION_VERDICT = "change_verdict"
GOVERNANCE_EVENT = "strategy_governance"
EXIT_POLICY_ACTION = "exit_policy_update"
SCOPE_ACTION = "scope_update"
ACTIVE_STATUSES = ("paper_trading_active", "paper_trading_candidate")
SOURCES = ("scheduled_round_trip", "exploration_round_trip")
WINDOWS = ("all", "2015+", "2020+")
MIN_TRADES_PER_WINDOW = 30
DRAWDOWN_TOLERANCE = 1.10
REVERT_EXPECTANCY_BPS = 10.0
RECENT_TRADES = 20
METHOD_FIXED_20 = "fixed_20"   # verdict_stats.METHOD_FIXED (the verdict before the e-process judge existed)
BASELINE = "baseline"
RULE = ("Adopt a candidate only if, in every window (all, 2015+, 2020+), it keeps >= 30 trades, net expectancy per "
        "trade is not lower, total net profit is not lower and portfolio max drawdown is <= 1.10x the current "
        "setup's; among eligible candidates take the largest minimum expectancy gain; none eligible = no change. "
        "Short-hold veto: a candidate whose median hold is under 20 sessions is vetoed unless its gross expectancy "
        "per trade over the last two years is at least max(30 bps, 3 x round-trip cost), at 10 bps a round trip "
        "until a measured round-trip cost replaces it.")


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
    # ``gate`` templates turn an entry gate; ``exit`` templates change the
    # exit geometry (stop / time stop) and have their own candidates for the
    # short-term reversion pair, whose tested exits are wider and shorter.
    kind: str = "gate"
    reversion_candidates: Optional[tuple] = None
    # A ``dynamic`` template derives its candidates from the strategy row at
    # run time (``scope``: the latest signal autopsy); ``candidates`` stays
    # empty and the static templates are unchanged.
    dynamic: Optional[Callable[[Any], tuple]] = None

    def candidates_for(self, strategy_type: str, strategy: Any = None) -> tuple:
        from app.services.gate_replay import REVERSION

        if self.dynamic is not None:
            return tuple(self.dynamic(strategy)) if strategy is not None else ()
        if self.reversion_candidates is not None and strategy_type in REVERSION:
            return self.reversion_candidates
        return self.candidates


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


def _stop_label(candidate: dict) -> str:
    return f"stop{candidate['stop_loss_pct'] * 100:g}"


def _stop_describe(candidate: dict) -> str:
    return f"{candidate['stop_loss_pct']:.0%} broker-side stop (target and time stop unchanged)"


def _hold_label(candidate: dict) -> str:
    return f"hold{candidate['max_holding_sessions']}"


def _hold_describe(candidate: dict) -> str:
    return f"time stop after {candidate['max_holding_sessions']} sessions (stop and target unchanged)"


def _scope_label(candidate: dict) -> str:
    from app.services.strategies.scope import scope_label

    return scope_label(candidate.get("scope"))


def _scope_describe(candidate: dict) -> str:
    from app.services.strategies.scope import describe_scope

    return describe_scope(candidate.get("scope"))


def _scope_candidates(strategy: Any) -> tuple:
    from app.services.signal_autopsy import scope_candidates

    return scope_candidates(strategy)


SECTOR_GATE_KEY = "sector_gate_sma"
BREADTH_GATE_KEY = "breadth_gate_min"
ENTRY_CAP_KEY = "max_new_entries_per_session"
ENTRY_CAP_ACTION = "entry_cap_update"


def _sector_label(candidate: dict) -> str:
    return "off" if not candidate[SECTOR_GATE_KEY] else f"sector{candidate[SECTOR_GATE_KEY]}"


def _sector_describe(candidate: dict) -> str:
    n = candidate[SECTOR_GATE_KEY]
    return "no sector gate" if not n else f"no new longs in a symbol while its sector ETF is at or below its {n}-day average"


def _breadth_label(candidate: dict) -> str:
    return "off" if not candidate[BREADTH_GATE_KEY] else f"breadth{float(candidate[BREADTH_GATE_KEY]):g}"


def _breadth_describe(candidate: dict) -> str:
    p = candidate[BREADTH_GATE_KEY]
    return ("no breadth gate" if not p else
            f"no new longs while under {float(p):g}% of S&P 500 members are above their 200-day average")


def _cap_label(candidate: dict) -> str:
    return "off" if not candidate[ENTRY_CAP_KEY] else f"cap{candidate[ENTRY_CAP_KEY]}"


def _cap_describe(candidate: dict) -> str:
    n = candidate[ENTRY_CAP_KEY]
    return "no session entry cap" if not n else f"at most {n} new entries per session across the book"


# A gate the gate-cost audit flags ``costly`` for a strategy (``gate_cost
# .costly_gate_lessons``) is a lesson too: it plans the same template, whose
# first candidate is always the gate turned off, under the same rule.
TEMPLATES = (
    Template("market_gate", frozenset({"missed_market_move", "market_down", "costly_market_gate"}),
             "market_gate_sma", "market_gate_update",
             ({"market_gate_sma": 0}, {"market_gate_sma": 10}, {"market_gate_sma": 20}, {"market_gate_sma": 50}),
             _market_label, _market_describe),
    Template("vix_gate", frozenset({"high_vix_entry", "costly_vix_gate"}), "vix_gate", "vix_gate_update",
             ({"vix_gate": None, "vix_level": None}, {"vix_gate": "below", "vix_level": 20.0},
              {"vix_gate": "below", "vix_level": 25.0}, {"vix_gate": "below", "vix_level": 30.0}),
             _vix_label, _vix_describe),
    Template("earnings_blackout", frozenset({"missed_earnings", "costly_earnings_blackout"}),
             "earnings_blackout_sessions", "earnings_blackout_update",
             ({"earnings_blackout_sessions": 0}, {"earnings_blackout_sessions": 10}, {"earnings_blackout_sessions": 20}),
             _earnings_label, _earnings_describe),
    # Exit geometry: a stop that keeps getting hit before a recovery, or a
    # time stop that keeps closing trades past their best gain, is tested as
    # a stop width / time stop change against the strategy's current exits.
    Template("stop_width", frozenset({"premature_stop"}), "stop_loss_pct", EXIT_POLICY_ACTION,
             ({"stop_loss_pct": 0.02}, {"stop_loss_pct": 0.03}, {"stop_loss_pct": 0.05}),
             _stop_label, _stop_describe, kind="exit",
             reversion_candidates=({"stop_loss_pct": 0.07}, {"stop_loss_pct": 0.10}, {"stop_loss_pct": 0.15})),
    Template("time_stop", frozenset({"held_too_long", "gave_back_gains"}), "max_holding_sessions",
             EXIT_POLICY_ACTION,
             ({"max_holding_sessions": 10}, {"max_holding_sessions": 20}, {"max_holding_sessions": 30}),
             _hold_label, _hold_describe, kind="exit",
             reversion_candidates=({"max_holding_sessions": 5}, {"max_holding_sessions": 10},
                                   {"max_holding_sessions": 15})),
    # Entry scope: ``signal_wrong`` losses are contrasted with the strategy's
    # wins on their decision-bar features (signal_autopsy.py); the features
    # that separate become one-condition scope rules, replayed on the same
    # bars and written to the row as ``parameters["scope"]``.  Candidates are
    # dynamic: the latest autopsy row for the strategy, scope off first.
    Template("scope", frozenset({"signal_wrong"}), "scope", SCOPE_ACTION, (), _scope_label, _scope_describe,
             kind="scope", dynamic=_scope_candidates),
    # The last three diagnosed lessons: the sector fell with the stock
    # (``missed_sector_move``), the entry was taken while most of the market
    # was below its 200-day average (``weak_breadth_entry``), and several
    # stops fired in one session because the entries were one bet
    # (``clustered_stops``).  The first two are strategy gates like the
    # market gate (``market_gate.sector_state`` / ``market_gauges
    # .breadth_state``, replayed by ``gate_replay.sector_gate_on`` /
    # ``breadth_gate_on``); the third is a *book* protection
    # (``entry_protections.entry_cap_block``, risk-rule key
    # ``max_new_entries_per_session``), replayed on the whole book
    # (``book_replay``) and judged by the same ``decide`` rule on book rows.
    Template("sector_gate", frozenset({"missed_sector_move", "costly_sector_gate"}),
             SECTOR_GATE_KEY, "sector_gate_update",
             ({SECTOR_GATE_KEY: 0}, {SECTOR_GATE_KEY: 10}, {SECTOR_GATE_KEY: 20}, {SECTOR_GATE_KEY: 50}),
             _sector_label, _sector_describe),
    Template("breadth_gate", frozenset({"weak_breadth_entry", "costly_breadth_gate"}),
             BREADTH_GATE_KEY, "breadth_gate_update",
             ({BREADTH_GATE_KEY: 0}, {BREADTH_GATE_KEY: 40.0}, {BREADTH_GATE_KEY: 50.0}, {BREADTH_GATE_KEY: 60.0}),
             _breadth_label, _breadth_describe),
    Template("entry_cluster_cap", frozenset({"clustered_stops", "costly_entry_cap"}),
             ENTRY_CAP_KEY, ENTRY_CAP_ACTION,
             ({ENTRY_CAP_KEY: 0}, {ENTRY_CAP_KEY: 2}, {ENTRY_CAP_KEY: 4}),
             _cap_label, _cap_describe, kind="book"),
)
TEMPLATE_BY_NAME = {template.name: template for template in TEMPLATES}
GATE_TEMPLATES = tuple(template for template in TEMPLATES if template.kind == "gate")
EXIT_TEMPLATES = tuple(template for template in TEMPLATES if template.kind == "exit")
# Book templates turn a book-level protection (one risk-rule key for every
# strategy) and are replayed on the whole book; ``BOOK`` is their target in a
# plan where a gate or exit template names a strategy type.
BOOK_TEMPLATES = tuple(template for template in TEMPLATES if template.kind == "book")
BOOK = "book"


def templates_for(triggers: Iterable[str]) -> list[Template]:
    wanted = set(triggers)
    return [template for template in TEMPLATES if template.triggers & wanted]


def current_setup(parameters: Optional[Mapping], strategy_type: Optional[str] = None) -> dict:
    """The strategy's live setup: its gates and, given its type, the exits its policy trades with.

    The earnings blackout falls back to the account-wide setting.  The exit
    keys come from ``exit_policy_for`` (class default with the row's
    overrides), so the baseline replays the exits the strategy really uses;
    without a strategy type they stay ``None`` (the replay's fixed barrier).
    ``scope`` is the row's validated entry scope (``strategies/scope.py``),
    present only when set, so a live scope is the baseline it is judged against.
    """
    from app.services.gate_replay import setup_from_parameters
    from app.services.strategies.scope import SCOPE_KEY, scope_condition
    from app.services.strategy_execution_policy import exit_policy_for

    setup = setup_from_parameters(parameters)
    scope = scope_condition(parameters)
    if scope is not None:
        setup[SCOPE_KEY] = scope
    if (parameters or {}).get("earnings_blackout_sessions") is None:
        setup["earnings_blackout_sessions"] = int(settings.paper_earnings_blackout_sessions or 0)
    if strategy_type:
        policy = exit_policy_for(SimpleNamespace(strategy_type=strategy_type, parameters=dict(parameters or {})))
        if policy is not None:
            setup["stop_loss_pct"] = float(policy.stop_loss_pct) if policy.stop_loss_pct is not None else None
            setup["take_profit_pct"] = float(policy.take_profit_pct) if policy.take_profit_pct is not None else None
            setup["max_holding_sessions"] = int(policy.max_holding_sessions) if policy.max_holding_sessions else None
    return setup


# ------------------------------------------------------------ decision rule

def decide(rows: Mapping[str, Mapping[str, dict]], baseline: str = BASELINE, *,
           min_trades: int = MIN_TRADES_PER_WINDOW, drawdown_tolerance: float = DRAWDOWN_TOLERANCE,
           calibration: float = 1.0, vetoes: Optional[Mapping[str, Mapping]] = None) -> dict:
    """The pre-registered rule: ``rows[label][window]`` -> adopted label (or None) and why.

    ``calibration`` is the engine audit's factor (``engine_audit.py``; 1.0
    unless the engine has been over-optimistic).  It multiplies into each
    candidate's expected gain (candidate minus baseline expectancy), and the
    deflated gain must cover the optimism's share of the revert margin:
    ``factor x gain >= (1 - factor) x REVERT_EXPECTANCY_BPS``.  At 1.0 that
    is exactly ``expectancy not lower``; the rule is otherwise unchanged.

    ``vetoes`` are the yardstick's short-hold vetoes per label
    (``yardstick.short_hold_veto``; the sentence at the end of ``RULE``): a
    candidate that passes every window but is vetoed is not eligible.  Applied
    only while ``yardstick_short_hold_veto`` is on; reported either way.
    """
    calibration = min(1.0, max(0.0, float(calibration if calibration is not None else 1.0)))
    required = (1.0 - calibration) * REVERT_EXPECTANCY_BPS
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
            gain = c["expectancy_bps"] - b["expectancy_bps"]
            if calibration * gain < required:
                reason = f"{window}: expectancy {c['expectancy_bps']:+.1f} < {b['expectancy_bps']:+.1f} bps"
                if calibration < 1.0:
                    reason = (f"{window}: expectancy {c['expectancy_bps']:+.1f} vs {b['expectancy_bps']:+.1f} bps; "
                              f"deflated gain {calibration * gain:+.1f} < {required:.1f} bps required at "
                              f"calibration {calibration:g}")
                break
            if (c.get("total_profit_bps") or 0.0) < (b.get("total_profit_bps") or 0.0):
                reason = f"{window}: total profit {c.get('total_profit_bps', 0):+.0f} < {b.get('total_profit_bps', 0):+.0f} bps"
                break
            if (c.get("portfolio_max_drawdown") or 0.0) > drawdown_tolerance * (b.get("portfolio_max_drawdown") or 0.0):
                reason = (f"{window}: drawdown {c.get('portfolio_max_drawdown', 0):.1%} > "
                          f"{drawdown_tolerance:.2f}x {b.get('portfolio_max_drawdown', 0):.1%}")
                break
            gains.append(calibration * gain)
        if reason is None:
            eligible[label] = round(min(gains), 2)
        else:
            failed[label] = reason
    vetoed = {label: (veto or {}).get("reason") for label, veto in (vetoes or {}).items()
              if label != baseline and (veto or {}).get("vetoed")}
    enforced = bool(settings.yardstick_short_hold_veto)
    if enforced:
        for label in list(eligible):
            if label in vetoed:
                eligible.pop(label)
                failed[label] = f"short-hold veto: {vetoed[label]}"
    adopted = max(eligible, key=eligible.get) if eligible else None
    return {"adopted": adopted, "eligible": eligible, "failed": failed, "rule": RULE,
            "short_hold_veto": {"enforced": enforced, "vetoed": vetoed},
            "calibration": {"factor": calibration, "required_gain_bps": round(required / calibration, 2) if calibration
                            else None, "margin_bps": REVERT_EXPECTANCY_BPS}}


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
        acted, no_candidates = False, None
        triggers = {lesson["cause"], *((lesson.get("contributing") or {}).keys())}
        # A shadow-evidence lesson names the template whose candidate beat
        # the live value; it plans exactly that template with its own trigger.
        named = TEMPLATE_BY_NAME.get(lesson.get("template") or "")
        matched = [named] if named is not None else templates_for(triggers)
        if not matched:
            untemplated.append({"strategy_type": lesson["strategy_type"], "cause": lesson["cause"],
                                "count": lesson["count"], "proposal": lesson.get("proposal"),
                                "reason": "no experiment template for this cause yet"})
            continue
        targets = [lesson["strategy_type"]] if lesson["strategy_type"] != "all" else sorted(strategies)
        for template in matched:
            trigger = lesson["cause"] if (lesson["cause"] in template.triggers or template is named) else next(
                flag for flag in (lesson.get("contributing") or {}) if flag in template.triggers)
            # A book template has one target, the book: whichever strategy
            # the lesson came from, the cap is tested on every strategy's
            # trades competing for the live slots.
            for target in (targets if template.kind != "book" else [BOOK]):
                if template.kind == "book":
                    key = (BOOK, template.name)
                    candidate = {"strategy_type": BOOK, "template": template.name, "trigger": trigger,
                                 "lesson_count": int(lesson["count"]), "lesson_scope": lesson["strategy_type"],
                                 "lesson": lesson.get("lesson")}
                    if key not in wanted or wanted[key]["lesson_count"] < candidate["lesson_count"]:
                        wanted[key] = candidate
                    acted = True
                    continue
                if target not in strategies:
                    skipped.append({"strategy_type": target, "template": template.name, "reason": "not an active strategy"})
                    continue
                if not replayable(target):
                    skipped.append({"strategy_type": target, "template": template.name,
                                    "reason": "strategy cannot be replayed over daily history"})
                    continue
                if template.dynamic is not None and not template.candidates_for(target, strategies[target]):
                    # A dynamic template without candidates (no signal autopsy row, or one with nothing that
                    # separates) cannot plan; the lesson is untemplated unless another template acts on it.
                    no_candidates = {"strategy_type": target, "template": template.name,
                                     "reason": f"no {template.name} candidates in the latest signal autopsy"}
                    skipped.append(no_candidates)
                    continue
                acted = True
                key = (target, template.name)
                candidate = {"strategy_type": target, "template": template.name, "trigger": trigger,
                             "lesson_count": int(lesson["count"]), "lesson_scope": lesson["strategy_type"],
                             "lesson": lesson.get("lesson")}
                if named is not None:
                    candidate["shadow_candidate"] = lesson.get("candidate")
                if key not in wanted or wanted[key]["lesson_count"] < candidate["lesson_count"]:
                    wanted[key] = candidate
        if not acted and no_candidates is not None:
            untemplated.append({"strategy_type": lesson["strategy_type"], "cause": lesson["cause"],
                                "count": lesson["count"], "proposal": lesson.get("proposal"),
                                "template": no_candidates["template"], "reason": no_candidates["reason"]})
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


def yardstick_readings(trades_by_label: Mapping[str, Any], rows: Mapping, frames: Mapping, spy, *,
                       context: Optional[Mapping] = None, calibration: float = 1.0) -> dict:
    """The yardstick per label (``yardstick.score_candidate``) and the rule's pick at each bracket column.

    Windows are the rule's (all, 2015+, 2020+); the short-hold veto's
    out-of-sample window is the last ``yardstick.OOS_YEARS`` years of the
    replayed history (the engine has no pristine holdout: this is the most
    recent evidence, which the rule's windows also contain).
    """
    from app.services import yardstick

    context = dict(context or {})
    symbols = sorted({s for trades in trades_by_label.values() if yardstick._has(trades, "symbol")
                      for s in trades["symbol"].unique()})
    first, last = yardstick.span(frames, symbols or None)
    oos_start = (last - pd.DateOffset(years=yardstick.OOS_YEARS)) if last is not None else None
    windows = {name: (start, None) for name, start in {"all": None, "2015+": "2015-01-01", "2020+": "2020-01-01"}.items()}
    veto_cost = yardstick.veto_cost_bps(context.get("measured"), yardstick.classes_of(symbols) if symbols else None)
    scores = {}
    for label, trades in trades_by_label.items():
        scores[label] = yardstick.score_candidate(
            trades, frames, spy=spy, windows=windows,
            passive_windows={"all": (None, None), "2015+": ("2015-01-01", None)},
            turnover_span=(first, last), oos_start=oos_start, veto_cost=veto_cost, cells=context.get("cells"),
            irx=context.get("irx"), cache=context.get("cache"))
    brackets = {label: score.get("bracket") or {} for label, score in scores.items()}
    decisions = yardstick.bracket_decisions(rows, brackets, lambda priced: decide(priced, calibration=calibration))
    return {"scores": scores, "vetoes": {label: score.get("short_hold_veto") for label, score in scores.items()},
            "bracket_decisions": decisions, "veto_cost": veto_cost,
            "oos_start": str(oos_start.date()) if oos_start is not None else None,
            "summary": {label: yardstick.compact(score, "2015+") for label, score in scores.items()}}


def _yardstick_safe(*args, **kwargs) -> dict:
    try:
        return yardstick_readings(*args, **kwargs)
    except Exception as exc:  # the readings never stop an experiment
        logger.warning("experiment engine: yardstick readings unavailable: %s", exc)
        return {"status": "unavailable", "error": f"{exc.__class__.__name__}: {exc}"[:200], "vetoes": {}}


def run_experiment(strategy: Any, template: Template, *, frames: Mapping, spy, vix, replays, simulate,
                   universe: Optional[Callable] = None, breadth=None, calibration: float = 1.0,
                   prior_trials: int = 0, prior_sharpes: Optional[Iterable[float]] = None,
                   yardstick_context: Optional[Mapping] = None, family_trials: int = 0) -> dict:
    """Replay the strategy under its current setup and each candidate; apply the rule.

    ``universe`` is the optional point-in-time membership mask (``symbol,
    dates -> bool array``) the replay ANDs into its entries; ``breadth`` the
    optional breadth history (``market_gauges.breadth_series``) the breadth
    gate reads.  Either is passed to ``simulate`` only when set.
    ``calibration`` is the engine audit's factor ``decide`` multiplies into
    each candidate's expected gain.  ``prior_trials`` / ``prior_sharpes`` are
    the candidates tried for this strategy in earlier experiments and their
    paired Sharpes, for the multiplicity hurdle (``apply_multiplicity_hurdle``)
    on each candidate's paired backtest difference (``paired_moments``);
    ``family_trials`` are the strategy family's trials made outside the engine
    (discovery library and research, ``yardstick.trial_families``), charged on
    top.  ``yardstick_context`` (calibration cells, measured costs, T-bills)
    feeds the yardstick readings and the short-hold veto.
    """
    from app.services.gate_replay import window_rows

    baseline = current_setup(getattr(strategy, "parameters", None), strategy.strategy_type)
    if template.kind == "scope":
        baseline.setdefault(template.key, None)   # so "scope off" equals a baseline without one
    setups = {BASELINE: baseline}
    candidates = template.candidates_for(strategy.strategy_type, strategy)
    for candidate in candidates:
        setup = {**baseline, **candidate}
        if setup == baseline:
            continue
        setups[template.label(candidate)] = setup
    rows = {}
    extra = {"universe": universe} if universe is not None else {}
    if breadth is not None:
        extra["breadth"] = breadth
    trades_by_label = {}
    for label, setup in setups.items():
        trades = simulate(strategy.strategy_type, frames, spy, setup, replays, vix=vix, **extra)
        rows[label] = window_rows(trades, frames)
        trades_by_label[label] = trades
    readings = _yardstick_safe(trades_by_label, rows, frames, spy, context=yardstick_context, calibration=calibration)
    decision = decide(rows, calibration=calibration, vetoes=readings.get("vetoes"))
    paired = _paired_moments(trades_by_label, spy, template)
    adopted = apply_multiplicity_hurdle(decision, paired, prior_trials=int(prior_trials or 0) + int(family_trials or 0),
                                        prior_sharpes=prior_sharpes, tried_now=len(setups) - 1)
    return {"strategy_type": strategy.strategy_type, "strategy_id": strategy.id, "template": template.name,
            "key": template.key, "baseline_setup": baseline, "setups": setups, "rows": rows, "decision": decision,
            "candidate_keys": sorted(candidates[0]) if candidates else [template.key],
            "calibration": decision["calibration"], "paired_moments": paired,
            "multiplicity_hurdle": decision.get("multiplicity_hurdle"), "yardstick": readings,
            "family_trials": int(family_trials or 0),
            "adopted": adopted, "adopted_setup": setups.get(adopted),
            "adopted_description": template.describe(setups[adopted]) if adopted else None}


def _paired_moments(trades_by_label: Mapping, spy, template: Template) -> dict:
    """Per candidate: the paired backtest difference against the baseline (``verdict_stats.paired_stats``); empty when unreadable."""
    from app.services import verdict_stats as vs

    base = trades_by_label.get(BASELINE)
    out = {}
    for label, trades in trades_by_label.items():
        if label == BASELINE:
            continue
        try:
            frame = vs.paired_backtest(base, trades, spy=spy, adjust=vs.adjusts_for_spy(template.name))
            out[label] = vs.paired_stats(frame)
        except Exception:
            out[label] = {}
    return out


def apply_multiplicity_hurdle(decision: dict, paired: Mapping, *, prior_trials: int = 0,
                              prior_sharpes: Optional[Iterable[float]] = None,
                              tried_now: Optional[int] = None) -> Optional[str]:
    """The adopted label after the multiplicity hurdle (``verdict_stats.multiplicity_hurdle``).

    ``RULE``'s checks are untouched: ``decision["adopted"]`` stays the rule's
    pick, the hurdle's result is added as ``decision["multiplicity_hurdle"]``,
    and only when ``experiment_multiplicity_hurdle`` is on does a candidate
    that fails it go unadopted (``decision["blocked_by_hurdle"]``).  The
    hurdle tests the paired backtest gain (``paired[label]``); trials = every
    candidate tried for the strategy before (``prior_trials``) plus this
    experiment's.  An unavailable hurdle (no paired data) never blocks.
    """
    from app.services.verdict_stats import multiplicity_hurdle

    adopted = decision.get("adopted")
    if not adopted:
        return None
    tried = int(tried_now if tried_now is not None else max(len(paired), 1))
    sharpes = [m.get("sharpe") for m in paired.values() if m and m.get("sharpe") is not None]
    sharpes += [s for s in (prior_sharpes or []) if s is not None]
    try:
        hurdle = multiplicity_hurdle(paired.get(adopted), trials=int(prior_trials or 0) + max(tried, 1),
                                     trial_sharpes=sharpes, label=adopted)
    except Exception as exc:  # an unreadable hurdle never blocks; it is reported as unavailable
        logger.warning("experiment engine: multiplicity hurdle unavailable: %s", exc)
        hurdle = {"label": adopted, "passed": None, "error": f"{exc.__class__.__name__}: {exc}"}
    hurdle["enabled"] = bool(settings.experiment_multiplicity_hurdle)
    decision["multiplicity_hurdle"] = hurdle
    if hurdle["enabled"] and hurdle.get("passed") is False:
        decision["blocked_by_hurdle"] = adopted
        return None
    return adopted


def book_baseline(rules: Optional[Mapping]) -> dict:
    """The book-level setup in force: the keys of every book template, from the active risk rule."""
    from app.services.entry_protections import protection_config

    config = protection_config(dict(rules or {}))
    return {ENTRY_CAP_KEY: int(config.max_new_entries_per_session)}


def run_book_experiment(strategies: Mapping[str, Any], template: Template, *, frames: Mapping, spy, vix, replays,
                        simulate, rules: Optional[Mapping] = None, universe: Optional[Callable] = None,
                        breadth=None, book_candidates: Optional[Callable] = None, calibration: float = 1.0,
                        prior_trials: int = 0, yardstick_context: Optional[Mapping] = None) -> dict:
    """Replay the whole book under the current book policy and each candidate; apply the same rule.

    Every active replayable strategy's trades (each under its own live setup)
    compete for the live slots (``book_replay.allocate``); a candidate changes
    only the template's key of the book policy.  The rows are book metrics
    (per-trade expectancy of the accepted trades, total profit, the book's
    drawdown with each position at 1 / total cap of equity), judged by
    ``decide`` exactly as a strategy experiment is.
    """
    from app.services import book_replay

    baseline = book_baseline(rules)
    setups = {BASELINE: baseline}
    for candidate in template.candidates:
        setup = {**baseline, **candidate}
        if setup == baseline:
            continue
        setups[template.label(candidate)] = setup
    live_setups = {name: current_setup(getattr(row, "parameters", None), name) for name, row in strategies.items()
                   if book_replay.gate_replay.replayable(name)}
    extra: dict = {"vix": vix}
    if universe is not None:
        extra["universe"] = universe
    if breadth is not None:
        extra["breadth"] = breadth
    rows_in = (book_candidates or book_replay.candidates)(frames, spy, replays, live_setups, simulate=simulate, **extra)
    rows = {}
    accepted_by_label = {}
    for label, setup in setups.items():
        policy = book_replay.book_policy(rules, max_new_per_day=int(setup[ENTRY_CAP_KEY] or 0))
        accepted = book_replay.allocate(rows_in, frames, policy)
        rows[label] = book_replay.book_rows(accepted, frames, weight=1.0 / policy["caps"]["total"])
        accepted_by_label[label] = accepted
    readings = _yardstick_safe(accepted_by_label, rows, frames, spy, context=yardstick_context,
                               calibration=calibration)
    decision = decide(rows, calibration=calibration, vetoes=readings.get("vetoes"))
    adopted = apply_multiplicity_hurdle(decision, {}, prior_trials=prior_trials, tried_now=len(setups) - 1)
    return {"strategy_type": BOOK, "strategy_id": None, "template": template.name, "key": template.key,
            "baseline_setup": baseline, "setups": setups, "rows": rows, "decision": decision,
            "calibration": decision["calibration"], "multiplicity_hurdle": decision.get("multiplicity_hurdle"),
            "yardstick": readings,
            "adopted": adopted, "adopted_setup": setups.get(adopted),
            "adopted_description": template.describe(setups[adopted]) if adopted else None,
            "book": {"strategies": sorted(live_setups), "candidate_trades": int(len(rows_in)),
                     "policy": book_replay.book_policy(rules)}}


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


def _apply_book_setting(db: Session, changes: Mapping[str, Any], *, reason: str) -> dict:
    """Write book-level keys to the active risk rule; returns the previous values."""
    from app.services.risk_settings import get_active_risk_rule, update_risk_settings

    rule = get_active_risk_rule(db)
    before = {key: (rule.value or {}).get(key) for key in changes}
    update_risk_settings(db, dict(changes), reason=reason, actor="experiment_engine",
                         authorization={"source": "experiment_engine", "paper_only": True})
    return before


def adopt_change(db: Session, strategy: Optional[Strategy], experiment: dict, *, now: datetime,
                 recent_rows: list) -> dict:
    """Apply the adopted candidate: to the strategy row (gate / exit templates) or the risk rule (book templates)."""
    template = TEMPLATE_BY_NAME[experiment["template"]]
    book = template.kind == "book"
    strategy_type = BOOK if book else strategy.strategy_type
    strategy_id = None if book else strategy.id
    first = template.candidates[0] if book else template.candidates_for(strategy.strategy_type, strategy)[0]
    keys = experiment.get("candidate_keys") or list(first)
    candidate = {key: experiment["adopted_setup"][key] for key in keys}
    change_id = hashlib.sha256(f"{strategy_id}:{template.name}:{now.isoformat()}".encode()).hexdigest()[:12]
    untouched = ("Entry gates are unchanged; lots already open keep the legs they were opened with."
                 if template.kind == "exit" else "Sells and exits are unchanged.")
    message = (f"experiment {change_id}: {experiment['adopted_description']}; adopted by the experiment engine "
               f"after the {experiment['trigger']} lesson repeated and the pre-registered backtest rule passed. "
               + untouched)
    if book:
        before = _apply_book_setting(db, candidate, reason=message)
        rule_value = _risk_rule_value(db)
        after = {key: rule_value.get(key) for key in candidate}
    else:
        before = _apply_parameters(strategy, candidate)
        after = {key: (strategy.parameters or {}).get(key) for key in candidate}
    write_audit_log(
        db, event_type=GOVERNANCE_EVENT, entity_type="risk_rule" if book else "strategy", entity_id=strategy_id,
        action=template.governance_action, status="complete", message=message,
        payload=_clean({**candidate, "source": "experiment_engine", "change_id": change_id, "paper_only": True}),
    )
    recent = _recent_performance(recent_rows, experiment["trigger"], limit=RECENT_TRADES)
    change = {
        "change_id": change_id, "strategy_id": strategy_id, "strategy_type": strategy_type,
        "template": template.name, "key": template.key, "trigger": experiment["trigger"],
        "description": experiment["adopted_description"], "before": before, "after": after,
        "adopted_at": _iso(now), "state": "watching",
        "expected": {"baseline": experiment["rows"][BASELINE], "adopted": experiment["rows"][experiment["adopted"]],
                     "label": experiment["adopted"],
                     "calibration_factor": (experiment.get("calibration") or {}).get("factor", 1.0),
                     "required_gain_bps": (experiment.get("calibration") or {}).get("required_gain_bps", 0.0)},
        "recent_before": recent,
        "watch": {"min_trades": settings.experiment_verdict_min_trades,
                  "max_trades": settings.experiment_verdict_max_trades, "cause": experiment["trigger"],
                  "revert_if_expectancy_worse_by_bps": REVERT_EXPECTANCY_BPS},
        "paper_only": True,
    }
    write_audit_log(
        db, event_type=EVENT, entity_type="risk_rule" if book else "strategy", entity_id=strategy_id,
        action=ACTION_ADOPTED, status="watching",
        message=f"{strategy_type}: {experiment['adopted_description']} (change {change_id})",
        payload=_clean(change),
    )
    return change


def _risk_rule_value(db: Session) -> dict:
    from app.services.risk_settings import get_active_risk_rule

    return dict(get_active_risk_rule(db).value or {})


def experiment_history(db: Session, *, limit: Optional[int] = None) -> list[dict]:
    return [row.payload or {} for row in _history_rows(db, ACTION_EXPERIMENT, limit)]


def prior_trials_by_strategy(history: Iterable[Mapping]) -> dict:
    """Per strategy: candidate setups tried in earlier experiments (baseline excluded) and their per-trade Sharpes."""
    out: dict[str, dict] = {}
    for item in history:
        key = item.get("strategy_type")
        if not key:
            continue
        entry = out.setdefault(key, {"trials": 0, "sharpes": []})
        setups = item.get("setups") or item.get("rows") or {}
        entry["trials"] += max(len(setups) - 1, 0)
        for label, moment in (item.get("paired_moments") or {}).items():
            if label != BASELINE and (moment or {}).get("sharpe") is not None:
                entry["sharpes"].append(float(moment["sharpe"]))
    return out


def _costly_gate_lessons(db: Session) -> list[dict]:
    """Gates the gate-cost audit flags costly, as lessons (an unreadable ledger is no lesson)."""
    from app.services.gate_cost import costly_gate_lessons

    try:
        return costly_gate_lessons(db)
    except Exception as exc:
        db.rollback()
        logger.warning("experiment engine: gate-cost lessons unavailable: %s", exc)
        return []


def _shadow_lessons(db: Session) -> list[dict]:
    """Candidates whose shadow record beats the live setup, as lessons (an unreadable ledger is no lesson)."""
    from app.services.setup_shadow import shadow_lessons

    try:
        return shadow_lessons(db, min_trades=settings.experiment_verdict_min_trades)
    except Exception as exc:
        db.rollback()
        logger.warning("experiment engine: setup-shadow lessons unavailable: %s", exc)
        return []


def _yardstick_context(db: Session, *, download: bool = True) -> dict:
    """Calibrated cells, measured round trips and T-bills for the yardstick readings; empty parts when unreadable."""
    from app.services import yardstick

    return {"cells": yardstick.current_cells(db), "measured": yardstick.measured_costs(db),
            "irx": yardstick.load_irx(download=download), "cache": {}}


def _family_extra(db: Session) -> dict:
    """Per family, trials made outside the engine (discovery library and research); empty when unreadable."""
    from app.services import yardstick

    return yardstick.external_family_trials(db, exclude="engine")


def _strategy_family(strategy: Any) -> str:
    from app.services.yardstick import family_of_strategy

    return family_of_strategy(getattr(strategy, "strategy_type", None), getattr(strategy, "parameters", None))


def _calibration_factor(db: Session) -> float:
    """The engine audit's calibration factor (``engine_audit.py``); 1.0 without an audit or when unreadable."""
    from app.services.engine_audit import current_calibration_factor

    return current_calibration_factor(db)


def _load_vix(start: str):
    from app.services.strategies.vix_gate import download_vix_history

    return download_vix_history(start)


def _load_breadth(now: datetime):
    """The point-in-time breadth history as a series (``market_gauges``); the cache is rebuilt weekly."""
    from app.services.market_gauges import breadth_series, load_breadth_history

    return breadth_series(load_breadth_history(now=now))


def _needs_breadth(planned: Iterable[Mapping], strategies: Mapping[str, Any]) -> bool:
    """Whether any planned experiment turns, or any planned strategy already trades with, the breadth gate."""
    for item in planned:
        template = TEMPLATE_BY_NAME[item["template"]]
        if template.key == BREADTH_GATE_KEY:
            return True
        row = strategies.get(item["strategy_type"])
        if row is not None and current_setup(getattr(row, "parameters", None))[BREADTH_GATE_KEY]:
            return True
        if template.kind == "book" and any(current_setup(getattr(r, "parameters", None))[BREADTH_GATE_KEY]
                                           for r in strategies.values()):
            return True
    return False


def release_read_transaction(db: Session, *, strict: bool = False) -> None:
    """End the session's open transaction before minutes of computation.

    A session that only read stays "idle in transaction" while the replays run
    and the server terminates it; committing (not rolling back) keeps any
    writes made so far. Loaded rows are not expired, so reading a strategy's
    parameters during a replay does not open a new transaction. ``strict``
    re-raises a failed commit (the per-experiment ledger write).
    """
    previous = db.expire_on_commit
    db.expire_on_commit = False
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        if strict:
            raise
        logger.warning("experiment engine: could not release the read transaction: %s", exc)
    finally:
        db.expire_on_commit = previous


def _active_rules(db: Session) -> dict:
    """The active risk rule's values (book-level protections read them); empty when unreadable."""
    try:
        return _risk_rule_value(db)
    except Exception as exc:
        db.rollback()
        logger.warning("experiment engine: risk rule unavailable: %s", exc)
        return {}


def run_experiment_engine(db: Session, *, now: Optional[datetime] = None, loader=None, earnings_loader=None,
                          vix_loader=None, simulate=None, breadth_loader=None, book_candidates=None) -> dict:
    """Weekly pass: plan from the repeating failures, replay, decide, adopt (paper only)."""
    from app.services.loss_diagnosis import repeating_failures
    from app.services.trade_attribution import current_attributions

    now = now or datetime.now(UTC)
    base = {"paper_only": True, "live_authorized": False, "rule": RULE}
    if not settings.experiment_engine_enabled:
        return base | {"status": "disabled"}
    attributions = current_attributions(db, sources=SOURCES)
    lessons = repeating_failures(attributions)
    gate_lessons = _costly_gate_lessons(db)
    shadow = _shadow_lessons(db)
    lessons = [*lessons, *gate_lessons, *shadow]
    strategies = {row.strategy_type: row for row in db.query(Strategy).filter(
        Strategy.current_status.in_(ACTIVE_STATUSES)).order_by(Strategy.id).all()}
    plan = plan_experiments(lessons, strategies, now=now, history=experiment_history(db),
                            cooldown_days=settings.experiment_engine_cooldown_days,
                            max_experiments=settings.experiment_engine_max_experiments)
    experiments, adopted, errors = [], [], []
    calibration = _calibration_factor(db)
    prior = prior_trials_by_strategy(experiment_history(db))
    injected = loader is not None   # a caller-supplied loader means no network for the T-bill history either
    if plan["planned"]:
        from app.services import gate_replay
        from app.services import meta_label_primary as primary
        from app.services.strategy_discovery import load_frames

        simulate = simulate or gate_replay.simulate
        # Read what the replays need from the database, then end the read
        # transaction before the long history load and replays. On 2026-11-10
        # the session stayed "idle in transaction" for 15 minutes, Postgres
        # terminated it, and the risk-rule read after the replays failed, so
        # the book experiments ran with no risk rules at all.
        rules = _active_rules(db) if any(TEMPLATE_BY_NAME[i["template"]].kind == "book" for i in plan["planned"]) else {}
        release_read_transaction(db)
        symbols = sorted({s.upper() for s in settings.paper_execution_symbols})
        universe, universe_summary = None, {"mode": "today", "symbols": len(symbols)}
        if settings.replay_point_in_time_universe:
            from app.services import point_in_time_universe

            try:  # a missing membership file falls back to today's list, never stops the run
                context = point_in_time_universe.replay_universe(symbols, loader=loader, earnings_loader=earnings_loader,
                                                                 now=now)
                symbols, universe, loader, earnings_loader = (context.symbols, context.mask, context.loader,
                                                              context.earnings_loader)
                universe_summary = context.summary()
            except Exception as exc:
                logger.warning("experiment engine: point-in-time universe unavailable, using today's list: %s", exc)
                universe_summary["fallback"] = f"{exc.__class__.__name__}: {exc}"
        frames, missing = load_frames(symbols, loader=loader, start=settings.experiment_engine_history_start,
                                      earnings_loader=earnings_loader)
        spy = frames.get("SPY")
        try:
            vix = (vix_loader or _load_vix)(settings.experiment_engine_history_start)
        except Exception as exc:  # the VIX gate fails open live; replay the same way
            logger.warning("experiment engine: VIX history unavailable: %s", exc)
            vix = None
        replays = {symbol: primary.replay_signals(frame) for symbol, frame in frames.items()}
        breadth = None
        if _needs_breadth(plan["planned"], strategies):
            try:
                breadth = (breadth_loader or _load_breadth)(now)
            except Exception as exc:  # the breadth gate fails open live; replay the same way
                logger.warning("experiment engine: breadth history unavailable: %s", exc)
                breadth = None
        yardstick_context = _yardstick_context(db, download=not injected)
        family_extra = _family_extra(db)
        release_read_transaction(db)
        for item in plan["planned"]:
            template = TEMPLATE_BY_NAME[item["template"]]
            book = template.kind == "book"
            strategy = None if book else strategies[item["strategy_type"]]
            try:
                if not frames or spy is None:
                    raise RuntimeError("no price history to replay")
                if book:
                    experiment = run_book_experiment(strategies, template, frames=frames, spy=spy, vix=vix,
                                                     replays=replays, simulate=simulate, rules=rules,
                                                     universe=universe, breadth=breadth,
                                                     book_candidates=book_candidates, calibration=calibration,
                                                     prior_trials=prior.get(BOOK, {}).get("trials", 0),
                                                     yardstick_context=yardstick_context)
                else:
                    tried = prior.get(item["strategy_type"], {})
                    family = _strategy_family(strategy)
                    experiment = run_experiment(strategy, template, frames=frames, spy=spy, vix=vix, replays=replays,
                                                simulate=simulate, universe=universe, breadth=breadth,
                                                calibration=calibration, prior_trials=tried.get("trials", 0),
                                                prior_sharpes=tried.get("sharpes", []),
                                                yardstick_context=yardstick_context,
                                                family_trials=int(family_extra.get(family, 0)))
                    experiment["family"] = family
            except Exception as exc:  # one experiment must not stop the run
                db.rollback()
                errors.append({**item, "error": f"{exc.__class__.__name__}: {exc}"})
                continue
            experiment.update({"trigger": item["trigger"], "lesson_count": item["lesson_count"],
                               "lesson_scope": item["lesson_scope"], "ran_at": _iso(now),
                               "symbols": len(frames), "missing_symbols": missing, "universe": universe_summary,
                               "breadth_history": breadth is not None,
                               "applied": bool(experiment["adopted"]) and settings.experiment_engine_adoption_enabled})
            change = None
            if experiment["adopted"] and settings.experiment_engine_adoption_enabled:
                recent = [row for row in attributions if book or row.strategy_type == strategy.strategy_type]
                change = adopt_change(db, strategy, experiment, now=now, recent_rows=recent)
                experiment["change_id"] = change["change_id"]
                adopted.append(change)
            write_audit_log(
                db, event_type=EVENT, entity_type="risk_rule" if book else "strategy",
                entity_id=None if book else strategy.id, action=ACTION_EXPERIMENT,
                status="adopted" if change else ("eligible" if experiment["adopted"] else "rejected"),
                message=(f"{item['strategy_type']} / {template.name} ({item['trigger']} x{item['lesson_count']}): "
                         + (f"adopt {experiment['adopted']}: {experiment['adopted_description']}" if experiment["adopted"]
                            else (f"{experiment['decision']['blocked_by_hurdle']} passed the rule but not the "
                                  "multiplicity hurdle" if experiment["decision"].get("blocked_by_hurdle")
                                  else ("vetoed by the short-hold veto: "
                                        + ", ".join(sorted(experiment["decision"]["short_hold_veto"]["vetoed"]))
                                        if experiment["decision"].get("short_hold_veto", {}).get("enforced")
                                        and any(str(r).startswith("short-hold veto")
                                                for r in experiment["decision"].get("failed", {}).values())
                                        else "no candidate beat the current setup")))),
                payload=_clean(experiment),
            )
            experiments.append(experiment)
            release_read_transaction(db, strict=True)
    summary = {"status": "complete", "ran_at": _iso(now), "lessons": len(lessons), "planned": len(plan["planned"]),
               "gate_cost_lessons": [{k: lesson.get(k) for k in ("strategy_type", "cause", "gate", "count",
                                                                   "mean_net_bps")} for lesson in gate_lessons],
               "shadow_lessons": [{k: lesson.get(k) for k in ("strategy_type", "template", "candidate", "live", "count",
                                                                "trades", "delta_expectancy_bps")} for lesson in shadow],
               "experiments": [{k: e.get(k) for k in ("strategy_type", "template", "trigger", "adopted",
                                                       "adopted_description", "change_id", "applied")}
                               for e in experiments],
               "adopted": [c["change_id"] for c in adopted], "errors": errors, "deferred": plan["deferred"],
               "skipped": plan["skipped"], "untemplated": plan["untemplated"],
               "adoption_enabled": settings.experiment_engine_adoption_enabled,
               "calibration_factor": calibration}
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
            # Both judges side by side (verdict_stats.py): which one acted, and what each said.
            change["verdict_method"] = history[-1].get("verdict_method", METHOD_FIXED_20)
            change["verdict_fixed_20"] = history[-1].get("verdict_fixed_20", history[-1]["verdict"]
                                                         if change["verdict_method"] == METHOD_FIXED_20 else None)
            change["verdict_e_process"] = history[-1].get("verdict_e_process")
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
    template = TEMPLATE_BY_NAME[change["template"]]
    message = f"experiment {change['change_id']} reverted: {reason}. Parameters restored to their values before the change."
    if template.kind == "book":
        # A key that was absent before the change goes back to off (0): the
        # risk rule never drops a key, and 0 is the protection's "off".
        before = {key: (value if value is not None else 0) for key, value in change["before"].items()}
        _apply_book_setting(db, before, reason=message)
        entity_type, entity_id = "risk_rule", None
    else:
        strategy = db.get(Strategy, change["strategy_id"])
        if strategy is None:
            return False
        _apply_parameters(strategy, change["before"])
        entity_type, entity_id = "strategy", strategy.id
    write_audit_log(
        db, event_type=GOVERNANCE_EVENT, entity_type=entity_type, entity_id=entity_id,
        action=template.governance_action, status="complete", message=message,
        payload=_clean({**change["before"], "source": "experiment_engine", "reverted_change": change["change_id"],
                        "paper_only": True}),
    )
    return True


def _shadow_record(db: Session, change: Mapping, *, since: datetime, min_trades: int) -> Optional[dict]:
    """The alternatives' forward record since the change (``setup_shadow``); None when unreadable."""
    from app.services.setup_shadow import shadow_track_record

    if TEMPLATE_BY_NAME[change["template"]].kind == "book":
        return None  # book protections are not shadowed per BUY decision
    try:
        return shadow_track_record(db, change["strategy_type"], change["template"], since=since,
                                   min_trades=min_trades, margin_bps=REVERT_EXPECTANCY_BPS)
    except Exception as exc:
        db.rollback()
        logger.warning("experiment engine: setup-shadow record unavailable for %s: %s", change.get("change_id"), exc)
        return None


def shadow_beats_adopted(shadow: Optional[Mapping]) -> Optional[dict]:
    """The alternative that beat the adopted value by the revert margin over ``min_trades`` overlapping decisions."""
    if not shadow:
        return None
    better = [c for c in shadow.get("candidates") or [] if c.get("beats_live") and not c.get("is_live")]
    if not better:
        return None
    return max(better, key=lambda c: (c.get("delta_expectancy_bps") or 0.0))


def _verdict_audit_fields(change: Mapping, after: Mapping, shadow: Optional[Mapping]) -> dict:
    """Expected vs realised gain for the verdict payload (``engine_audit.verdict_fields``); empty when unavailable."""
    from app.services.engine_audit import verdict_fields

    try:
        return verdict_fields(change, after, shadow)
    except Exception as exc:
        logger.warning("experiment engine: expected-vs-realised fields unavailable for %s: %s",
                       change.get("change_id"), exc)
        return {}


def _fmt_bps(value: Any) -> str:
    return f"{float(value):+.1f}" if isinstance(value, (int, float)) else "n/a"


def _spy_frame(db: Session):
    """Trusted SPY daily bars for the residual (SPY-adjusted) returns; None when unavailable."""
    from app.services.gate_cost import daily_frame

    try:
        return daily_frame(db, "SPY")
    except Exception as exc:
        db.rollback()
        logger.warning("experiment engine: SPY bars unavailable for the e-process verdict: %s", exc)
        return None


def _shadow_decisions(db: Session, change: Mapping, since: datetime) -> list[dict]:
    """Scored setup-shadow decisions of the change's strategy from the session after the change."""
    from app.services import gate_cost
    from app.services.setup_shadow import latest_scores

    latest = latest_scores(db) or {}
    first = gate_cost.last_completed_session(since).isoformat()
    return [d for d in latest.get("decisions") or [] if d.get("strategy") == change["strategy_type"]
            and str(d.get("session")) > first]


def e_process_judgement(db: Session, change: Mapping, *, after_rows: list, now: datetime, state: dict,
                        inputs: Mapping, spy=None) -> dict:
    """The always-valid judge's verdict for one change (``verdict_stats.e_process_verdict``); updates ``state``.

    Gate templates are judged on paired differences from the setup shadow
    (the adopted value minus the value it replaced, on the same BUY
    decisions); exit, scope and book templates, or a gate without a shadow
    record, on the live trades after the change minus the mean of the 20
    before.  SPY-adjusted except for market-timing gates.  The first decision
    is frozen in the e-LOND state so later data cannot undo a stopped test.
    """
    from app.services import verdict_stats as vs
    from app.services.engine_audit import _before_label
    from app.services.trade_attribution import current_attributions

    cid = change["change_id"]
    entry = state["changes"].setdefault(cid, {"index": len(state["changes"]) + 1, "levels": [], "discovered": False})
    if entry.get("decision"):
        return {**entry["decision"], "frozen": True}
    template = TEMPLATE_BY_NAME[change["template"]]
    adjust = vs.adjusts_for_spy(template.name)
    cls = vs.strategy_class(change["strategy_type"])
    bound = float(inputs["loss_bound_bps"][cls])
    sigma = float(inputs["sigma_bps"][cls])
    rho = float(inputs.get("rho") or 0.0)
    adopted_at = _parse(change["adopted_at"])
    observations, source, baseline = [], None, None
    if template.kind == "gate":
        before_label = _before_label(change)
        if before_label:
            observations = vs.paired_observations(_shadow_decisions(db, change, adopted_at), template.name,
                                                  before_label, spy=spy, adjust=adjust)
            source = "shadow_paired" if observations else None
    if not observations:
        scope = None if change["strategy_type"] == BOOK else [change["strategy_type"]]
        before_rows = [row for row in current_attributions(db, sources=SOURCES, strategy_types=scope)
                       if _parse(_iso(row.closed_at)) < adopted_at]
        observations, baseline = vs.unpaired_observations(after_rows, before_rows, spy=spy, adjust=adjust,
                                                          baseline_trades=RECENT_TRADES)
        source = "live_unpaired" if baseline is not None else None
        if baseline is not None:
            bound += abs(baseline)
    if source is None:
        return {"method": vs.METHOD_E_PROCESS, "verdict": "watching", "status": "no_observations", "trades": 0,
                "reason": "no paired shadow decisions and no live baseline yet"}
    clusters = vs.cluster_by_entry_date(observations)
    j, discoveries, level = vs.level_for(state, cid)
    levels = vs.levels_by_observation(entry, clusters, level)
    result = vs.e_process_verdict(clusters, bound_bps=bound, sigma_bps=sigma, improvement_level=level,
                                  levels=levels, rho=rho)
    seen = [int(t) for t, _ in entry.get("levels") or []]
    if result["trades"] and (not seen or result["trades"] > max(seen)):
        entry.setdefault("levels", []).append([int(result["trades"]), level])
    result.update({"source": source, "adjusted_for_spy": adjust, "baseline_bps": baseline,
                   "elond_index": j, "elond_discoveries_before": discoveries, "inputs_as_of": inputs.get("as_of"),
                   "inputs_recorded": bool(inputs.get("recorded"))})
    if result["verdict"] != "watching":
        if result["verdict"] == "working":
            entry["discovered"] = True
        entry["decision"] = {**result, "judged_at": _iso(now)}
    return result


def _e_process_safe(db: Session, change: Mapping, **kwargs) -> dict:
    try:
        return e_process_judgement(db, change, **kwargs)
    except Exception as exc:  # the shadow judge must never stop the acting one
        db.rollback()
        logger.warning("experiment engine: e-process verdict unavailable for %s: %s", change.get("change_id"), exc)
        return {"method": "e_process", "verdict": None, "status": "unavailable",
                "error": f"{exc.__class__.__name__}: {exc}"}


def _fixed_verdict(change: Mapping, after: Mapping, beaten_by: Optional[Mapping], *, min_trades: int,
                   max_trades: int) -> dict:
    """The fixed 20-trade rule (with the shadow early revert): verdict and reason, or ``watching`` with what it needs."""
    if beaten_by is not None:
        return {"verdict": "not_helping", "shadow": True,
                "reason": (f"in shadow, {change['template']} = {beaten_by['candidate']} beat the adopted "
                           f"{beaten_by['live']} by {_fmt_bps(beaten_by['delta_expectancy_bps'])} bps per trade over "
                           f"{beaten_by['overlap']} of the same BUY decisions ({beaten_by['trades']} forward trades)")}
    if after["trades"] < min_trades:
        return {"verdict": "watching", "needed": min_trades - after["trades"], "shadow": False}
    verdict = judge(change.get("recent_before") or {}, after, max_trades=max_trades)
    if verdict == "watching":
        return {"verdict": "watching", "needed": max_trades - after["trades"], "so_far": "mixed", "shadow": False}
    return {"verdict": verdict, "shadow": False,
            "reason": (f"after {after['trades']} trades expectancy {_fmt_bps(after['expectancy_bps'])} bps vs "
                       f"{_fmt_bps((change.get('recent_before') or {}).get('expectancy_bps'))} before, and "
                       f"{change['trigger']} losses no rarer")}


SLEEVE_GUARDRAIL = "sleeve_guardrail"


def _sleeve_row(db: Session, change: Mapping) -> Optional[Strategy]:
    """The change's strategy row when it is a monthly sleeve (``sleeve_guardrail.is_monthly_sleeve``), else None."""
    from app.services.sleeve_guardrail import is_monthly_sleeve

    if not change.get("strategy_id") or change.get("strategy_type") == BOOK:
        return None
    try:
        row = db.get(Strategy, change["strategy_id"])
    except Exception:
        db.rollback()
        return None
    return row if is_monthly_sleeve(row) else None


def _sleeve_verdict(db: Session, row: Strategy) -> dict:
    """A monthly sleeve skips fixed_20; its harm guardrail on daily active P&L acts instead (no data = watching)."""
    from app.services import sleeve_guardrail

    guard = sleeve_guardrail.harm_guardrail(sleeve_guardrail.sleeve_daily_returns(db, row))
    if guard["harm"]:
        return {"verdict": "not_helping", "shadow": False, "guardrail": guard,
                "reason": f"monthly-sleeve harm guardrail: {guard['reason']}"}
    return {"verdict": "watching", "shadow": False, "needed": None, "guardrail": guard,
            "reason": f"monthly-sleeve harm guardrail: {guard['status']} ({guard['reason']})"}


def _model_round_trip_bps(db: Session, row: Any, frames: dict) -> Optional[float]:
    """The published model's round trip for one live trade (entry and exit sessions, bars before each)."""
    from app.services.gate_cost import daily_frame
    from app.services.paper_cost_calibration import model_cost, session_date

    symbol = str(getattr(row, "symbol", "") or "").upper()
    if not symbol or row.opened_at is None or row.closed_at is None:
        return None
    if symbol not in frames:
        frames[symbol] = daily_frame(db, symbol)
    frame = frames[symbol]
    if frame is None:
        return None
    entry = model_cost(frame, session_date(row.opened_at), 0.0)["total_bps"]
    exit_ = model_cost(frame, session_date(row.closed_at), 0.0)["total_bps"]
    return float(entry) + float(exit_)


def forward_bracket(db: Session, change: Mapping, after_rows: list, *, max_trades: int) -> dict:
    """The forward verdict's cost bracket: before / after expectancy and ``judge`` at 5, 10, 25 bps and the model.

    Each trade's gross return minus the column's round trip; ``realized`` is
    the paper net as measured.  The acting verdict is unchanged.
    """
    from app.services import yardstick
    from app.services.trade_attribution import current_attributions

    adopted_at = _parse(change["adopted_at"])
    scope = None if change["strategy_type"] == BOOK else [change["strategy_type"]]
    before_rows = sorted((row for row in current_attributions(db, sources=SOURCES, strategy_types=scope)
                          if _parse(_iso(row.closed_at)) < adopted_at),
                         key=lambda row: _parse(_iso(row.closed_at)))[-RECENT_TRADES:]
    frames: dict = {}
    trigger = change["trigger"]

    def gross(row) -> Optional[float]:
        value = row.gross_return if row.gross_return is not None else row.net_return
        return float(value) if value is not None else None

    def priced(rows: list, cost) -> dict:
        perf = _recent_performance(rows, trigger)
        values = []
        for row in rows:
            g, c = gross(row), cost(row)
            if g is not None and c is not None:
                values.append(g - c / 10_000)
        perf["expectancy_bps"] = round(sum(values) / len(values) * 10_000, 1) if values else None
        perf["trades"] = len(values)
        return perf

    columns = {name: (lambda row, c=c: c) for name, c in zip(yardstick.FLAT_COLUMNS, yardstick.BRACKET_BPS)}
    columns[yardstick.MODEL] = lambda row: _model_round_trip_bps(db, row, frames)
    out = {"columns": {}, "realized": {"before": _recent_performance(before_rows, trigger).get("expectancy_bps"),
                                       "after": _recent_performance(after_rows, trigger).get("expectancy_bps")}}
    for name, cost in columns.items():
        before, after = priced(before_rows, cost), priced(after_rows, cost)
        verdict = judge(before, after, max_trades=max_trades) if after["trades"] else "watching"
        out["columns"][name] = {"before_bps": before["expectancy_bps"], "after_bps": after["expectancy_bps"],
                                "after_trades": after["trades"], "judge": verdict}
    return out


def _forward_bracket_safe(db: Session, change: Mapping, after_rows: list, *, max_trades: int) -> dict:
    try:
        return forward_bracket(db, change, after_rows, max_trades=max_trades)
    except Exception as exc:  # a reading only; the acting verdict never waits for it
        db.rollback()
        logger.warning("experiment engine: forward cost bracket unavailable for %s: %s", change.get("change_id"), exc)
        return {"status": "unavailable", "error": f"{exc.__class__.__name__}: {exc}"[:200]}


def _compact_e(e_verdict: Mapping) -> dict:
    keys = ("method", "verdict", "status", "reason", "trades", "observations", "effective_trades", "source",
            "adjusted_for_spy", "mean_difference_bps", "e_harm", "e_improvement", "harm_threshold",
            "improvement_level", "improvement_threshold", "elond_index", "elond_discoveries_before",
            "decided_at_trades", "bound_bps", "sigma_bps", "bound_breach", "breach_at_observation", "process",
            "lcb_bps", "kept_at_cap", "cap_trades", "rho", "baseline_bps", "inputs_as_of", "inputs_recorded",
            "frozen", "judged_at", "error")
    return {k: e_verdict.get(k) for k in keys if k in e_verdict}


def review_changes(db: Session, *, now: Optional[datetime] = None) -> dict:
    """Daily: give each watched change a verdict once enough paper trades closed after it.

    Two judges run on every watched change.  The fixed 20-trade rule (with
    the shadow early revert: an alternative that beats the adopted value by
    ``REVERT_EXPECTANCY_BPS`` over ``min_trades`` overlapping decisions) and
    the always-valid e-process judge (``verdict_stats``).  Only
    ``experiment_verdict_method`` acts (``fixed_20`` by default); the other is
    recorded beside it in every ``change_verdict`` payload
    (``verdict_fixed_20`` / ``verdict_e_process``) and in each pending entry.
    When the e-process judge is the acting one but cannot read its data, the
    fixed rule acts and the payload says so.
    """
    from app.services import verdict_stats as vs
    from app.services.trade_attribution import current_attributions

    now = now or datetime.now(UTC)
    method = settings.experiment_verdict_method
    reviewed, pending = [], []
    changes = adopted_changes(db)
    state = vs.assign_indices(vs.elond_state(db), changes)
    before_state = json.dumps(state, sort_keys=True, default=str)
    inputs = vs.current_inputs(db)
    spy = None
    spy_loaded = False
    for change in changes:
        if change.get("state") != "watching":
            continue
        if not spy_loaded:
            spy, spy_loaded = _spy_frame(db), True
        adopted_at = _parse(change["adopted_at"])
        # A book change is judged on every strategy's trades after it.
        scope = None if change["strategy_type"] == BOOK else [change["strategy_type"]]
        rows = [row for row in current_attributions(db, since=adopted_at, sources=SOURCES, strategy_types=scope)
                if _parse(_iso(row.opened_at)) >= adopted_at]
        watch = change.get("watch") or {}
        min_trades = int(watch.get("min_trades") or settings.experiment_verdict_min_trades)
        max_trades = int(watch.get("max_trades") or settings.experiment_verdict_max_trades)
        after = _recent_performance(rows, change["trigger"])
        shadow = _shadow_record(db, change, since=adopted_at, min_trades=min_trades)
        shadow_summary = ({"decisions": shadow.get("decisions"), "beating": shadow.get("beating"),
                           "candidates": shadow.get("candidates")} if shadow else None)
        beaten_by = shadow_beats_adopted(shadow)
        # A monthly sleeve is exempt from the 20-trade revert: its harm guardrail acts (sleeve_guardrail.py).
        sleeve = _sleeve_row(db, change)
        if sleeve is not None:
            beaten_by = None
            fixed = _sleeve_verdict(db, sleeve)
        else:
            fixed = _fixed_verdict(change, after, beaten_by, min_trades=min_trades, max_trades=max_trades)
        fixed_label = "skipped_monthly_sleeve" if sleeve is not None else fixed["verdict"]
        e_verdict = _e_process_safe(db, change, after_rows=rows, now=now, state=state, inputs=inputs, spy=spy)
        e_compact = _compact_e(e_verdict)
        bracket = _forward_bracket_safe(db, change, rows, max_trades=max_trades)
        acting = method
        if sleeve is not None:
            acting = SLEEVE_GUARDRAIL
        elif method == vs.METHOD_E_PROCESS and e_verdict.get("status") == "unavailable":
            acting = vs.METHOD_FIXED
        if acting == vs.METHOD_E_PROCESS:
            verdict = e_verdict.get("verdict") or "watching"
            reason = (f"e-process: {e_verdict.get('reason')} after {e_verdict.get('decided_at_trades')} trades "
                      f"(mean difference {_fmt_bps(e_verdict.get('mean_difference_bps'))} bps)")
            shadow_acted = False
        else:
            verdict, reason, shadow_acted = fixed["verdict"], fixed.get("reason"), fixed["shadow"]
        if verdict == "watching":
            if acting == SLEEVE_GUARDRAIL:
                needed = None
            elif acting == vs.METHOD_FIXED:
                needed = fixed.get("needed")
            else:
                needed = max(int(e_verdict.get("cap_trades") or vs.CAP_TRADES) - int(e_verdict.get("trades") or 0), 0)
            entry = {"change_id": change["change_id"], "strategy_type": change["strategy_type"],
                     "trades": after["trades"], "needed": needed, "shadow": shadow_summary,
                     "verdict_method": acting, "verdict_fixed_20": fixed_label, "verdict_e_process": e_compact,
                     "cost_bracket": bracket}
            if sleeve is not None:
                entry["sleeve_guardrail"] = fixed.get("guardrail")
            if fixed.get("so_far") and acting == vs.METHOD_FIXED:
                entry["so_far"] = fixed["so_far"]
            pending.append(entry)
            continue
        state_name = {"working": "confirmed", "not_helping": "reverted", "inconclusive": "closed"}[verdict]
        reverted = False
        if verdict == "not_helping":
            reverted = revert_change(db, change, now=now, reason=reason)
            if not reverted:
                state_name = "closed"
        acted_beaten = beaten_by if shadow_acted else None
        payload = {"change_id": change["change_id"], "strategy_id": change["strategy_id"],
                   "strategy_type": change["strategy_type"], "template": change["template"],
                   "trigger": change["trigger"], "verdict": verdict, "state": state_name, "reverted": reverted,
                   "before": change.get("recent_before"), "after": after, "judged_at": _iso(now),
                   "shadow": shadow_summary, "shadow_verdict": acted_beaten is not None,
                   **_verdict_audit_fields(change, after, shadow_summary),
                   "beaten_by": ({k: beaten_by.get(k) for k in ("candidate", "live", "overlap", "trades",
                                                                 "expectancy_bps", "delta_expectancy_bps",
                                                                 "delta_total_bps")} if acted_beaten else None),
                   "verdict_method": acting, "verdict_method_setting": method, "reason": reason,
                   "verdict_fixed_20": fixed_label, "verdict_e_process": e_compact,
                   "cost_bracket": bracket,
                   **({"sleeve_guardrail": fixed.get("guardrail")} if sleeve is not None else {}),
                   "paper_only": True}
        if acting in (vs.METHOD_E_PROCESS, SLEEVE_GUARDRAIL):
            message = f"{change['strategy_type']}: {change['description']} -> {verdict} ({reason})"
        else:
            message = (f"{change['strategy_type']}: {change['description']} -> {verdict} "
                       + (f"on shadow evidence: {reason}" if acted_beaten else
                          f"after {after['trades']} trades ({_fmt_bps(after['expectancy_bps'])} bps vs "
                          f"{(change.get('recent_before') or {}).get('expectancy_bps')} before)"))
        write_audit_log(db, event_type=EVENT, entity_type="risk_rule" if change["strategy_type"] == BOOK else "strategy",
                        entity_id=change["strategy_id"],
                        action=ACTION_VERDICT, status=state_name, message=message, payload=_clean(payload))
        reviewed.append(payload)
    wrote_state = False
    if json.dumps(state, sort_keys=True, default=str) != before_state:
        try:
            vs.write_elond_state(db, _clean(state), now=now)
            wrote_state = True
        except Exception as exc:
            db.rollback()
            logger.warning("experiment engine: e-LOND state not recorded: %s", exc)
    if reviewed or wrote_state:
        db.commit()
    return {"status": "complete", "reviewed": reviewed, "pending": pending, "verdict_method": method,
            "paper_only": True}


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


def _ledger_yardstick(db: Session) -> dict:
    """The yardstick's rules and the trials per family, for the ledger (read-only; never fails the ledger)."""
    from app.services import sleeve_guardrail, yardstick

    try:
        families = yardstick.trial_families(db)
    except Exception as exc:
        db.rollback()
        families = {"status": "unavailable", "error": f"{exc.__class__.__name__}: {exc}"[:200]}
    return {"bracket_bps": list(yardstick.BRACKET_BPS), "short_hold_veto": yardstick.VETO_RULE,
            "short_hold_veto_enabled": bool(settings.yardstick_short_hold_veto), "passive": yardstick.PASSIVE_RULE,
            "monthly_sleeves": sleeve_guardrail.RULE, "trial_families": families}


def experiment_ledger(db: Session, *, limit: int = 30) -> dict:
    runs = _history_rows(db, ACTION_RUN, 1)
    return {"rule": RULE, "yardstick": _ledger_yardstick(db), "verdict_methods": verdict_methods_view(db), "templates": [{"name": t.name, "kind": t.kind, "triggers": sorted(t.triggers), "key": t.key,
                                         "candidates": [t.label(c) for c in t.candidates], "dynamic": t.dynamic is not None,
                                         "reversion_candidates": ([t.label(c) for c in t.reversion_candidates]
                                                                  if t.reversion_candidates else None)}
                                        for t in TEMPLATES],
            "changes": adopted_changes(db), "experiments": experiment_history(db, limit=limit),
            "latest_run": (runs[0].payload if runs else None), "paper_only": True}


def verdict_methods_view(db: Session) -> dict:
    """Which forward judge acts, the measured inputs the e-process judge reads, and the e-LOND stream."""
    from app.services import verdict_stats as vs

    state = vs.elond_state(db)
    inputs = vs.current_inputs(db)
    changes = state.get("changes") or {}
    return {"acting": settings.experiment_verdict_method, "available": list(vs.METHODS),
            "multiplicity_hurdle": bool(settings.experiment_multiplicity_hurdle),
            "e_process": {"harm_alpha": vs.HARM_ALPHA, "improvement_fdr_alpha": vs.IMPROVEMENT_FDR_ALPHA,
                          "cap_trades": vs.CAP_TRADES, "keep_lcb_bps": vs.KEEP_LCB_BPS,
                          "market_timing_templates_unadjusted": sorted(vs.MARKET_TIMING_TEMPLATES)},
            "inputs": {k: inputs.get(k) for k in ("as_of", "source", "recorded", "loss_bound_bps", "sigma_bps", "rho")},
            "e_lond": {"alpha": state.get("alpha"), "gamma": state.get("gamma"), "changes": len(changes),
                       "confirmed": sum(1 for v in changes.values() if v.get("discovered")),
                       "as_of": state.get("as_of")},
            "fixed_20": {"min_trades": settings.experiment_verdict_min_trades,
                         "max_trades": settings.experiment_verdict_max_trades,
                         "revert_margin_bps": REVERT_EXPECTANCY_BPS,
                         "skips": "strategy rows with parameters.sleeve_cadence == 'monthly'"},
            "sleeve_guardrail": {"acts_on": "monthly sleeves", "rule": _sleeve_rule()}}


def _sleeve_rule() -> str:
    from app.services.sleeve_guardrail import RULE as SLEEVE_RULE

    return SLEEVE_RULE
