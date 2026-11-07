"""Exploration budget: spend the learning-sized lots where evidence is scarcest.

Learning-sized trades (0.25% of equity with the one-share floor) come from
candidate and research strategies (``paper_trading_candidate`` rows, the
discovered ``rule_*`` strategies) and from news-found symbols.  Until now
they went wherever a signal fired, so the forward verdicts of the
experiment engine, the setup shadow and the regime records all waited on
whichever strategy happened to signal most.  This module makes the bot
choose:

1. **Evidence map** (``evidence_map``).  One item per thing the bot is
   trying to learn about, each with the forward trades so far, the trades
   still needed to reach its evidence minimum and the Wilson half-width of
   its win-rate estimate:

   * ``strategy``: closed paper round trips per learning strategy
     (``trade_attribution``; minimum ``MIN_TRADES_FOR_EVIDENCE``, the
     scorecard's bar);
   * ``regime``: the same round trips per regime cell (``regime_allocation``;
     minimum ``paper_allocation_min_trades``, the record's own bar);
   * ``shadow``: setup-shadow decisions per candidate gate value
     (``setup_shadow``; minimum ``experiment_verdict_min_trades``);
   * ``experiment``: closed trades since each watched change was adopted
     (``experiment_engine``; minimum the change's ``watch.min_trades``);
   * ``news``: closed round trips on news-found symbols.

2. **Budget** (``plan_exploration``).  Each session may spend
   ``exploration_budget_fraction`` of equity (default 1%) on learning-sized
   lots.  Every eligible learning BUY of a cycle gets a priority: the trades
   its linked items still need, weighted by the candidate's backtest
   expectancy and discounted by that strategy's lots already in flight.
   Lots are allotted greedily in priority order; the rest are skipped with
   the reason ``exploration_budget`` in the BUY outcome.  Scheduled
   (full-size) entries, exits and every cap are untouched.

3. **Lift** (``lift_multiplier`` / ``apply_lift``).  An item far from its
   minimum (fewer than half the trades) with a positive backtest expectancy
   may trade up to 2x the learning size so its evidence arrives sooner:
   still under every ledger cap and never above the scheduled size.  The
   multiplier and its reason are recorded in the signal's features
   (``features.exploration``) and the ledger admits the lifted lot only
   when the signal carries that record.

4. **Surface**: ``exploration_budget_view`` for
   ``GET /stock-paper/exploration-budget`` and ``scoreboard_exploration``
   for the learning scoreboard ("trades until the next verdict").

Paper only.  Nothing here touches live trading; the plan is audit rows.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Callable, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.stock_paper_ledger import EXPLORATION_SOURCE, EXPLORATION_STRATEGY_STATUSES

logger = logging.getLogger(__name__)

UTC = timezone.utc
ET = ZoneInfo("America/New_York")
EVENT = "stock_paper"
ACTION = "exploration_budget"
SKIP_REASON = "exploration_budget"
SIGNAL_FEATURE = "exploration"
LEARNING_SOURCES = ("exploration_round_trip", "scheduled_round_trip")
# The scorecard's evidence bar (paper_trade_scorecard.MIN_TRADES_FOR_EVIDENCE).
STRATEGY_MIN_TRADES = 30
NEWS_MIN_TRADES = 30
Z = 1.96
MAX_LIFT = 2.0
IN_FLIGHT_DAYS = 45  # longer than the longest tested hold (20 sessions)
DEAD_ORDER_STATUSES = frozenset({"cancelled", "canceled", "rejected", "expired", "unknown"})
QUANTITY_INCREMENT = Decimal("0.000001")
LIFT_FAR = "far from its evidence minimum with a positive backtest expectancy"
LIFT_NEAR = "within half of its evidence minimum; learning size"
LIFT_NO_EDGE = "no positive backtest expectancy on record; learning size"
METHOD = ("priority = trades still needed by the entry's items (strategy, current regime cell, watched changes, news) "
          "x (1 + backtest expectancy / 100 bps, bounded to [0.5, 2]) / (1 + lots of that strategy in flight); "
          "lots are allotted in priority order until the session budget is spent.")


# ------------------------------------------------------------------ helpers

def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _iso(value: Any) -> Optional[str]:
    return _utc(value).isoformat() if isinstance(value, datetime) else (str(value) if value is not None else None)


def _parse(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return _utc(value)
    try:
        return _utc(datetime.fromisoformat(str(value)))
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def wilson(wins: int, trades: int, *, z: float = Z) -> dict:
    """Wilson score interval of a win rate: ``{"win_rate", "low", "high", "half_width"}`` (None below one trade)."""
    n = int(trades)
    if n <= 0:
        return {"win_rate": None, "low": None, "high": None, "half_width": None}
    p = min(max(int(wins) / n, 0.0), 1.0)
    z2 = z * z
    centre = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    return {"win_rate": round(p, 4), "low": round(max(0.0, centre - half), 4),
            "high": round(min(1.0, centre + half), 4), "half_width": round(half, 4)}


def backtest_expectancy_bps(parameters: Optional[Mapping]) -> Optional[float]:
    """The candidate's backtest expectancy (bps per trade) from its strategy parameters, if any.

    Discovered rules carry their holdout and discovery statistics under
    ``parameters.discovery``; a seeded candidate may carry
    ``parameters.backtest.expectancy_bps`` or ``parameters.expectancy_bps``.
    """
    parameters = parameters or {}
    discovery = parameters.get("discovery") if isinstance(parameters.get("discovery"), Mapping) else {}
    holdout = discovery.get("holdout") if isinstance(discovery.get("holdout"), Mapping) else {}
    inner = discovery.get("discovery") if isinstance(discovery.get("discovery"), Mapping) else {}
    net = inner.get("net") if isinstance(inner.get("net"), Mapping) else {}
    backtest = parameters.get("backtest") if isinstance(parameters.get("backtest"), Mapping) else {}
    for value in (holdout.get("expectancy_bps"), net.get("expectancy_bps"),
                  discovery.get("post_period_expectancy_bps"), backtest.get("expectancy_bps"),
                  parameters.get("expectancy_bps")):
        found = _float(value)
        if found is not None:
            return found
    return None


def _window_expectancy(rows: Any) -> Optional[float]:
    """``expectancy_bps`` of a replay row, or of its ``all`` window when the row is per window."""
    if not isinstance(rows, Mapping):
        return None
    if "expectancy_bps" in rows:
        return _float(rows.get("expectancy_bps"))
    window = rows.get("all")
    return _float(window.get("expectancy_bps")) if isinstance(window, Mapping) else None


def make_item(kind: str, key: str, label: str, *, strategy_type: Optional[str], trades: int, wins: int,
              minimum: int, expectancy_bps: Optional[float], **extra) -> dict:
    trades, minimum = int(trades), int(minimum)
    return {"kind": kind, "key": key, "label": label, "strategy_type": strategy_type, "trades": trades,
            "wins": int(wins), "minimum": minimum, "needed": max(minimum - trades, 0),
            "expectancy_bps": expectancy_bps, **wilson(wins, trades), **extra}


def _guarded(db: Session, loader: Callable[[], Any], default: Any, what: str) -> Any:
    """Run one evidence read inside a savepoint; an unreadable source degrades to ``default``."""
    try:
        try:
            nested = db.begin_nested()
        except Exception:  # a test double without savepoints
            return loader()
        with nested:
            return loader()
    except Exception as exc:
        logger.info("exploration budget: %s unavailable: %s", what, exc.__class__.__name__)
        return default


# ------------------------------------------------------------- evidence map

def learning_strategies(db: Session, strategies: Optional[Iterable] = None) -> list:
    """Active candidate / research strategy rows (one per type): the ones that trade learning-sized."""
    if strategies is None:
        from app.models import Strategy

        strategies = db.query(Strategy).filter(Strategy.is_active.is_(True)).order_by(Strategy.id).all()
    out, seen = [], set()
    for row in strategies:
        status = getattr(row, "current_status", None)
        if status in EXPLORATION_STRATEGY_STATUSES and row.strategy_type not in seen:
            seen.add(row.strategy_type)
            out.append(row)
    return out


def strategy_items(db: Session, strategies: Iterable, *, current_cell: Optional[str] = None) -> list[dict]:
    """``strategy`` and ``regime`` items from the closed paper round trips of each learning strategy."""
    from app.services.regime_allocation import cell_for_attribution
    from app.services.trade_attribution import current_attributions

    minimum_cell = int(settings.paper_allocation_min_trades)
    cache: dict = {}
    out = []
    for row in strategies:
        strategy_type = row.strategy_type
        expectancy = backtest_expectancy_bps(getattr(row, "parameters", None))
        trades = [t for t in current_attributions(db, sources=LEARNING_SOURCES, strategy_types=[strategy_type])
                  if t.closed_at is not None]
        wins = sum(1 for t in trades if not t.is_loss)
        out.append(make_item("strategy", f"strategy:{strategy_type}", strategy_type, strategy_type=strategy_type,
                             trades=len(trades), wins=wins, minimum=STRATEGY_MIN_TRADES, expectancy_bps=expectancy,
                             status=getattr(row, "current_status", None),
                             sources={source: sum(1 for t in trades if t.source == source) for source in LEARNING_SOURCES}))
        by_cell: dict[str, list] = {}
        for trade in trades:
            try:
                cell = cell_for_attribution(db, trade, cache=cache)["key"]
            except Exception:
                cell = "unknown/unknown/unknown"
            by_cell.setdefault(cell, []).append(trade)
        if current_cell and current_cell not in by_cell:
            by_cell[current_cell] = []
        for cell, members in sorted(by_cell.items()):
            out.append(make_item("regime", f"regime:{strategy_type}:{cell}", f"{strategy_type} in {cell}",
                                 strategy_type=strategy_type, trades=len(members),
                                 wins=sum(1 for t in members if not t.is_loss), minimum=minimum_cell,
                                 expectancy_bps=expectancy, cell=cell, current=cell == current_cell))
    return out


def shadow_items(db: Session) -> list[dict]:
    """One item per (strategy, template, candidate value) from the stored setup-shadow records."""
    from app.services.setup_shadow import latest_scores

    latest = latest_scores(db) or {}
    out = []
    for record in latest.get("records") or []:
        if record.get("is_live"):
            continue
        trades = int(record.get("trades") or 0)
        overlap = int(record.get("overlap") or 0)
        minimum = int(record.get("min_trades") or settings.experiment_verdict_min_trades)
        win_rate = _float(record.get("win_rate"))
        wins = int(round(win_rate * trades)) if win_rate is not None else 0
        delta = _float(record.get("delta_expectancy_bps"))
        item = make_item("shadow", f"shadow:{record.get('strategy_type')}:{record.get('template')}:{record.get('candidate')}",
                         f"{record.get('strategy_type')} {record.get('template')} = {record.get('candidate')}",
                         strategy_type=record.get("strategy_type"), trades=trades, wins=wins, minimum=minimum,
                         expectancy_bps=_float(record.get("expectancy_bps")), template=record.get("template"),
                         candidate=record.get("candidate"), live=record.get("live"), overlap=overlap,
                         delta_expectancy_bps=delta, beats_live=bool(record.get("beats_live")),
                         as_of=latest.get("as_of"))
        # A candidate is compared once both its own trades and the overlap reach the minimum.
        item["needed"] = max(minimum - trades, minimum - overlap, 0)
        out.append(item)
    return out


def experiment_items(db: Session, *, now: datetime) -> list[dict]:
    """One item per watched (adopted, unjudged) experiment change: closed trades since the adoption."""
    from app.services.experiment_engine import SOURCES, adopted_changes
    from app.services.trade_attribution import current_attributions

    out = []
    for change in adopted_changes(db):
        if change.get("state") != "watching":
            continue
        adopted_at = _parse(change.get("adopted_at")) or now
        rows = [row for row in current_attributions(db, since=adopted_at, sources=SOURCES,
                                                    strategy_types=[change["strategy_type"]])
                if _utc(row.opened_at) >= adopted_at]
        watch = change.get("watch") or {}
        minimum = int(watch.get("min_trades") or settings.experiment_verdict_min_trades)
        expected = change.get("expected") or {}
        adopted = _window_expectancy(expected.get("adopted"))
        baseline = _window_expectancy(expected.get("baseline"))
        potential = (adopted - baseline) if adopted is not None and baseline is not None else adopted
        out.append(make_item("experiment", f"experiment:{change['change_id']}",
                             f"{change['strategy_type']}: {change.get('description')}",
                             strategy_type=change["strategy_type"], trades=len(rows),
                             wins=sum(1 for row in rows if not row.is_loss), minimum=minimum,
                             expectancy_bps=potential, change_id=change["change_id"], template=change.get("template"),
                             adopted_at=change.get("adopted_at"), max_trades=watch.get("max_trades")))
    return out


def news_item(db: Session, news_admitted: Optional[Mapping] = None) -> Optional[dict]:
    """Closed round trips on every news-found symbol, against the scorecard's evidence bar."""
    from app.services.trade_attribution import current_attributions

    symbols = set(news_admitted or {})
    try:
        from app.services.news_discovery import latest_state

        state = latest_state(db) or {}
        symbols |= {row.get("symbol") for row in (state.get("admitted") or []) if row.get("symbol")}
        symbols |= set(state.get("holding") or [])
    except Exception:
        pass
    symbols = {str(symbol).upper() for symbol in symbols if symbol}
    if not symbols:
        return None
    rows = [row for row in current_attributions(db, sources=LEARNING_SOURCES) if str(row.symbol).upper() in symbols]
    return make_item("news", "news", "news-found symbols", strategy_type=None, trades=len(rows),
                     wins=sum(1 for row in rows if not row.is_loss), minimum=NEWS_MIN_TRADES, expectancy_bps=None,
                     symbols=sorted(symbols))


