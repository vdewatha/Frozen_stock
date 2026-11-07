"""What each entry gate costs: the trades it stopped, scored as if they had been taken.

Every BUY signal the paper autotrader withholds is recorded in a
``buy_signal_outcomes`` audit row with the reason (``stock_paper_autotrader
._record_buy_outcomes``).  Until now those reasons were only counted
(``weekly_report.blocked_buy_reasons``): a gate earned its verdict on the
trades it let through, never on the ones it stopped.  This module follows the
stopped trades up.

1. **Blocked trades.**  Each withheld BUY of the last ``N`` sessions is
   attributed to a gate (``gate_for_reason``: the entry protection name, the
   VIX or regime gate, the earnings or announcement blackout, the
   one-position-per-symbol rule, ...) and deduplicated to one
   ``(symbol, strategy, session, gate)`` per session, since the cycle records
   the same block again every time the picture changes.
2. **Counterfactual.**  The blocked trade is replayed the way the backtester
   would have executed it: entry at the next session's open, then the
   strategy's own tested exit plan (``strategy_execution_policy
   .exit_policy_for``: 3% stop / 6% target / 20 sessions for the technical
   strategies, 10% stop / 10 sessions / indicator exit for RSI(2) and IBS)
   over the stored daily bars, with ``triple_barrier.label_events`` costs on
   both sides.  A trade still inside its horizon is ``open``, not scored.
3. **Verdict per gate and per strategy.**  Blocked, scored and open counts,
   mean net return, win rate, the net profit the gate forfeited (the blocked
   winners) against the losses it avoided (the blocked losers).  With at least
   ``MIN_SCORED`` scored trades, a positive mean marks the gate ``costly`` and
   a negative one ``earning its keep``; fewer trades is ``too early``.

The result is a ``gate_cost`` audit row (no migration), read by
``GET /stock-paper/gate-costs`` and the learning scoreboard.  A gate flagged
costly for a strategy becomes a lesson for the experiment engine
(``costly_gate_lessons``), which plans the matching template with "off" among
its candidates under the engine's unchanged pre-registered rule.  Nothing
here places, blocks or exits an order.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import logging
import math
import re
from typing import Any, Callable, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import AuditLog, IntradayBar, Strategy
from app.services.audit import write_audit_log
from app.services.exchange_sessions import is_nyse_session, session_bounds
from app.services.triple_barrier import BarrierConfig, label_events

logger = logging.getLogger(__name__)
UTC = timezone.utc
ET = ZoneInfo("America/New_York")
EVENT = "stock_paper"
ACTION = "gate_cost"
BUY_OUTCOME_ACTION = "buy_signal_outcomes"
MIN_SCORED = 30
MAX_STORED_TRADES = 2000
COSTLY = "costly"
EARNING = "earning its keep"
TOO_EARLY = "too early"
NEUTRAL = "neutral"
# Gates the experiment engine has a template for, and the lesson cause that plans it.
ENGINE_GATES = {"market_gate": "costly_market_gate", "vix_gate": "costly_vix_gate",
                "earnings_blackout": "costly_earnings_blackout", "sector_gate": "costly_sector_gate",
                "breadth_gate": "costly_breadth_gate", "entry_cap": "costly_entry_cap"}
LESSON_TEXT = ("The {gate} gate stopped {scored} trades that, taken with the strategy's tested exits, "
               "averaged {mean_bps:+.1f} bps net: the gate forfeits more than it saves.")
PROPOSAL_TEXT = "Replay the strategy with the gate off against its current setup under the pre-registered rule."
# Risk-rule bracket a policy leg of ``None`` falls back to (``stock_paper_autotrader``).
DEFAULT_STOP = 0.03
DEFAULT_TAKE = 0.03
DEFAULT_HOLD_SESSIONS = 20
# Daily bars loaded per symbol: the lookback window plus the longest horizon
# plus the cost model's volatility lookback, with room for holidays.
HISTORY_BARS = 260


# --------------------------------------------------------------- gate names

def gate_for_reason(reason: Any, status: Any = None) -> Optional[str]:
    """Which gate a withheld BUY's reason names; ``None`` for a submitted or failed signal."""
    text = str(reason or "")
    if status == "submitted" or status == "failed":
        return None
    match = re.match(r"entry protection ([a-z_]+):", text)
    if match:
        return match.group(1)
    if text.startswith("VIX gate"):
        return "vix_gate"
    if text.startswith("Regime gate"):
        return "regime_gate"
    if "earnings on" in text and "hold window" in text:
        return "earnings_blackout"
    if "no new BUYs until" in text or "position would be held through it" in text:
        return "announcement_blackout"
    if text.startswith("one position per symbol"):
        return "one_position_per_symbol"
    if text.startswith("sector group"):
        return "sector_cap"
    if "meta-label" in text.lower() or "meta label" in text.lower():
        return "meta_label"
    if "no same-cycle re-entry" in text:
        return "exit_pending"
    if "no pyramiding" in text:
        return "no_pyramiding"
    if "broker-side exit legs" in text:
        return "exit_legs_live"
    if "news admission expired" in text:
        return "news_expired"
    if "open-position risk limit" in text:
        return "position_limit"
    if text.startswith("Exploration"):
        return "exploration_limit"
    if "daily-bar entry window" in text:
        return "entry_window"
    if not text:
        return None
    return "other"


