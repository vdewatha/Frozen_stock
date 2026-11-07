"""Signal autopsy: what did this strategy's losing entries look like that its winning ones did not?

``signal_wrong`` (the stock fell on its own, beyond the market and its
sector) is the largest loss cause in the paper ledger and, until now, the one
cause without an experiment template: the lesson said "compare the entry
features of these losses with this strategy's wins" and nobody did.  This
module does, weekly, from the data:

1. **Features at the decision bar.**  For every closed paper trade of a
   strategy (``trade_attribution.current_attributions``, scheduled and
   exploration round trips) the entry-time features are recomputed from the
   stored daily bars that end on the decision bar (the last completed
   session before the entry session), with the same ``rules.feature``
   functions the discovered rules and the live scope use: ``rsi_2``,
   ``rsi_14``, ``dist_sma_200``, ``dist_sma_50``, ``atr_pct_14``, ``gap``,
   ``vol_ratio_20_100``, ``ibs``, ``bb_pctb_20``, ``down_streak``, ``dow``,
   plus the VIX and breadth the attribution recorded at entry.
2. **Contrast.**  Per feature, losses against wins: the difference in means,
   Cohen's d, and a two-sided permutation p-value on the difference in means
   (``PERMUTATIONS`` label shuffles, seeded), corrected across features with
   Benjamini-Hochberg (``q_value``).
3. **Scope candidates.**  A rule feature that separates at ``q < MAX_Q``
   with at least ``MIN_SIDE`` trades on each side becomes a candidate scope
   rule: a one-condition threshold at the median of the winners' side, in
   the direction of the winners (``rsi_2 < 9.8``, ``dist_sma_200 > 0.01``).
   VIX and breadth are contrasted and reported but are not scope features
   (the VIX gate template owns the VIX).
4. **Ledger.**  One ``signal_autopsy`` audit row per strategy with at least
   ``MIN_TRADES`` trades (no migration): the table and the candidates.  The
   experiment engine's ``scope`` template reads the latest row for the
   strategy (``scope_candidates``) and replays each candidate under the
   engine's unchanged pre-registered rule; a candidate that cuts the history
   below 30 trades per window simply fails it.  Adoption writes
   ``parameters["scope"]`` (``strategies/scope.py``) through the governance
   action ``scope_update``.

Paper only.  Nothing here places, sizes or blocks an order; the autopsy is a
hindsight table, and only the engine's rule turns it into a change.
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping, Optional

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session, object_session
from sqlalchemy.orm.exc import UnmappedInstanceError

from app.models import AuditLog, MarketPrice, Strategy
from app.services.audit import write_audit_log
from app.services.strategies import rules
from app.services.strategies.scope import SCOPE_KEY, describe_scope, scope_condition, scope_label

logger = logging.getLogger(__name__)
UTC = timezone.utc
EVENT = "stock_paper"
ACTION = "signal_autopsy"
GOVERNANCE_EVENT = "strategy_governance"
SCOPE_ACTION = "scope_update"
VERSION = "signal-autopsy-v1"
SOURCES = ("scheduled_round_trip", "exploration_round_trip")
TRIGGER = "signal_wrong"
RULE_FEATURES = ("rsi_2", "rsi_14", "dist_sma_200", "dist_sma_50", "atr_pct_14", "gap", "vol_ratio_20_100", "ibs",
                 "bb_pctb_20", "down_streak", "dow")
CONTEXT_FEATURES = ("entry_vix", "entry_breadth")
FEATURES = (*RULE_FEATURES, *CONTEXT_FEATURES)
MIN_TRADES = 10          # a strategy is autopsied from this many closed trades
MIN_SIDE = 10            # a candidate needs this many losses and this many wins with the feature
MAX_Q = 0.10             # Benjamini-Hochberg q below which a feature separates
PERMUTATIONS = 2000
MAX_CANDIDATES = 5
HISTORY_BARS = 420       # bars before the entry session the features are computed on (rsi/sma_200 need 201)
METHOD = (f"Per feature, losses vs wins: difference in means, Cohen's d and a two-sided permutation p-value "
          f"({PERMUTATIONS} label shuffles, seeded), Benjamini-Hochberg corrected across features. A rule feature "
          f"with q < {MAX_Q:g} and at least {MIN_SIDE} trades on each side is a scope candidate: a one-condition "
          f"threshold at the median of the winners, in the winners' direction. Features are computed with "
          f"rules.feature on the stored daily bars ending on the decision bar (the last completed session before "
          f"the entry session), corporate-action adjusted like the paper frame; VIX and breadth come from the "
          f"attribution's entry context.")


# ----------------------------------------------------------------- helpers

def _f(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clean(value: Any) -> Any:
    """JSON-safe copy: numpy scalars to Python, NaN to None, dates to ISO."""
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()
    return str(value)


def _session(moment: datetime) -> date:
    from app.services.trade_attribution import session_date

    return session_date(moment)


# ------------------------------------------------------------ decision bars

def symbol_bars(db: Session, symbol: str, before: date) -> pd.DataFrame:
    """Stored daily bars of ``symbol`` dated before ``before``, oldest first, one per date (lowest id wins).

    When every bar carries a valid ``adjusted_close`` the OHLC are scaled by
    ``adjusted_close / close`` exactly as the paper frame is
    (``trusted_data.trusted_history``), so a feature here equals the one the
    strategy saw.
    """
    rows = db.execute(
        select(MarketPrice.price_date, MarketPrice.open, MarketPrice.high, MarketPrice.low, MarketPrice.close,
               MarketPrice.adjusted_close, MarketPrice.volume)
        .where(MarketPrice.symbol == symbol, MarketPrice.price_date < before)
        .order_by(MarketPrice.price_date, MarketPrice.id)
    ).all()
    seen: dict[date, dict] = {}
    for price_date, open_, high, low, close, adjusted, volume in rows:
        if price_date in seen or not _f(close):
            continue
        seen[price_date] = {"date": price_date.isoformat(), "open": _f(open_), "high": _f(high), "low": _f(low),
                            "close": _f(close), "adjusted_close": _f(adjusted), "volume": _f(volume)}
    if not seen:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    frame = pd.DataFrame([seen[key] for key in sorted(seen)])
    for column in ("open", "high", "low"):
        frame[column] = frame[column].fillna(frame["close"])
    adjusted = frame["adjusted_close"].to_numpy(float)
    factor = adjusted / frame["close"].to_numpy(float)
    if np.isfinite(factor).all() and (factor > 0).all():
        for column in ("open", "high", "low", "close"):
            frame[column] = frame[column] * factor
    return frame.drop(columns=["adjusted_close"]).reset_index(drop=True)


def decision_frame(bars: pd.DataFrame, entry_session: date, *, history: int = HISTORY_BARS) -> pd.DataFrame:
    """The bars a decision on ``entry_session`` was made from: the last ``history`` bars dated before it."""
    if bars is None or bars.empty:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    before = bars.loc[bars["date"].astype(str) < entry_session.isoformat()]
    return before.iloc[-history:].reset_index(drop=True)


def entry_features(frame: pd.DataFrame) -> dict:
    """The rule features on the frame's last row (the decision bar); ``None`` where not finite."""
    out: dict[str, Optional[float]] = {name: None for name in RULE_FEATURES}
    if frame is None or frame.empty:
        return out | {"decision_bar": None}
    cache: dict = {}
    for name in RULE_FEATURES:
        try:
            value = float(rules.feature(frame, name, cache).iloc[-1])
        except (KeyError, ValueError, TypeError, IndexError):
            value = float("nan")
        out[name] = round(value, 6) if math.isfinite(value) else None
    return out | {"decision_bar": str(frame["date"].iloc[-1])}