def evidence_map(db: Session, *, now: Optional[datetime] = None, strategies: Optional[Iterable] = None,
                 news_admitted: Optional[Mapping] = None) -> dict:
    """Every item the bot is learning about, with trades so far, trades still needed and the Wilson half-width."""
    from app.services.regime_allocation import current_regime

    now = _utc(now or datetime.now(UTC))
    rows = _guarded(db, lambda: learning_strategies(db, strategies), [], "learning strategies")
    regime = _guarded(db, lambda: current_regime(db, now=now), None, "current regime")
    current_cell = (regime or {}).get("cell", {}).get("key") if isinstance(regime, Mapping) else None
    items: list[dict] = []
    items += _guarded(db, lambda: strategy_items(db, rows, current_cell=current_cell), [], "strategy records")
    items += _guarded(db, lambda: shadow_items(db), [], "setup-shadow records")
    items += _guarded(db, lambda: experiment_items(db, now=now), [], "watched experiments")
    news = _guarded(db, lambda: news_item(db, news_admitted), None, "news symbols")
    if news is not None:
        items.append(news)
    # A strategy with no record yet still needs its full minimum: the map must
    # name it even when its round trips could not be read.
    known = {item["key"] for item in items}
    for row in rows:
        key = f"strategy:{row.strategy_type}"
        if key not in known:
            items.append(make_item("strategy", key, row.strategy_type, strategy_type=row.strategy_type, trades=0,
                                   wins=0, minimum=STRATEGY_MIN_TRADES,
                                   expectancy_bps=backtest_expectancy_bps(getattr(row, "parameters", None)),
                                   status=getattr(row, "current_status", None), unread=True))
    items.sort(key=lambda item: (-item["needed"], item["kind"], item["key"]))
    return {
        "as_of": now.isoformat(), "current_cell": current_cell, "items": items,
        "totals": {"items": len(items), "waiting": sum(1 for item in items if item["needed"] > 0),
                   "trades_needed": sum(item["needed"] for item in items)},
        "minimums": {"strategy": STRATEGY_MIN_TRADES, "regime": int(settings.paper_allocation_min_trades),
                     "shadow": int(settings.experiment_verdict_min_trades),
                     "experiment": int(settings.experiment_verdict_min_trades), "news": NEWS_MIN_TRADES},
        "paper_only": True,
    }