# ---------------------------------------------------------------- sessions

def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _session_date(stamp: datetime) -> date:
    return _utc(stamp).astimezone(ET).date()


def sessions_back(day: date, count: int) -> date:
    """The NYSE session ``count`` sessions before ``day`` (``day`` itself when ``count`` is 0)."""
    seen = 0
    cursor = day
    guard = 0
    while seen < count and guard < count * 3 + 30:
        cursor -= timedelta(days=1)
        guard += 1
        if is_nyse_session(cursor):
            seen += 1
    return cursor


def last_completed_session(now: datetime) -> date:
    """The latest NYSE session whose close is at or before ``now``."""
    day = now.astimezone(ET).date()
    for _ in range(14):
        bounds = session_bounds(day)
        if bounds is not None and _utc(bounds[1]) <= now:
            return day
        day -= timedelta(days=1)
    return day


# ------------------------------------------------------------ blocked trades

def _outcome_rows(db: Session, start: datetime, end: datetime) -> list[AuditLog]:
    return db.query(AuditLog).filter(
        AuditLog.event_type == EVENT, AuditLog.action == BUY_OUTCOME_ACTION,
        AuditLog.created_at >= start.astimezone(UTC).replace(tzinfo=None),
        AuditLog.created_at <= end.astimezone(UTC).replace(tzinfo=None),
    ).order_by(AuditLog.id).all()


def blocked_trades(rows: Iterable[AuditLog]) -> list[dict]:
    """One blocked BUY per ``(symbol, strategy, session, gate)`` from the outcome rows, oldest first."""
    seen: set[tuple] = set()
    out = []
    for row in rows:
        payload = row.payload or {}
        observed = payload.get("observed_at")
        try:
            stamp = datetime.fromisoformat(str(observed)) if observed else _utc(row.created_at)
        except ValueError:
            stamp = _utc(row.created_at)
        session = _session_date(stamp)
        for outcome in payload.get("outcomes") or []:
            gate = gate_for_reason(outcome.get("reason"), outcome.get("status"))
            if gate is None or not outcome.get("symbol") or not outcome.get("strategy"):
                continue
            key = (str(outcome["symbol"]).upper(), str(outcome["strategy"]), session.isoformat(), gate)
            if key in seen:
                continue
            seen.add(key)
            out.append({"symbol": key[0], "strategy": key[1], "session": key[2], "gate": gate,
                        "reason": outcome.get("reason"), "mode": outcome.get("mode"),
                        "observed_at": _utc(stamp).isoformat()})
    return out


# ------------------------------------------------------------------ scoring

def barrier_for(policy: Any) -> BarrierConfig:
    """The strategy's tested exit plan as a triple-barrier config (costs as in ``triple_barrier``)."""
    stop = getattr(policy, "stop_loss_pct", None)
    take = getattr(policy, "take_profit_pct", None)
    hold = getattr(policy, "max_holding_sessions", None)
    return BarrierConfig(
        take_profit_pct=float(DEFAULT_TAKE if take is None else take),
        stop_loss_pct=float(DEFAULT_STOP if stop is None else stop),
        max_holding_days=int(hold) if hold else DEFAULT_HOLD_SESSIONS,
    )


