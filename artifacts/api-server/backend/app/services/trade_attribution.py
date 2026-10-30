"""Append-only attribution of every closed paper trade.

Two sources are attributed:

* ``trial_lot`` - each fully closed forward-trial lot (governed ML trials).
* ``scheduled_round_trip`` / ``exploration_round_trip`` - each closed
  autotrader round trip rebuilt by the paper-trade scorecard, so losses can
  be tied to the indicator strategy that opened them.

Each record explains the trade: signal quality at entry, slippage against
the decision (reserved reference) price, holding time against the exit rule,
point-in-time regime tags (trend / volatility bucket from daily closes
strictly before the entry date), the exit cause and the net return.

Safety contract:
* Records are hindsight evaluation.  They feed the nightly research agenda,
  meta-label mode reviews and paper allocation weights, all of which act only
  on decisions dated after the outcome was observed.
* Unknown broker costs stay unknown (``net_return`` is NULL); they are never
  treated as zero.
* Rows are immutable.  A trade whose evidence changes (costs become known,
  a late fill) is recorded as a new revision of the same ``source_key``.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import MarketPrice, StockPaperTrial, StockPaperTrialDecision, Strategy, StrategySignal
from app.models.loss_feedback import StockTradeAttribution
from app.models.stock_paper import StockPaperOrder, StockPaperTrialLot

UTC = timezone.utc
ATTRIBUTION_VERSION = "trade-attribution-v1"
EXIT_CAUSES = ("indicator", "time_stop", "catastrophe_stop", "protection_lock", "take_profit", "operator", "other")
SOURCES = ("trial_lot", "scheduled_round_trip", "exploration_round_trip")

_TIME_REASONS = {"horizon", "trial_window_complete", "paper_session_expired", "time_stop", "max_holding"}
_STOP_REASONS = {"stop", "stop_loss_leg", "protective_stop", "catastrophe_stop"}
_PROTECTION_MARKERS = ("loss", "drawdown", "kill", "protect", "halt", "recovery", "lock", "pause")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _f(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _round(value: Optional[float], digits: int = 8) -> Optional[float]:
    return None if value is None else round(value, digits)


# ----------------------------------------------------------------- pure helpers

def exit_cause(raw_reason: Optional[str], *, held_sessions: Optional[int] = None,
               max_holding_sessions: Optional[int] = None) -> str:
    """Map a recorded exit reason onto the attribution exit-cause vocabulary."""
    reason = str(raw_reason or "").strip().lower()
    if reason in _STOP_REASONS:
        return "catastrophe_stop"
    if reason in _TIME_REASONS:
        return "time_stop"
    if reason in {"take_profit_leg", "take_profit"}:
        return "take_profit"
    if reason in {"sell_signal", "indicator"}:
        # A strategy SELL and an exit-policy indicator exit share one path;
        # a lot held to its declared session cap was closed by the time stop.
        if max_holding_sessions and held_sessions is not None and held_sessions >= max_holding_sessions:
            return "time_stop"
        return "indicator"
    if reason in {"operator_stop", "manual"}:
        return "operator"
    if any(marker in reason for marker in _PROTECTION_MARKERS):
        return "protection_lock"
    return "other"


def held_sessions(opened_at: datetime, closed_at: datetime) -> int:
    """NYSE regular sessions after the entry session up to the exit session."""
    from app.services.exchange_sessions import is_nyse_session
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    start = _utc(opened_at).astimezone(et).date()
    end = _utc(closed_at).astimezone(et).date()
    count, day = 0, start
    while day < end and count < 400:
        day += timedelta(days=1)
        if is_nyse_session(day):
            count += 1
    return count


def holding_relation(sessions: Optional[int], rule_sessions: Optional[int]) -> str:
    if not rule_sessions:
        return "no_time_rule"
    if sessions is None:
        return "unknown"
    if sessions < rule_sessions:
        return "before_rule"
    return "at_rule" if sessions == rule_sessions else "beyond_rule"


def slippage_bps(fill_price: Optional[float], reference: Optional[float], side: str) -> Optional[float]:
    """Adverse slippage in basis points (positive = worse than the decision price)."""
    if fill_price is None or not reference or reference <= 0:
        return None
    raw = (fill_price - reference) / reference * 10_000
    return round(raw if side == "buy" else -raw, 4)


def signal_quality(*, probability: Optional[float] = None, threshold: Optional[float] = None,
                   confidence: Optional[float] = None) -> dict:
    """Bucket the entry signal: strong / moderate / marginal / unknown."""
    margin = None
    if probability is not None:
        margin = probability - (threshold if threshold is not None else 0.5)
    if margin is not None:
        bucket = "strong" if margin >= 0.10 else "moderate" if margin >= 0.03 else "marginal"
    elif confidence is not None:
        bucket = "strong" if confidence >= 0.70 else "moderate" if confidence >= 0.55 else "marginal"
    else:
        bucket = "unknown"
    return {"probability": _round(probability, 6), "threshold": threshold, "confidence": _round(confidence, 6),
            "margin": _round(margin, 6), "bucket": bucket}


def regime_tags(closes: list[float]) -> dict:
    """Trend and volatility bucket from daily closes strictly before entry."""
    closes = [value for value in closes if value and value > 0]
    if len(closes) < 21:
        return {"trend": "unknown", "volatility": "unknown", "annualized_volatility": None, "bars": len(closes)}
    window = 50 if len(closes) >= 50 else 20
    sma = sum(closes[-window:]) / window
    trend = "up" if closes[-1] >= sma else "down"
    returns = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - 20, len(closes))]
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / max(len(returns) - 1, 1)
    vol = math.sqrt(variance) * math.sqrt(252)
    bucket = "low" if vol < 0.15 else "normal" if vol < 0.30 else "high"
    return {"trend": trend, "trend_sma_window": window, "volatility": bucket,
            "annualized_volatility": round(vol, 6), "bars": len(closes)}


def digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


# ----------------------------------------------------------------- data access

def _closes_before(db: Session, symbol: str, day: date, *, limit: int = 60) -> list[float]:
    rows = db.execute(
        select(MarketPrice.price_date, MarketPrice.close, MarketPrice.adjusted_close)
        .where(MarketPrice.symbol == symbol, MarketPrice.price_date < day)
        .order_by(MarketPrice.price_date.desc(), MarketPrice.id)
        .limit(limit * 3)
    ).all()
    by_day: dict[date, float] = {}
    for price_date, close, adjusted in rows:
        value = _f(adjusted) or _f(close)
        if value and price_date not in by_day:
            by_day[price_date] = value
    return [by_day[key] for key in sorted(by_day)][-limit:]


def _regime(db: Session, symbol: str, opened_at: datetime, cache: dict) -> dict:
    from zoneinfo import ZoneInfo

    day = _utc(opened_at).astimezone(ZoneInfo("America/New_York")).date()
    key = (symbol, day)
    if key not in cache:
        cache[key] = regime_tags(_closes_before(db, symbol, day)) | {"as_of_before": day.isoformat()}
    return cache[key]


def _persist(db: Session, *, source: str, source_key: str, record: dict) -> Optional[StockTradeAttribution]:
    """Insert a new revision only when the attribution content changed."""
    sha = digest(record)
    latest = db.scalar(select(StockTradeAttribution).where(StockTradeAttribution.source_key == source_key)
                       .order_by(StockTradeAttribution.revision.desc()))
    if latest is not None and latest.attribution_sha256 == sha:
        return None
    returns = record["returns"]
    row = StockTradeAttribution(
        source=source, source_key=source_key, revision=(latest.revision + 1) if latest else 1,
        attribution_sha256=sha, strategy_id=record.get("strategy_id"), strategy_type=record["strategy_type"],
        trial_id=record.get("trial_id"), symbol=record["symbol"],
        opened_at=datetime.fromisoformat(record["opened_at"]), closed_at=datetime.fromisoformat(record["closed_at"]),
        exit_cause=record["exit"]["cause"],
        net_return=Decimal(str(returns["net_return"])) if returns["net_return"] is not None else None,
        gross_return=Decimal(str(returns["gross_return"])),
        net_pnl=Decimal(str(returns["net_pnl"])) if returns["net_pnl"] is not None else None,
        costs_known=bool(returns["costs_known"]), is_loss=bool(returns["is_loss"]),
        detail=record, paper_only=True,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        return None  # a concurrent writer recorded the same revision
    return row


def _record(**values) -> dict:
    gross_return = values.pop("gross_return")
    net_return = values.pop("net_return")
    net_pnl = values.pop("net_pnl")
    costs_known = values.pop("costs_known")
    effective = net_return if net_return is not None else gross_return
    return {
        "version": ATTRIBUTION_VERSION,
        **values,
        "returns": {
            "gross_return": _round(gross_return), "net_return": _round(net_return),
            "net_pnl": _round(net_pnl), "fees": values.get("fees"), "costs_known": costs_known,
            "is_loss": effective <= 0,
        },
        "paper_only": True,
        "hindsight_only": True,
    }


# ----------------------------------------------------------------- trial lots

def attribute_trial(db: Session, trial: StockPaperTrial, *, regime_cache: Optional[dict] = None) -> list[StockTradeAttribution]:
    """Attribute every fully closed lot of one forward trial (idempotent)."""
    from app.services.trade_mistakes import classify_trade, trial_lot_trades

    cache = regime_cache if regime_cache is not None else {}
    strategy = db.get(Strategy, trial.strategy_id) if trial.strategy_id else None
    strategy_type = strategy.strategy_type if strategy else "stock_forward_trial"
    created = []
    for trade in trial_lot_trades(db, trial):
        lot = db.get(StockPaperTrialLot, trade["lot_id"])
        entry_order = db.get(StockPaperOrder, lot.entry_order_id) if lot and lot.entry_order_id else None
        decision = db.get(StockPaperTrialDecision, trade["entry_decision_id"])
        lineage = (decision.lineage if decision else None) or {}
        opened_at = _utc(datetime.fromisoformat(trade["entered_at"]))
        closed_at = _utc(datetime.fromisoformat(trade["exited_at"]))
        sessions = held_sessions(opened_at, closed_at)
        entry_price, exit_price = trade["entry_price"], trade["exit_price"]
        fees = trade["fees"]
        quantity = trade["quantity"]
        cost = entry_price * quantity
        gross_pnl = (exit_price - entry_price) * quantity
        net_pnl = gross_pnl - fees if fees is not None else None
        threshold = _f(lineage.get("threshold")) or _f((trial.policy or {}).get("probability_threshold")) or 0.5
        classification = trade.get("classification") or classify_trade(trade)
        record = _record(
            strategy_id=trial.strategy_id, strategy_type=strategy_type, trial_id=trial.id,
            symbol=trade["symbol"], opened_at=opened_at.isoformat(), closed_at=closed_at.isoformat(),
            quantity=quantity, fees=fees,
            signal_quality=signal_quality(probability=trade.get("probability"), threshold=threshold),
            slippage={
                "decision_price": _f(entry_order.limit_price) if entry_order else None,
                "entry_price": entry_price,
                "entry_bps": slippage_bps(entry_price, _f(entry_order.limit_price) if entry_order else None, "buy"),
                "exit_reference_price": _f(lot.exit_reference_price) if lot else None,
                "exit_price": exit_price,
                "exit_bps": slippage_bps(exit_price, _f(lot.exit_reference_price) if lot else None, "sell"),
            },
            holding={"sessions": sessions, "minutes": round((closed_at - opened_at).total_seconds() / 60, 2),
                     "rule_sessions": trade.get("planned_horizon_sessions"),
                     "relation": holding_relation(sessions, trade.get("planned_horizon_sessions"))},
            regime=_regime(db, trade["symbol"], opened_at, cache),
            exit={"cause": exit_cause(trade.get("exit_reason")), "raw_reason": trade.get("exit_reason")},
            mistakes={"primary": classification.get("primary_mistake"), "all": classification.get("mistakes") or []},
            lineage={"lot_id": trade["lot_id"], "entry_decision_id": trade["entry_decision_id"],
                     "entry_order_id": lot.entry_order_id if lot else None},
            gross_return=gross_pnl / cost if cost > 0 else 0.0,
            net_return=(net_pnl / cost) if net_pnl is not None and cost > 0 else None,
            net_pnl=net_pnl, costs_known=fees is not None,
        )
        row = _persist(db, source="trial_lot", source_key=f"trial_lot:{trade['lot_id']}", record=record)
        if row is not None:
            created.append(row)
    return created


def attribute_trials(db: Session, *, since: Optional[datetime] = None) -> list[StockTradeAttribution]:
    statement = select(StockPaperTrial).order_by(StockPaperTrial.created_at)
    if since is not None:
        statement = statement.where(StockPaperTrial.created_at >= since - timedelta(days=60))
    cache: dict = {}
    created = []
    for trial in db.scalars(statement).all():
        created.extend(attribute_trial(db, trial, regime_cache=cache))
    return created


# ----------------------------------------------------------------- autotrader round trips

def attribute_round_trips(db: Session, *, account_id: Optional[int] = None,
                          since: Optional[datetime] = None) -> list[StockTradeAttribution]:
    """Attribute closed scheduled / exploration round trips (idempotent)."""
    from app.services import paper_trade_scorecard as scorecard
    from app.services.trade_mistakes import classify_round_trip

    account = scorecard._account(db, account_id)
    if account is None:
        return []
    rebuilt = scorecard.reconstruct_round_trips(db, account.id)
    strategies = rebuilt["strategies"]
    orders = rebuilt["orders"]
    curve = scorecard._EquityCurve([])
    lots = [lot for lot in rebuilt["lots"] if lot.closed and lot.entry_order.id and lot.entry_order.id > 0
            and lot.source in {"scheduled", "exploration"}]
    signal_ids = {lot.entry_order.signal_id for lot in lots if lot.entry_order.signal_id}
    signals = {row.id: row for row in db.query(StrategySignal).filter(StrategySignal.id.in_(signal_ids)).all()} \
        if signal_ids else {}
    cache: dict = {}
    created = []
    for lot in lots:
        closed_at = max(item["filled_at"] for item in lot.exits)
        if since is not None and closed_at < since:
            continue
        signal = signals.get(lot.entry_order.signal_id)
        probability = _f(signal.probability_up) if signal else None
        trade = scorecard._trade_row(lot, strategies, curve, probability)
        strategy = strategies.get(lot.strategy_id)
        strategy_type = (strategy.strategy_type if strategy else None) or "unattributed"
        parameters = (strategy.parameters or {}) if strategy else {}
        max_hold = int(parameters["max_holding_sessions"]) if _f(parameters.get("max_holding_sessions")) else None
        opened_at = _utc(lot.entered_at)
        sessions = held_sessions(opened_at, closed_at)
        exit_ids = sorted({item["order_id"] for item in lot.exits if item["order_id"]})
        exit_refs = []
        for order_id in exit_ids:
            order = orders.get(order_id)
            if order is not None:
                exit_refs.append(_f(order.stop_price) if order.leg_role == "stop_loss" and order.stop_price
                                 else _f(order.limit_price))
        exit_refs = [value for value in exit_refs if value]
        exit_reference = sum(exit_refs) / len(exit_refs) if exit_refs else None
        decision_price = _f(lot.entry_order.limit_price)
        features = (signal.features or {}) if signal else {}
        meta = features.get("meta_label") if isinstance(features.get("meta_label"), dict) else None
        classification = classify_round_trip(db, trade) if trade["net_pnl"] <= 0 else {"primary_mistake": None, "mistakes": []}
        costs_known = bool(trade["costs_known"])
        cost = float(lot.cost)
        record = _record(
            strategy_id=lot.strategy_id, strategy_type=strategy_type, trial_id=None,
            symbol=lot.symbol, opened_at=opened_at.isoformat(), closed_at=closed_at.isoformat(),
            quantity=trade["quantity"], fees=trade["fees"] if costs_known else None,
            signal_quality=signal_quality(confidence=_f(signal.confidence) if signal else None)
            | {"probability_up": _round(probability, 6)},
            slippage={
                "decision_price": decision_price, "entry_price": trade["entry_price"],
                "entry_bps": slippage_bps(trade["entry_price"], decision_price, "buy"),
                "exit_reference_price": _round(exit_reference, 6), "exit_price": trade["exit_price"],
                "exit_bps": slippage_bps(trade["exit_price"], exit_reference, "sell"),
            },
            holding={"sessions": sessions, "minutes": trade["holding_minutes"], "rule_sessions": max_hold,
                     "relation": holding_relation(sessions, max_hold)},
            regime=_regime(db, lot.symbol, opened_at, cache),
            exit={"cause": exit_cause(trade["exit_reason"], held_sessions=sessions, max_holding_sessions=max_hold),
                  "raw_reason": trade["exit_reason"], "reasons_by_quantity": trade["exit_reasons"]},
            meta_label=({"decision": meta.get("decision"), "probability": meta.get("probability"),
                         "mode": meta.get("mode"), "status": meta.get("status")} if meta else None),
            mistakes={"primary": classification.get("primary_mistake"), "all": classification.get("mistakes") or []},
            lineage={"entry_order_id": lot.entry_order.id, "exit_order_ids": exit_ids,
                     "signal_id": lot.entry_order.signal_id, "order_source": lot.source},
            gross_return=trade["gross_pnl"] / cost if cost > 0 else 0.0,
            net_return=trade["net_return"] if costs_known else None,
            net_pnl=trade["net_pnl"] if costs_known else None, costs_known=costs_known,
        )
        row = _persist(db, source=f"{lot.source}_round_trip", source_key=f"round_trip:{lot.entry_order.id}",
                       record=record)
        if row is not None:
            created.append(row)
    return created


def run_trade_attribution(db: Session, *, now: Optional[datetime] = None, lookback_days: int = 120) -> dict:
    now = now or datetime.now(UTC)
    since = now - timedelta(days=lookback_days)
    trial_rows = attribute_trials(db, since=since)
    trip_rows = attribute_round_trips(db, since=since)
    return {"trial_lots_recorded": len(trial_rows), "round_trips_recorded": len(trip_rows)}


# ----------------------------------------------------------------- projections

def current_attributions(db: Session, *, since: Optional[datetime] = None,
                         sources: Optional[Iterable[str]] = None,
                         strategy_types: Optional[Iterable[str]] = None,
                         trial_ids: Optional[Iterable[str]] = None) -> list[StockTradeAttribution]:
    """Latest revision per trade, newest close first."""
    latest = (select(StockTradeAttribution.source_key, func.max(StockTradeAttribution.revision).label("revision"))
              .group_by(StockTradeAttribution.source_key).subquery())
    statement = select(StockTradeAttribution).join(
        latest, (StockTradeAttribution.source_key == latest.c.source_key)
        & (StockTradeAttribution.revision == latest.c.revision))
    if since is not None:
        statement = statement.where(StockTradeAttribution.closed_at >= since)
    if sources is not None:
        statement = statement.where(StockTradeAttribution.source.in_(list(sources)))
    if strategy_types is not None:
        statement = statement.where(StockTradeAttribution.strategy_type.in_(list(strategy_types)))
    if trial_ids is not None:
        statement = statement.where(StockTradeAttribution.trial_id.in_(list(trial_ids)))
    return list(db.scalars(statement.order_by(StockTradeAttribution.closed_at.desc(), StockTradeAttribution.id.desc())).all())


def _effective_return(row: StockTradeAttribution) -> float:
    return float(row.net_return if row.net_return is not None else row.gross_return)


def _breakdown(rows: list[StockTradeAttribution], key) -> list[dict]:
    groups: dict[str, list] = defaultdict(list)
    for row in rows:
        groups[key(row)].append(row)
    out = []
    for name, members in groups.items():
        losses = [row for row in members if row.is_loss]
        out.append({
            "key": name,
            "trades": len(members),
            "losses": len(losses),
            "loss_rate": round(len(losses) / len(members), 6),
            "sum_return": round(sum(_effective_return(row) for row in members), 8),
            "loss_return": round(sum(_effective_return(row) for row in losses), 8),
            "net_pnl_known": round(sum(float(row.net_pnl) for row in members if row.net_pnl is not None), 4),
            "costs_unknown": sum(1 for row in members if not row.costs_known),
        })
    out.sort(key=lambda item: (item["loss_return"], -item["losses"], item["key"]))
    return out


def attribution_row_out(row: StockTradeAttribution) -> dict:
    detail = row.detail or {}
    return {
        "id": row.id, "source": row.source, "source_key": row.source_key, "revision": row.revision,
        "strategy_id": row.strategy_id, "strategy_type": row.strategy_type, "trial_id": row.trial_id,
        "symbol": row.symbol, "opened_at": _utc(row.opened_at).isoformat(), "closed_at": _utc(row.closed_at).isoformat(),
        "exit_cause": row.exit_cause, "net_return": float(row.net_return) if row.net_return is not None else None,
        "gross_return": float(row.gross_return), "costs_known": row.costs_known, "is_loss": row.is_loss,
        "signal_quality": detail.get("signal_quality"), "slippage": detail.get("slippage"),
        "holding": detail.get("holding"), "regime": detail.get("regime"), "exit": detail.get("exit"),
        "meta_label": detail.get("meta_label"), "mistakes": detail.get("mistakes"),
        "attribution_sha256": row.attribution_sha256,
    }


def attribution_summary(rows: list[StockTradeAttribution]) -> dict:
    """Compact, deterministic loss breakdown used in reports and the API."""
    losses = [row for row in rows if row.is_loss]

    def regime_key(row):
        regime = (row.detail or {}).get("regime") or {}
        return f"{regime.get('trend', 'unknown')}/{regime.get('volatility', 'unknown')}"

    def quality_key(row):
        return ((row.detail or {}).get("signal_quality") or {}).get("bucket", "unknown")

    def holding_key(row):
        return ((row.detail or {}).get("holding") or {}).get("relation", "unknown")

    return {
        "version": ATTRIBUTION_VERSION,
        "trades": len(rows),
        "losses": len(losses),
        "loss_rate": round(len(losses) / len(rows), 6) if rows else None,
        "sum_return": round(sum(_effective_return(row) for row in rows), 8),
        "loss_return": round(sum(_effective_return(row) for row in losses), 8),
        "costs_unknown": sum(1 for row in rows if not row.costs_known),
        "by_strategy": _breakdown(rows, lambda row: row.strategy_type),
        "by_symbol": _breakdown(rows, lambda row: row.symbol),
        "by_exit_cause": _breakdown(rows, lambda row: row.exit_cause),
        "by_regime": _breakdown(rows, regime_key),
        "by_signal_quality": _breakdown(rows, quality_key),
        "by_holding": _breakdown(rows, holding_key),
        "primary_mistakes": dict(Counter(
            ((row.detail or {}).get("mistakes") or {}).get("primary") for row in losses
            if ((row.detail or {}).get("mistakes") or {}).get("primary")
        )),
        "attribution_ids": sorted(row.id for row in rows)[-200:],
    }


def attribution_projection(db: Session, *, days: int = 30, now: Optional[datetime] = None,
                           strategy_type: Optional[str] = None, symbol: Optional[str] = None,
                           exit_cause_filter: Optional[str] = None, limit: int = 100) -> dict:
    now = now or datetime.now(UTC)
    rows = current_attributions(db, since=now - timedelta(days=max(1, days)),
                                strategy_types=[strategy_type] if strategy_type else None)
    if symbol:
        rows = [row for row in rows if row.symbol == symbol.strip().upper()]
    if exit_cause_filter:
        rows = [row for row in rows if row.exit_cause == exit_cause_filter]
    summary = attribution_summary(rows)
    summary.pop("attribution_ids", None)
    return {
        "as_of": now.isoformat(), "window_days": days,
        "summary": summary,
        "recent_losses": [attribution_row_out(row) for row in rows if row.is_loss][: max(1, min(limit, 500))],
        "exit_causes": list(EXIT_CAUSES),
        "paper_only": True, "live_authorized": False, "hindsight_only": True,
    }