def items_for_entry(items: Iterable[Mapping], strategy_type: str, *, news: bool = False) -> list[dict]:
    """The items one learning BUY would add a trade to: its strategy, the current regime cell, watched changes, news."""
    out = []
    for item in items:
        kind = item.get("kind")
        if kind == "strategy" and item.get("strategy_type") == strategy_type:
            out.append(item)
        elif kind == "regime" and item.get("strategy_type") == strategy_type and item.get("current"):
            out.append(item)
        elif kind == "experiment" and item.get("strategy_type") == strategy_type:
            out.append(item)
        elif kind == "news" and news:
            out.append(item)
    return out


# ------------------------------------------------------------------ priority

def potential_weight(expectancy_bps: Optional[float]) -> float:
    """1.0 without a backtest; 100 bps of expectancy doubles the weight, a loss halves it at worst."""
    if expectancy_bps is None:
        return 1.0
    return round(min(2.0, max(0.5, 1.0 + expectancy_bps / 100.0)), 4)


def priority(linked: Iterable[Mapping], *, expectancy_bps: Optional[float], in_flight: int) -> dict:
    linked = list(linked)
    needed = max((int(item.get("needed") or 0) for item in linked), default=0)
    weight = potential_weight(expectancy_bps)
    discount = 1.0 + max(int(in_flight), 0)
    return {"trades_needed": needed, "potential_weight": weight, "in_flight": max(int(in_flight), 0),
            "score": round(needed * weight / discount, 4), "items": [item.get("key") for item in linked]}