def trade_features(db: Session, rows: Iterable[Any]) -> list[dict]:
    """One record per attribution row: the decision-bar features plus the entry VIX and breadth."""
    rows = list(rows)
    by_symbol: dict[str, list] = defaultdict(list)
    for row in rows:
        by_symbol[row.symbol].append(row)
    out = []
    for symbol, members in by_symbol.items():
        sessions = {row.id: _session(row.opened_at) for row in members}
        bars = symbol_bars(db, symbol, max(sessions.values()))
        for row in members:
            detail = row.detail or {}
            diagnosis = detail.get("diagnosis") or {}
            features = entry_features(decision_frame(bars, sessions[row.id]))
            features["entry_vix"] = _f(diagnosis.get("entry_vix"))
            features["entry_breadth"] = _f(diagnosis.get("entry_breadth"))
            net = row.net_return if row.net_return is not None else row.gross_return
            out.append({"symbol": symbol, "opened_at": _iso(row.opened_at), "closed_at": _iso(row.closed_at),
                        "entry_session": sessions[row.id].isoformat(), "is_loss": bool(row.is_loss),
                        "return": _f(net), "cause": diagnosis.get("cause"), "features": features})
    out.sort(key=lambda item: (item["closed_at"] or "", item["symbol"]))
    return out