def daily_frame(db: Session, symbol: str) -> Optional[pd.DataFrame]:
    """Trusted completed daily bars (``trusted_history`` over ``market_prices``), oldest first."""
    from app.services.trusted_data import UntrustedMarketData, trusted_history

    try:
        prices, _source = trusted_history(db, symbol, limit=HISTORY_BARS, require_active=False)
    except UntrustedMarketData as exc:
        logger.info("gate cost: no trusted daily bars for %s: %s", symbol, exc)
        return None
    except Exception as exc:  # one symbol must not stop the run
        db.rollback()
        logger.warning("gate cost: daily bars unavailable for %s: %s", symbol, exc)
        return None
    return normalise_frame(prices)


def normalise_frame(prices: pd.DataFrame) -> Optional[pd.DataFrame]:
    if prices is None or len(prices) == 0 or "date" not in prices or "close" not in prices:
        return None
    frame = prices.copy()
    frame["date"] = pd.to_datetime(frame["date"], utc=True, errors="coerce").dt.strftime("%Y-%m-%d")
    frame = frame.dropna(subset=["date"]).drop_duplicates("date").sort_values("date").reset_index(drop=True)
    for column in ("open", "high", "low", "close"):
        if column not in frame:
            frame[column] = frame["close"]
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if "volume" not in frame:
        frame["volume"] = np.nan
    return frame[["date", "open", "high", "low", "close", "volume"]]


# Only consolidated minute bars give the opening print the replay is scored
# against; an IEX-only first trade (about 3% of volume) is a sample of it, so
# without a SIP bar the consolidated daily open stands.
CONSOLIDATED_FEED_CLASSES = ("sip", "sip_delayed")


def intraday_open(db: Session, symbol: str, session: date) -> Optional[float]:
    """The first consolidated minute bar's open of ``session`` where SIP 1m bars exist (the real opening print)."""
    bounds = session_bounds(session)
    if bounds is None:
        return None
    opened = _utc(bounds[0])
    try:
        row = db.query(IntradayBar).filter(
            IntradayBar.symbol == symbol, IntradayBar.timeframe == "1m",
            IntradayBar.feed_class.in_(CONSOLIDATED_FEED_CLASSES),
            IntradayBar.opened_at >= opened, IntradayBar.opened_at < opened + timedelta(minutes=5),
        ).order_by(IntradayBar.opened_at).first()
    except Exception:
        db.rollback()
        return None
    if row is None or row.open is None or float(row.open) <= 0:
        return None
    return float(row.open)


def score_trade(trade: Mapping, frame: Optional[pd.DataFrame], policy: Any, *,
                exit_signals: Optional[np.ndarray] = None, entry_open: Optional[float] = None) -> dict:
    """Replay one blocked BUY from the next open under the strategy's exit plan.

    ``trade["session"]`` is the session the BUY was withheld in; the decision
    bar is that session's bar (the last completed bar the paper cycle could
    have acted on by its close) and the entry is the next bar's open.
    ``entry_open`` replaces the stored daily open when an intraday opening
    print is known.  Returns the trade with ``status`` ``scored`` /
    ``open`` / ``unscorable`` and, when scored, the path and net return.
    """
    out = dict(trade)
    if policy is None:
        return out | {"status": "unscorable", "why": "strategy has no tested exit plan"}
    if frame is None or frame.empty:
        return out | {"status": "unscorable", "why": "no daily bars"}
    dates = frame["date"].astype(str).to_numpy()
    on_or_before = np.flatnonzero(dates <= trade["session"])
    if not len(on_or_before):
        return out | {"status": "unscorable", "why": "no daily bar on or before the session"}
    t = int(on_or_before[-1])
    if dates[t] != trade["session"]:
        # The session's bar is not stored yet (the import lags): nothing to enter from.
        return out | {"status": "open", "why": "the session's daily bar is not stored yet", "sessions_elapsed": 0}
    config = barrier_for(policy)
    out["exit_plan"] = {"stop_loss_pct": config.stop_loss_pct, "take_profit_pct": config.take_profit_pct,
                        "max_holding_sessions": config.max_holding_days,
                        "indicator_exit": bool(getattr(policy, "indicator_exit", False))}
    if t + 1 >= len(frame):
        return out | {"status": "open", "why": "the next open has not happened yet", "sessions_elapsed": 0}
    replay = frame
    if entry_open is not None and math.isfinite(entry_open) and entry_open > 0:
        replay = frame.copy()
        replay.loc[t + 1, "open"] = entry_open
        replay.loc[t + 1, "high"] = max(float(replay.loc[t + 1, "high"]), entry_open)
        replay.loc[t + 1, "low"] = min(float(replay.loc[t + 1, "low"]), entry_open)
        out["entry_source"] = "intraday_open"
    else:
        out["entry_source"] = "daily_open"
    signals = None
    if getattr(policy, "indicator_exit", False) and exit_signals is not None and len(exit_signals) == len(replay):
        signals = exit_signals
    labels = label_events(replay, [t], config, exit_signals=signals)
    if labels.empty:
        return out | {"status": "open", "sessions_elapsed": int(len(frame) - 1 - (t + 1)),
                      "entry_date": str(dates[t + 1]), "entry_price": float(replay.loc[t + 1, "open"])}
    row = labels.iloc[0]
    return out | {
        "status": "scored", "decision_date": str(row["decision_date"]), "entry_date": str(row["entry_date"]),
        "exit_date": str(row["exit_date"]), "entry_price": round(float(row["entry_price"]), 6),
        "exit_price": round(float(row["exit_price"]), 6), "barrier": str(row["barrier"]),
        "holding_sessions": int(row["holding_days"]), "gross_return": round(float(row["gross_return"]), 6),
        "net_return": round(float(row["net_return"]), 6), "win": bool(row["net_return"] > 0),
    }