def lift_multiplier(item: Optional[Mapping], *, max_lift: Optional[float] = None) -> dict:
    """Up to 2x learning size for an item far from its minimum with a positive backtest expectancy."""
    cap = min(MAX_LIFT, float(max_lift if max_lift is not None else settings.exploration_lift_max))
    if not item:
        return {"multiplier": 1.0, "reason": LIFT_NO_EDGE}
    expectancy = _float(item.get("expectancy_bps"))
    if expectancy is None or expectancy <= 0:
        return {"multiplier": 1.0, "reason": LIFT_NO_EDGE}
    minimum = int(item.get("minimum") or 0)
    if minimum <= 0 or int(item.get("trades") or 0) * 2 >= minimum:
        return {"multiplier": 1.0, "reason": LIFT_NEAR}
    return {"multiplier": round(max(1.0, cap), 4), "reason": LIFT_FAR}


def apply_lift(quantity: Decimal, multiplier: float, scheduled_quantity: Optional[Decimal] = None) -> Decimal:
    """``quantity x multiplier``, never above the scheduled (full-size) quantity."""
    lifted = Decimal(str(quantity)) * Decimal(str(multiplier))
    if scheduled_quantity is not None and Decimal(str(scheduled_quantity)) > 0:
        lifted = min(lifted, Decimal(str(scheduled_quantity)))
    return max(lifted, Decimal("0")).quantize(QUANTITY_INCREMENT, rounding=ROUND_DOWN)


def estimate_notional(equity: Decimal, price: Decimal, *, multiplier: float = 1.0) -> Decimal:
    """What a learning lot at ``price`` would cost, with the bracket's whole-share rounding and the one-share floor."""
    equity, price = max(Decimal(str(equity)), Decimal("0")), Decimal(str(price))
    if price <= 0:
        return Decimal("0")
    base = equity * Decimal(str(settings.paper_exploration_risk_fraction)) * Decimal(str(multiplier))
    whole = (base / price).quantize(Decimal("1"), rounding=ROUND_DOWN)
    if whole <= 0:
        round_up = Decimal(str(settings.paper_min_share_round_up))
        one_share_cap = equity * Decimal(str(settings.paper_min_one_share_equity_fraction))
        if (round_up > 0 and base / price >= round_up) or (one_share_cap > 0 and price <= one_share_cap):
            whole = Decimal("1")
    return whole * price