# --------------------------------------------------------------- statistics

def permutation_test(losses: Iterable[float], wins: Iterable[float], *, permutations: int = PERMUTATIONS,
                     seed: int = 0) -> dict:
    """Difference in means (losses - wins), Cohen's d and a two-sided permutation p-value.  Pure."""
    a = np.asarray([v for v in losses if v is not None and math.isfinite(float(v))], dtype=float)
    b = np.asarray([v for v in wins if v is not None and math.isfinite(float(v))], dtype=float)
    out = {"n_losses": int(len(a)), "n_wins": int(len(b)), "mean_losses": None, "mean_wins": None,
           "median_losses": None, "median_wins": None, "diff": None, "effect_size": None, "p_value": None}
    if len(a):
        out.update(mean_losses=round(float(a.mean()), 6), median_losses=round(float(np.median(a)), 6))
    if len(b):
        out.update(mean_wins=round(float(b.mean()), 6), median_wins=round(float(np.median(b)), 6))
    if len(a) < 2 or len(b) < 2:
        return out
    diff = float(a.mean() - b.mean())
    pooled = math.sqrt(((len(a) - 1) * a.var(ddof=1) + (len(b) - 1) * b.var(ddof=1)) / (len(a) + len(b) - 2))
    out["diff"] = round(diff, 6)
    out["effect_size"] = round(diff / pooled, 4) if pooled > 0 else 0.0
    pooled_values = np.concatenate([a, b])
    rng = np.random.default_rng(seed)
    order = np.argsort(rng.random((int(permutations), len(pooled_values))), axis=1)
    shuffled = pooled_values[order]
    simulated = shuffled[:, :len(a)].mean(axis=1) - shuffled[:, len(a):].mean(axis=1)
    hits = int(np.sum(np.abs(simulated) >= abs(diff) - 1e-12))
    out["p_value"] = round((hits + 1) / (int(permutations) + 1), 6)
    return out


def benjamini_hochberg(p_values: Iterable[Optional[float]]) -> list[Optional[float]]:
    """Step-up q-values; ``None`` entries are left out of the family and returned as ``None``."""
    values = list(p_values)
    tested = [(p, i) for i, p in enumerate(values) if p is not None]
    out: list[Optional[float]] = [None] * len(values)
    if not tested:
        return out
    m = len(tested)
    ranked = sorted(tested, key=lambda item: item[0])
    running = 1.0
    for rank in range(m, 0, -1):
        p, index = ranked[rank - 1]
        running = min(running, p * m / rank)
        out[index] = round(min(running, 1.0), 6)
    return out


def contrast(trades: Iterable[Mapping], *, permutations: int = PERMUTATIONS, seed: int = 0) -> list[dict]:
    """Per feature: losses vs wins, with the corrected q-value.  Pure."""
    trades = list(trades)
    table = []
    for name in FEATURES:
        losses = [t["features"].get(name) for t in trades if t["is_loss"]]
        wins = [t["features"].get(name) for t in trades if not t["is_loss"]]
        table.append({"feature": name, "scope_feature": name in RULE_FEATURES,
                      **permutation_test(losses, wins, permutations=permutations, seed=seed)})
    for row, q in zip(table, benjamini_hochberg([row["p_value"] for row in table])):
        row["q_value"] = q
    return table