def _policy_by_type(db: Session) -> dict[str, Any]:
    from app.services.strategy_execution_policy import exit_policy_for

    policies: dict[str, Any] = {}
    rows = db.query(Strategy).order_by(Strategy.id).all()
    # Prefer the active row's parameters when a strategy type has several rows.
    rows.sort(key=lambda row: (getattr(row, "current_status", "") != "paper_trading_active", row.id))
    for row in rows:
        if row.strategy_type in policies:
            continue
        try:
            policies[row.strategy_type] = exit_policy_for(row)
        except Exception:
            policies[row.strategy_type] = None
    return policies


def _exit_signals(frame: pd.DataFrame, strategy_type: str) -> Optional[np.ndarray]:
    from app.services import meta_label_primary as primary

    try:
        signals = primary.replay_exit_signals(frame, strategy_type)
    except Exception as exc:
        logger.info("gate cost: indicator exit unavailable for %s: %s", strategy_type, exc)
        return None
    return None if signals is None else np.asarray(signals, dtype=bool)


def score_trades(db: Session, trades: list[dict], *, loader: Optional[Callable] = None,
                 policies: Optional[Mapping[str, Any]] = None, opens: Optional[Callable] = None) -> list[dict]:
    """Score every blocked trade; bars are loaded once per symbol, exits once per (symbol, strategy)."""
    loader = loader or (lambda symbol: daily_frame(db, symbol))
    opens = opens or (lambda symbol, session: intraday_open(db, symbol, session))
    policies = dict(policies) if policies is not None else _policy_by_type(db)
    frames: dict[str, Optional[pd.DataFrame]] = {}
    exits: dict[tuple[str, str], Optional[np.ndarray]] = {}
    out = []
    for trade in trades:
        symbol = trade["symbol"]
        if symbol not in frames:
            frames[symbol] = loader(symbol)
        frame = frames[symbol]
        policy = policies.get(trade["strategy"])
        key = (symbol, trade["strategy"])
        if key not in exits:
            exits[key] = _exit_signals(frame, trade["strategy"]) if (
                frame is not None and getattr(policy, "indicator_exit", False)) else None
        entry_session = None
        if frame is not None:
            dates = frame["date"].astype(str).to_numpy()
            after = np.flatnonzero(dates > trade["session"])
            if len(after):
                entry_session = date.fromisoformat(str(dates[after[0]]))
        entry_open = opens(symbol, entry_session) if entry_session is not None else None
        out.append(score_trade(trade, frame, policy, exit_signals=exits[key], entry_open=entry_open))
    return out


# -------------------------------------------------------------- aggregation

def _bps(value: float) -> float:
    return round(value * 10_000, 1)


def verdict(scored: int, mean_return: Optional[float], *, min_scored: int = MIN_SCORED) -> str:
    if scored < min_scored or mean_return is None:
        return TOO_EARLY
    if mean_return > 0:
        return COSTLY
    if mean_return < 0:
        return EARNING
    return NEUTRAL