# --------------------------------------------------------------- the ledger side

def session_window(now: datetime) -> tuple[datetime, datetime, str]:
    """(start, end, label) of the ET session ``now`` belongs to; the calendar day when the exchange is closed."""
    from app.services.exchange_sessions import session_bounds

    now = _utc(now)
    day = now.astimezone(ET).date()
    bounds = session_bounds(day)
    if bounds is not None:
        return _utc(bounds[0]), _utc(bounds[1]), day.isoformat()
    start = datetime.combine(day, datetime.min.time(), tzinfo=ET).astimezone(UTC)
    return start, start + timedelta(days=1), day.isoformat()


def spent_today(db: Session, account, now: datetime) -> Decimal:
    """Notional of this session's exploration BUYs that were not cancelled or rejected."""
    from app.models.stock_paper import StockPaperOrder

    start, end, _label = session_window(now)
    rows = db.query(StockPaperOrder).filter(
        StockPaperOrder.account_id == account.id, StockPaperOrder.source == EXPLORATION_SOURCE,
        StockPaperOrder.side == "buy", StockPaperOrder.created_at >= start, StockPaperOrder.created_at < end,
    ).all()
    total = Decimal("0")
    for row in rows:
        if str(row.status) in DEAD_ORDER_STATUSES:
            continue
        notional = Decimal(str(row.reserved_cash or 0))
        if notional <= 0 and row.limit_price is not None:
            notional = Decimal(str(row.quantity)) * Decimal(str(row.limit_price))
        total += max(notional, Decimal("0"))
    return total


def in_flight_by_strategy(db: Session, account, now: datetime) -> dict[str, int]:
    """Exploration lots per strategy type bought lately and not yet closed into a round trip (an estimate)."""
    from app.models import Strategy
    from app.models.stock_paper import StockPaperOrder
    from app.services.trade_attribution import current_attributions

    since = _utc(now) - timedelta(days=IN_FLIGHT_DAYS)
    rows = db.query(StockPaperOrder).filter(
        StockPaperOrder.account_id == account.id, StockPaperOrder.source == EXPLORATION_SOURCE,
        StockPaperOrder.side == "buy", StockPaperOrder.created_at >= since,
    ).all()
    live = [row for row in rows if str(row.status) not in DEAD_ORDER_STATUSES and row.strategy_id is not None]
    if not live:
        return {}
    ids = {row.strategy_id for row in live}
    type_by_id = {row.id: row.strategy_type for row in db.query(Strategy).filter(Strategy.id.in_(ids)).all()}
    bought: dict[str, int] = {}
    for row in live:
        strategy_type = type_by_id.get(row.strategy_id)
        if strategy_type:
            bought[strategy_type] = bought.get(strategy_type, 0) + 1
    closed: dict[str, int] = {}
    for trade in current_attributions(db, since=since, sources=["exploration_round_trip"], strategy_types=list(bought)):
        if _utc(trade.opened_at) >= since:
            closed[trade.strategy_type] = closed.get(trade.strategy_type, 0) + 1
    return {key: max(count - closed.get(key, 0), 0) for key, count in bought.items() if count - closed.get(key, 0) > 0}


# ---------------------------------------------------------------- the plan

def pending_entries(signal_outcomes: Mapping, strategies: Iterable, news_admitted: Optional[Mapping] = None) -> list[dict]:
    """The learning-sized BUYs a cycle could place: candidate / research strategies, and active ones on news symbols."""
    by_type = {}
    for row in strategies:
        by_type.setdefault(row.strategy_type, row)
    news_admitted = news_admitted or {}
    out = []
    for key, signal in signal_outcomes.items():
        if not isinstance(signal, Mapping) or signal.get("signal_action") != "BUY":
            continue
        symbol, strategy_type = key
        strategy = by_type.get(strategy_type)
        if strategy is None:
            continue
        status = getattr(strategy, "current_status", "paper_trading_active")
        exploring = (status != "paper_trading_active" and settings.paper_exploration_enabled
                     and status in EXPLORATION_STRATEGY_STATUSES)
        news = symbol in news_admitted
        if not exploring and not (news and status == "paper_trading_active"):
            continue
        price = _float(signal.get("reference_price"))
        out.append({"symbol": str(symbol), "strategy_type": strategy_type, "price": price,
                    "exploring": exploring, "news": news,
                    "expectancy_bps": backtest_expectancy_bps(getattr(strategy, "parameters", None))})
    out.sort(key=lambda entry: (entry["symbol"], entry["strategy_type"]))
    return out