def candidates(table: Iterable[Mapping], *, min_side: int = MIN_SIDE, max_q: float = MAX_Q,
               limit: int = MAX_CANDIDATES) -> list[dict]:
    """Scope rules from the features that separate: threshold at the winners' median, in the winners' direction."""
    out = []
    for row in table:
        if not row.get("scope_feature") or row.get("q_value") is None or row["q_value"] >= max_q:
            continue
        if row["n_losses"] < min_side or row["n_wins"] < min_side:
            continue
        if row["mean_wins"] is None or row["mean_losses"] is None or row["mean_wins"] == row["mean_losses"]:
            continue
        op = "<" if row["mean_wins"] < row["mean_losses"] else ">"
        rule = {"f": row["feature"], "op": op, "v": round(float(row["median_wins"]), 4)}
        out.append({"rule": rule, "label": scope_label(rule), "description": describe_scope(rule),
                    "feature": row["feature"], "effect_size": row["effect_size"], "p_value": row["p_value"],
                    "q_value": row["q_value"], "n_losses": row["n_losses"], "n_wins": row["n_wins"],
                    "why": (f"losses averaged {row['mean_losses']:g} vs wins {row['mean_wins']:g} "
                            f"(d = {row['effect_size']:+.2f}, q = {row['q_value']:.3f})")})
    out.sort(key=lambda item: (-abs(item["effect_size"] or 0.0), item["feature"]))
    return out[:limit]


# ------------------------------------------------------------------ autopsy

def autopsy(strategy_type: str, trades: list[dict], *, now: datetime, permutations: int = PERMUTATIONS,
            seed: int = 0) -> dict:
    """One strategy's autopsy payload from its trade feature records.  Pure."""
    table = contrast(trades, permutations=permutations, seed=seed)
    losses = [t for t in trades if t["is_loss"]]
    return {
        "version": VERSION, "strategy_type": strategy_type, "as_of": _iso(now), "trades": len(trades),
        "losses": len(losses), "wins": len(trades) - len(losses),
        "signal_wrong_losses": sum(1 for t in losses if t.get("cause") == TRIGGER),
        "first_closed_at": trades[0]["closed_at"] if trades else None,
        "last_closed_at": trades[-1]["closed_at"] if trades else None,
        "features": table, "candidates": candidates(table),
        "trade_records": [{"symbol": t["symbol"], "entry_session": t["entry_session"], "is_loss": t["is_loss"],
                           "return": t["return"], "cause": t["cause"],
                           "decision_bar": t["features"].get("decision_bar")} for t in trades],
        "method": METHOD, "min_side": MIN_SIDE, "max_q": MAX_Q, "permutations": int(permutations),
        "hindsight_only": True, "paper_only": True,
    }


def record_autopsies(db: Session, *, now: Optional[datetime] = None, min_trades: int = MIN_TRADES,
                     permutations: int = PERMUTATIONS, seed: int = 0) -> dict:
    """Weekly: one ``signal_autopsy`` audit row per strategy with at least ``min_trades`` closed paper trades."""
    from app.services.trade_attribution import current_attributions

    now = now or datetime.now(UTC)
    by_strategy: dict[str, list] = defaultdict(list)
    for row in current_attributions(db, sources=SOURCES):
        by_strategy[row.strategy_type].append(row)
    strategy_ids = {row.strategy_type: row.id for row in db.query(Strategy).order_by(Strategy.id).all()}
    recorded, skipped = [], []
    for strategy_type, rows in sorted(by_strategy.items()):
        if len(rows) < min_trades:
            skipped.append({"strategy_type": strategy_type, "trades": len(rows),
                            "reason": f"fewer than {min_trades} closed trades"})
            continue
        payload = autopsy(strategy_type, trade_features(db, rows), now=now, permutations=permutations, seed=seed)
        payload["strategy_id"] = strategy_ids.get(strategy_type)
        labels = [c["label"] for c in payload["candidates"]]
        write_audit_log(
            db, event_type=EVENT, entity_type="strategy", entity_id=strategy_ids.get(strategy_type), action=ACTION,
            status="complete",
            message=(f"signal autopsy {strategy_type}: {payload['losses']} losses vs {payload['wins']} wins "
                     f"({payload['signal_wrong_losses']} signal_wrong); scope candidates: "
                     + (", ".join(labels) if labels else "none")),
            payload=_clean(payload),
        )
        recorded.append({"strategy_type": strategy_type, "trades": len(rows), "losses": payload["losses"],
                         "signal_wrong_losses": payload["signal_wrong_losses"], "candidates": labels})
    return {"status": "complete", "as_of": _iso(now), "recorded": recorded, "skipped": skipped,
            "min_trades": int(min_trades), "paper_only": True}