def summarise(trades: Iterable[dict], *, min_scored: int = MIN_SCORED, notional: float = BarrierConfig().position_notional) -> dict:
    """Counts, mean net return, win rate and forfeited-vs-avoided totals for one group of trades."""
    trades = list(trades)
    scored = [trade for trade in trades if trade.get("status") == "scored"]
    returns = [float(trade["net_return"]) for trade in scored]
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value <= 0]
    mean = sum(returns) / len(returns) if returns else None
    forfeited = sum(wins)
    avoided = -sum(losses)
    label = verdict(len(scored), mean, min_scored=min_scored)
    return {
        "blocked": len(trades), "scored": len(scored),
        "open": sum(trade.get("status") == "open" for trade in trades),
        "unscorable": sum(trade.get("status") == "unscorable" for trade in trades),
        "mean_net_bps": _bps(mean) if mean is not None else None,
        "win_rate": round(len(wins) / len(scored), 4) if scored else None,
        "forfeited_profit_bps": _bps(forfeited), "avoided_losses_bps": _bps(avoided),
        "net_bps": _bps(forfeited - avoided),
        # The same totals in dollars at the cost model's nominal order size.
        "forfeited_profit": round(forfeited * notional, 2), "avoided_losses": round(avoided * notional, 2),
        "net_profit_forfeited": round((forfeited - avoided) * notional, 2),
        "verdict": label,
        "evidence": ("enough scored trades to judge" if len(scored) >= min_scored
                     else f"{min_scored - len(scored)} more scored trades before judging"),
    }


def aggregate(trades: list[dict], *, min_scored: int = MIN_SCORED) -> dict:
    """Per gate (across strategies) and per (gate, strategy)."""
    by_gate: dict[str, list[dict]] = defaultdict(list)
    by_pair: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for trade in trades:
        by_gate[trade["gate"]].append(trade)
        by_pair[(trade["gate"], trade["strategy"])].append(trade)
    gates = [{"gate": gate, **summarise(members, min_scored=min_scored)} for gate, members in by_gate.items()]
    gates.sort(key=lambda item: (-item["blocked"], item["gate"]))
    pairs = [{"gate": gate, "strategy_type": strategy, **summarise(members, min_scored=min_scored)}
             for (gate, strategy), members in by_pair.items()]
    pairs.sort(key=lambda item: (-item["blocked"], item["gate"], item["strategy_type"]))
    return {"gates": gates, "by_strategy": pairs}


# ------------------------------------------------------------------- report

def build_gate_costs(db: Session, *, now: Optional[datetime] = None, sessions: Optional[int] = None,
                     min_scored: Optional[int] = None, loader: Optional[Callable] = None,
                     policies: Optional[Mapping[str, Any]] = None, opens: Optional[Callable] = None) -> dict:
    now = _utc(now or datetime.now(UTC))
    sessions = int(sessions if sessions is not None else settings.gate_cost_lookback_sessions)
    min_scored = int(min_scored if min_scored is not None else settings.gate_cost_min_scored)
    as_of = last_completed_session(now)
    first = sessions_back(as_of, max(sessions - 1, 0))
    start = datetime.combine(first, datetime.min.time(), ET)
    blocked = blocked_trades(_outcome_rows(db, start, now))
    scored = score_trades(db, blocked, loader=loader, policies=policies, opens=opens)
    totals = aggregate(scored, min_scored=min_scored)
    stored = scored[-MAX_STORED_TRADES:]
    return {
        "as_of": as_of.isoformat(), "generated_at": now.isoformat(),
        "window": {"sessions": sessions, "first_session": first.isoformat(), "last_session": as_of.isoformat()},
        "min_scored": min_scored,
        "totals": summarise(scored, min_scored=min_scored),
        **totals,
        "trades": stored, "trades_truncated": len(scored) - len(stored),
        "method": ("Each withheld BUY is replayed from the next session's open under the strategy's tested exit "
                   "plan over stored daily bars, with volatility-scaled costs on both sides (triple_barrier). "
                   "Trades still inside their horizon are open, not scored."),
        "paper_only": True, "mutates_strategy_state": False,
    }


def latest_gate_costs(db: Session) -> Optional[dict]:
    row = db.query(AuditLog).filter(AuditLog.event_type == EVENT, AuditLog.action == ACTION).order_by(
        AuditLog.id.desc()).first()
    return (row.payload or {}).get("report") if row else None