class ExplorationPlan:
    """One cycle's allotment of the session's learning budget."""

    def __init__(self, *, session: str, budget: Decimal, spent_before: Decimal, decisions: dict[tuple[str, str], dict],
                 evidence: Optional[dict] = None, degraded: Optional[str] = None):
        self.session = session
        self.budget = Decimal(str(budget))
        self.spent_before = Decimal(str(spent_before))
        self.decisions = decisions
        self.evidence = evidence
        self.degraded = degraded

    def decide(self, symbol: str, strategy_type: str) -> dict:
        found = self.decisions.get((str(symbol), strategy_type))
        if found is None:
            # Not a learning BUY the plan saw (a policy exit, a late signal): learning size, no lift.
            return {"status": "taken", "multiplier": 1.0, "reason": "not ranked this cycle", "priority": None,
                    "trades_needed": None, "items": [], "estimated_notional": None, "session": self.session}
        return dict(found)

    @property
    def taken(self) -> list[dict]:
        return [row for row in self.decisions.values() if row["status"] == "taken"]

    @property
    def skipped(self) -> list[dict]:
        return [row for row in self.decisions.values() if row["status"] == "skipped"]

    def summary(self) -> dict:
        planned = sum((Decimal(str(row["estimated_notional"] or 0)) for row in self.taken), Decimal("0"))
        return {"session": self.session, "budget": str(self.budget), "spent_before": str(self.spent_before),
                "planned": str(planned), "remaining": str(max(self.budget - self.spent_before - planned, Decimal("0"))),
                "ranked": len(self.decisions), "taken": len(self.taken), "skipped": len(self.skipped),
                "degraded": self.degraded, "paper_only": True}

    def picture(self) -> list:
        return sorted((key[0], key[1], row["status"], row["multiplier"]) for key, row in self.decisions.items())


def rank_entries(entries: list[dict], items: Iterable[Mapping], *, in_flight: Optional[Mapping[str, int]] = None,
                 equity: Decimal, budget: Decimal, spent: Decimal, max_lift: Optional[float] = None) -> dict[tuple[str, str], dict]:
    """Allot ``budget - spent`` to ``entries`` greedily by priority (pure; the cycle's decisions keyed by symbol, strategy)."""
    items = list(items)
    in_flight = dict(in_flight or {})
    remaining = max(Decimal(str(budget)) - Decimal(str(spent)), Decimal("0"))
    pending = list(entries)
    decisions: dict[tuple[str, str], dict] = {}
    rank = 0
    while pending:
        scored = []
        for entry in pending:
            linked = items_for_entry(items, entry["strategy_type"], news=entry["news"])
            strategy_item = next((item for item in linked if item.get("kind") == "strategy"), None)
            expectancy = entry.get("expectancy_bps")
            if expectancy is None and strategy_item is not None:
                expectancy = strategy_item.get("expectancy_bps")
            score = priority(linked, expectancy_bps=expectancy, in_flight=in_flight.get(entry["strategy_type"], 0))
            scored.append((score, strategy_item, entry))
        scored.sort(key=lambda row: (-row[0]["score"], -row[0]["trades_needed"], row[2]["symbol"], row[2]["strategy_type"]))
        score, strategy_item, entry = scored[0]
        pending.remove(entry)
        rank += 1
        lift = lift_multiplier(strategy_item, max_lift=max_lift)
        price = entry.get("price")
        notional = estimate_notional(equity, Decimal(str(price)), multiplier=lift["multiplier"]) if price else Decimal("0")
        if lift["multiplier"] > 1 and notional > remaining:
            # The lift is a bonus, not a reason to go without: fall back to learning size.
            lift = {"multiplier": 1.0, "reason": "lift withheld: the session budget covers the learning size only"}
            notional = estimate_notional(equity, Decimal(str(price)), multiplier=1.0) if price else Decimal("0")
        # A lot that rounds to nothing costs nothing here; the cycle's own
        # whole-share check names the real reason for it.
        taken = notional <= remaining
        decision = {
            "status": "taken" if taken else "skipped", "symbol": entry["symbol"], "strategy_type": entry["strategy_type"],
            "rank": rank, "priority": score["score"], "trades_needed": score["trades_needed"],
            "potential_weight": score["potential_weight"], "in_flight": score["in_flight"], "items": score["items"],
            "expectancy_bps": entry.get("expectancy_bps") if entry.get("expectancy_bps") is not None
            else (strategy_item or {}).get("expectancy_bps"),
            "multiplier": lift["multiplier"] if taken else 1.0, "reason": lift["reason"] if taken else
            f"session learning budget spent on higher-priority entries ({remaining:.2f} left of {budget}; lot {notional:.2f})",
            "estimated_notional": str(notional) if price else None, "budget_remaining": str(remaining),
            "news": entry["news"], "exploring": entry["exploring"],
        }
        if taken:
            remaining -= notional
            in_flight[entry["strategy_type"]] = in_flight.get(entry["strategy_type"], 0) + 1
            for item in items:
                if item.get("key") in score["items"]:
                    item["needed"] = max(int(item.get("needed") or 0) - 1, 0)
        decisions[(entry["symbol"], entry["strategy_type"])] = decision
    return decisions