def latest_autopsies(db: Session) -> dict[str, dict]:
    """The newest autopsy payload per strategy type."""
    out: dict[str, dict] = {}
    rows = db.query(AuditLog).filter(AuditLog.event_type == EVENT, AuditLog.action == ACTION).order_by(
        AuditLog.id.desc()).all()
    for row in rows:
        payload = row.payload or {}
        name = payload.get("strategy_type")
        if name and name not in out:
            out[name] = payload
    return out


def latest_autopsy(db: Session, strategy_type: str) -> Optional[dict]:
    return latest_autopsies(db).get(strategy_type)


# ------------------------------------------------------- the engine's feed

def scope_candidates(strategy: Any) -> tuple:
    """The ``scope`` template's candidates for a strategy row, from its latest autopsy.

    ``{"scope": None}`` (the scope turned off) is always first, so a scope
    that costs profit can be removed by the same rule that added it.  A row
    without an autopsy, or one outside a session (a test double), has no
    candidates; a live scope with no fresh candidates is still tested against
    off.
    """
    try:
        session = object_session(strategy) if strategy is not None else None
    except UnmappedInstanceError:  # a test double, not a row
        session = None
    live = scope_condition(getattr(strategy, "parameters", None))
    found: list[dict] = []
    if session is not None:
        payload = latest_autopsy(session, getattr(strategy, "strategy_type", None) or "")
        for item in (payload or {}).get("candidates") or []:
            rule = scope_condition({SCOPE_KEY: item.get("rule")})
            if rule is not None and rule != live and rule not in found:
                found.append(rule)
    if not found and live is None:
        return ()
    return ({SCOPE_KEY: None}, *({SCOPE_KEY: rule} for rule in found))


def scope_decided_by_engine(db: Session, strategy_id: int) -> bool:
    """Has the experiment engine written a ``scope_update`` for this row?  A seed step must not override one."""
    row = db.query(AuditLog.id).filter(
        AuditLog.event_type == GOVERNANCE_EVENT, AuditLog.action == SCOPE_ACTION, AuditLog.entity_type == "strategy",
        AuditLog.entity_id == strategy_id).order_by(AuditLog.id.desc()).first()
    return row is not None


# -------------------------------------------------------------- projections

def _compact(payload: Mapping) -> dict:
    return {key: payload.get(key) for key in ("strategy_type", "strategy_id", "as_of", "trades", "losses", "wins",
                                              "signal_wrong_losses", "first_closed_at", "last_closed_at",
                                              "features", "candidates")}


def signal_autopsy_view(db: Session) -> dict:
    """The per-strategy tables for ``GET /stock-paper/signal-autopsy``."""
    latest = latest_autopsies(db)
    strategies = [_compact(payload) for _, payload in sorted(latest.items())]
    live = {row.strategy_type: scope_condition(row.parameters) for row in db.query(Strategy).order_by(Strategy.id).all()}
    return {
        "as_of": max((p.get("as_of") or "" for p in latest.values()), default=None) or None,
        "strategies": strategies,
        "live_scopes": {name: {"condition": condition, "description": describe_scope(condition)}
                        for name, condition in live.items() if condition},
        "features": list(FEATURES), "scope_features": list(RULE_FEATURES), "min_trades": MIN_TRADES,
        "min_side": MIN_SIDE, "max_q": MAX_Q, "permutations": PERMUTATIONS, "method": METHOD,
        "how_it_feeds_the_engine": (
            "The experiment engine's scope template, triggered by a repeating signal_wrong lesson, replays each "
            "candidate (and scope off) under its pre-registered rule; a candidate that keeps fewer than 30 trades "
            "in any window fails. An adopted scope is written to the strategy row as parameters['scope'] through "
            "the scope_update governance action and turns a BUY into a HOLD when the decision bar fails it."),
        "paper_only": True,
    }