def record_gate_costs(db: Session, *, now: Optional[datetime] = None, force: bool = False, **kwargs) -> dict:
    """Store one gate-cost report per completed session (paper only)."""
    now = _utc(now or datetime.now(UTC))
    as_of = last_completed_session(now).isoformat()
    latest = latest_gate_costs(db)
    if latest and latest.get("as_of") == as_of and not force:
        return {"status": "complete", "recorded": False, "as_of": as_of,
                "reason": "gate costs already recorded for this session", "paper_only": True}
    report = build_gate_costs(db, now=now, **kwargs)
    costly = [f"{item['gate']}/{item['strategy_type']}" for item in report["by_strategy"] if item["verdict"] == COSTLY]
    totals = report["totals"]
    write_audit_log(
        db, event_type=EVENT, action=ACTION, status="recorded",
        message=(f"gate costs through {as_of}: {totals['blocked']} blocked BUYs, {totals['scored']} scored, "
                 f"{len(costly)} costly gate/strategy pairs"),
        payload={"report": report, "paper_only": True},
    )
    db.commit()
    return {"status": "complete", "recorded": True, "as_of": as_of, "blocked": totals["blocked"],
            "scored": totals["scored"], "open": totals["open"], "costly": costly, "paper_only": True}


# -------------------------------------------------------------- projections

def gate_costs_view(db: Session) -> dict:
    """The latest stored report for ``GET /stock-paper/gate-costs``."""
    latest = latest_gate_costs(db)
    return {"latest": latest, "min_scored": int(settings.gate_cost_min_scored),
            "lookback_sessions": int(settings.gate_cost_lookback_sessions),
            "verdicts": {COSTLY: "blocked trades average a net profit over the evidence minimum: the gate forfeits "
                                 "more than it saves", EARNING: "blocked trades average a net loss: the gate avoids "
                                 "losses", TOO_EARLY: "fewer scored trades than the evidence minimum"},
            "engine_gates": sorted(ENGINE_GATES), "paper_only": True}


def scoreboard_gate_costs(db: Session) -> dict:
    """The ``gate_costs`` section of the learning scoreboard."""
    latest = latest_gate_costs(db)
    if not latest:
        return {"as_of": None, "status": "waiting",
                "detail": "No gate-cost report yet; the first runs after the next session's close.",
                "gates": [], "by_strategy": [], "costly": []}
    totals = latest.get("totals") or {}
    keys = ("gate", "strategy_type", "blocked", "scored", "open", "mean_net_bps", "win_rate",
            "forfeited_profit_bps", "avoided_losses_bps", "net_bps", "verdict", "evidence")
    pairs = [{key: item.get(key) for key in keys} for item in latest.get("by_strategy") or []]
    gates = [{key: item.get(key) for key in keys if key != "strategy_type"} for item in latest.get("gates") or []]
    costly = [item for item in pairs if item["verdict"] == COSTLY]
    return {
        "as_of": latest.get("as_of"), "window": latest.get("window"), "min_scored": latest.get("min_scored"),
        "status": "ok",
        "detail": (f"{totals.get('blocked', 0)} blocked BUYs in the window, {totals.get('scored', 0)} scored, "
                   f"{totals.get('open', 0)} still open; {len(costly)} gate/strategy pairs are costly."),
        "totals": totals, "gates": gates, "by_strategy": pairs, "costly": costly,
    }


def costly_gate_lessons(db: Session, *, report: Optional[Mapping] = None) -> list[dict]:
    """Lessons for the experiment engine: one per costly (engine gate, strategy) pair.

    Shaped like ``loss_diagnosis.repeating_failures`` rows so ``plan_experiments``
    treats them as any other lesson; the cause names the gate's template.
    """
    report = report if report is not None else latest_gate_costs(db)
    if not report:
        return []
    out = []
    for item in report.get("by_strategy") or []:
        gate = item.get("gate")
        if item.get("verdict") != COSTLY or gate not in ENGINE_GATES:
            continue
        mean = float(item.get("mean_net_bps") or 0.0)
        out.append({
            "strategy_type": item["strategy_type"], "cause": ENGINE_GATES[gate], "family": "gate_cost",
            "count": int(item.get("scored") or 0), "blocked": int(item.get("blocked") or 0),
            "mean_net_bps": mean, "net_bps": item.get("net_bps"), "gate": gate,
            "lesson": LESSON_TEXT.format(gate=gate, scored=item.get("scored"), mean_bps=mean),
            "proposal": PROPOSAL_TEXT, "contributing": {}, "source": "gate_cost",
            "as_of": report.get("as_of"), "auto_apply": False, "requires_fresh_holdout_and_forward_trial": True,
        })
    out.sort(key=lambda item: (-item["count"], item["strategy_type"], item["cause"]))
    return out