def plan_exploration(db: Session, account, signal_outcomes: Mapping, strategies: Iterable,
                     news_admitted: Optional[Mapping] = None, *, now: datetime) -> ExplorationPlan:
    """Rank this cycle's learning BUYs against the evidence map and allot the session's budget.

    Every read degrades explicitly: without the map each strategy needs its
    full minimum, without the ledger nothing was spent yet.  The plan never
    raises into the cycle: when it cannot be built at all, every learning
    BUY is taken at learning size, as before the budget existed.
    """
    try:
        return _plan_exploration(db, account, signal_outcomes, strategies, news_admitted, now=now)
    except Exception as exc:
        logger.warning("exploration budget: plan unavailable, learning BUYs taken unranked: %s", exc.__class__.__name__)
        return ExplorationPlan(session=_utc(now).astimezone(ET).date().isoformat(), budget=Decimal("0"),
                               spent_before=Decimal("0"), decisions={},
                               degraded=f"plan unavailable ({exc.__class__.__name__}); learning BUYs taken unranked")


def _plan_exploration(db: Session, account, signal_outcomes: Mapping, strategies: Iterable,
                      news_admitted: Optional[Mapping], *, now: datetime) -> ExplorationPlan:
    now = _utc(now)
    strategies = list(strategies)
    try:
        start, end, session = session_window(now)
    except Exception:
        session = now.astimezone(ET).date().isoformat()
    equity = max(Decimal(str(getattr(account, "equity", 0) or 0)), Decimal("0"))
    budget = equity * Decimal(str(settings.exploration_budget_fraction))
    entries = pending_entries(signal_outcomes, strategies, news_admitted)
    if not entries:
        return ExplorationPlan(session=session, budget=budget, spent_before=Decimal("0"), decisions={})
    degraded = []
    evidence = _guarded(db, lambda: evidence_map(db, now=now, strategies=strategies, news_admitted=news_admitted),
                        None, "evidence map")
    if evidence is None:
        degraded.append("evidence map unreadable; every item needs its full minimum")
        evidence = {"as_of": now.isoformat(), "current_cell": None, "items": [
            make_item("strategy", f"strategy:{row.strategy_type}", row.strategy_type, strategy_type=row.strategy_type,
                      trades=0, wins=0, minimum=STRATEGY_MIN_TRADES,
                      expectancy_bps=backtest_expectancy_bps(getattr(row, "parameters", None)))
            for row in learning_strategies(db, strategies)], "paper_only": True}
    spent = _guarded(db, lambda: spent_today(db, account, now), None, "session spend")
    if spent is None:
        degraded.append("session spend unreadable; assumed nothing spent")
        spent = Decimal("0")
    in_flight = _guarded(db, lambda: in_flight_by_strategy(db, account, now), None, "lots in flight")
    if in_flight is None:
        degraded.append("lots in flight unreadable; no discount")
        in_flight = {}
    items = [dict(item) for item in evidence.get("items") or []]
    decisions = rank_entries(entries, items, in_flight=in_flight, equity=equity, budget=budget, spent=spent)
    return ExplorationPlan(session=session, budget=budget, spent_before=spent, decisions=decisions, evidence=evidence,
                           degraded="; ".join(degraded) or None)


# ------------------------------------------------------------ audit rows

def latest_plan_row(db: Session):
    from app.models import AuditLog

    return db.query(AuditLog).filter(AuditLog.event_type == EVENT, AuditLog.action == ACTION).order_by(
        AuditLog.id.desc()).first()


def record_plan(db: Session, plan: ExplorationPlan, results: Iterable[Mapping], now: datetime) -> bool:
    """Audit what the budget took and skipped, only when that picture changes within the session (paper only)."""
    from app.services.audit import write_audit_log

    if not plan.decisions:
        return False
    outcomes = {(str(row.get("symbol")), str(row.get("strategy"))): row.get("status") for row in results}
    picture = [(*entry, outcomes.get((entry[0], entry[1]))) for entry in plan.picture()]
    previous = latest_plan_row(db)
    payload_before = (previous.payload or {}) if previous is not None else {}
    if previous is not None and payload_before.get("session") == plan.session and payload_before.get("picture") == [
            list(row) for row in picture]:
        return False
    taken = [{**row, "outcome": outcomes.get((row["symbol"], row["strategy_type"]))} for row in plan.taken]
    skipped = [{**row, "outcome": outcomes.get((row["symbol"], row["strategy_type"]))} for row in plan.skipped]
    write_audit_log(
        db, event_type=EVENT, action=ACTION, status="recorded",
        message=(f"exploration budget {plan.session}: {len(taken)} learning lots taken, {len(skipped)} skipped "
                 f"({plan.summary()['remaining']} of {plan.budget} left)"),
        payload={"observed_at": _utc(now).isoformat(), **plan.summary(), "taken": taken, "skipped": skipped,
                 "picture": [list(row) for row in picture], "method": METHOD,
                 "evidence": {"as_of": (plan.evidence or {}).get("as_of"),
                              "current_cell": (plan.evidence or {}).get("current_cell"),
                              "items": _compact((plan.evidence or {}).get("items") or [])}},
    )
    db.commit()
    return True


ITEM_KEYS = ("kind", "key", "label", "strategy_type", "trades", "wins", "minimum", "needed", "expectancy_bps",
             "win_rate", "low", "high", "half_width", "cell", "current", "template", "candidate", "overlap",
             "delta_expectancy_bps", "change_id")


def _compact(items: Iterable[Mapping]) -> list[dict]:
    return [{key: item.get(key) for key in ITEM_KEYS if key in item} for item in items]


# ------------------------------------------------------------- projections

def next_verdicts(items: Iterable[Mapping]) -> list[dict]:
    """Trades until the next verdict per watched item, soonest first (items already past their minimum last)."""
    rows = []
    for item in items:
        if item.get("kind") == "regime" and not item.get("current"):
            continue
        rows.append({"key": item.get("key"), "kind": item.get("kind"), "label": item.get("label"),
                     "strategy_type": item.get("strategy_type"), "trades": item.get("trades"),
                     "minimum": item.get("minimum"), "trades_until_verdict": item.get("needed"),
                     "win_rate": item.get("win_rate"), "half_width": item.get("half_width"),
                     "expectancy_bps": item.get("expectancy_bps")})
    rows.sort(key=lambda row: (row["trades_until_verdict"] == 0, row["trades_until_verdict"] or 0, row["key"] or ""))
    return rows


def budget_today(db: Session, *, now: datetime) -> dict:
    from app.services.stock_paper_ledger import active_paper_account

    account = _guarded(db, lambda: active_paper_account(db), None, "paper account")
    equity = max(Decimal(str(getattr(account, "equity", 0) or 0)), Decimal("0")) if account is not None else Decimal("0")
    budget = equity * Decimal(str(settings.exploration_budget_fraction))
    spent = _guarded(db, lambda: spent_today(db, account, now), Decimal("0"), "session spend") if account is not None else Decimal("0")
    _start, _end, session = session_window(now)
    return {"session": session, "equity": str(equity), "fraction": float(settings.exploration_budget_fraction),
            "budget": str(budget), "spent": str(spent), "remaining": str(max(budget - spent, Decimal("0"))),
            "learning_fraction": float(settings.paper_exploration_risk_fraction),
            "max_lift": min(MAX_LIFT, float(settings.exploration_lift_max))}


def exploration_budget_view(db: Session, *, now: Optional[datetime] = None) -> dict:
    """``GET /stock-paper/exploration-budget``: the evidence map, today's budget, what was taken and skipped."""
    now = _utc(now or datetime.now(UTC))
    evidence = evidence_map(db, now=now)
    budget = budget_today(db, now=now)
    latest = _guarded(db, lambda: latest_plan_row(db), None, "latest plan")
    payload = (latest.payload or {}) if latest is not None else {}
    today = payload if payload.get("session") == budget["session"] else {}
    return {
        "as_of": now.isoformat(), "budget": budget, "evidence": evidence,
        "next_verdicts": next_verdicts(evidence["items"]),
        "latest_cycle": ({"observed_at": payload.get("observed_at"), "session": payload.get("session"),
                          "taken": payload.get("taken") or [], "skipped": payload.get("skipped") or [],
                          "planned": payload.get("planned"), "degraded": payload.get("degraded")} if payload else None),
        "today": {"taken": today.get("taken") or [], "skipped": today.get("skipped") or []},
        "method": METHOD,
        "lift_rule": (f"Up to {budget['max_lift']:g}x learning size for an item with fewer than half its minimum trades "
                      "and a positive backtest expectancy; never above the scheduled size or any ledger cap."),
        "paper_only": True,
    }


def scoreboard_exploration(db: Session, *, now: Optional[datetime] = None) -> dict:
    """The ``exploration`` section of the learning scoreboard: trades until the next verdict per watched item."""
    now = _utc(now or datetime.now(UTC))
    evidence = evidence_map(db, now=now)
    budget = budget_today(db, now=now)
    verdicts = next_verdicts(evidence["items"])
    waiting = [row for row in verdicts if (row["trades_until_verdict"] or 0) > 0]
    latest = _guarded(db, lambda: latest_plan_row(db), None, "latest plan")
    payload = (latest.payload or {}) if latest is not None else {}
    today = payload if payload.get("session") == budget["session"] else {}
    soonest = waiting[0] if waiting else None
    return {
        "as_of": now.isoformat(), "status": "ok" if evidence["items"] else "waiting",
        "detail": (f"{len(waiting)} of {len(verdicts)} watched items still need trades; "
                   + (f"next verdict: {soonest['label']} in {soonest['trades_until_verdict']} trades. " if soonest else "")
                   + f"Session budget {budget['budget']} ({budget['fraction']:.2%} of equity), {budget['spent']} spent."),
        "budget": budget, "next_verdicts": verdicts,
        "today": {"taken": len(today.get("taken") or []), "skipped": len(today.get("skipped") or [])},
    }
