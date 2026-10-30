"""Performance-weighted paper risk fractions.

Each paper session every active autotrader strategy type gets a bounded
multiplier on the risk-rule ``max_risk_per_trade`` used to size its BUYs:

* Evidence is the strategy's most recent scheduled round trips (attributed,
  costs known; exploration probes never count), within a lookback window.
* Below ``paper_allocation_min_trades`` the multiplier is exactly 1.0.
* ``mean`` net return per trade is shrunk toward zero by sample size
  (``n / (n + k)``).  Negative performance shrinks the multiplier linearly
  down to ``paper_allocation_floor`` at ``-return_scale``; positive
  performance grows it only modestly, up to ``paper_allocation_ceiling`` at
  ``+return_scale``.  No strategy is ever zeroed.
* Concentration cap: no single strategy may hold more than
  ``paper_allocation_max_share`` of the summed multipliers when at least
  three strategies are weighted; excess growth is trimmed (never below 1.0).

Weights are immutable per (session, strategy type): the first cycle of a
session computes and stores them, later cycles reuse them.  Paper only;
live sizing does not read these rows.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.loss_feedback import StockAllocationWeight

UTC = timezone.utc


def bounds() -> dict:
    return {
        "floor": settings.paper_allocation_floor,
        "ceiling": settings.paper_allocation_ceiling,
        "min_trades": settings.paper_allocation_min_trades,
        "lookback_trades": settings.paper_allocation_lookback_trades,
        "lookback_days": settings.paper_allocation_lookback_days,
        "return_scale": settings.paper_allocation_return_scale,
        "shrinkage_trades": settings.paper_allocation_shrinkage_trades,
        "max_share": settings.paper_allocation_max_share,
    }


def performance_multiplier(returns: list[float], policy: Optional[dict] = None) -> dict:
    """Pure, bounded multiplier from a list of per-trade net returns."""
    policy = policy or bounds()
    n = len(returns)
    if n < policy["min_trades"]:
        return {"multiplier": 1.0, "status": "insufficient_evidence", "trades": n, "mean_return": None,
                "shrunk_return": None}
    mean = sum(returns) / n
    shrunk = mean * n / (n + policy["shrinkage_trades"])
    scale = policy["return_scale"]
    if shrunk < 0:
        value = max(policy["floor"], 1.0 + (shrunk / scale) * (1.0 - policy["floor"]))
        status = "reduced"
    elif shrunk > 0:
        value = min(policy["ceiling"], 1.0 + (shrunk / scale) * (policy["ceiling"] - 1.0))
        status = "increased"
    else:
        value, status = 1.0, "baseline"
    value = min(policy["ceiling"], max(policy["floor"], value))
    return {"multiplier": round(value, 6), "status": status, "trades": n,
            "mean_return": round(mean, 8), "shrunk_return": round(shrunk, 8)}


def apply_concentration_cap(weights: dict[str, float], max_share: float) -> dict[str, float]:
    """Trim growth so no strategy exceeds ``max_share`` of the total (>=3 strategies)."""
    if len(weights) < 3:
        return dict(weights)
    out = dict(weights)
    for _ in range(10):
        total = sum(out.values())
        over = {key: value for key, value in out.items() if value / total > max_share + 1e-9 and value > 1.0}
        if not over:
            break
        for key in over:
            others = total - out[key]
            cap = max_share * others / (1 - max_share)
            out[key] = round(max(1.0, min(out[key], cap)), 6)
    return out


def compute_weights(db: Session, strategy_types: Iterable[str], *, now: datetime) -> dict[str, dict]:
    from app.services.trade_attribution import current_attributions

    policy = bounds()
    since = now - timedelta(days=policy["lookback_days"])
    results: dict[str, dict] = {}
    for strategy_type in sorted(set(strategy_types)):
        rows = [row for row in current_attributions(db, since=since, sources=["scheduled_round_trip"],
                                                    strategy_types=[strategy_type])
                if row.costs_known and row.net_return is not None and row.closed_at is not None]
        rows = rows[: policy["lookback_trades"]]
        returns = [float(row.net_return) for row in rows]
        results[strategy_type] = performance_multiplier(returns, policy) | {
            "attribution_ids": [row.id for row in rows],
        }
    capped = apply_concentration_cap({key: value["multiplier"] for key, value in results.items()}, policy["max_share"])
    for key, value in capped.items():
        if value != results[key]["multiplier"]:
            results[key]["uncapped_multiplier"] = results[key]["multiplier"]
            results[key]["multiplier"] = value
            results[key]["status"] = "concentration_capped"
    return results


def session_weights(db: Session, strategy_types: Iterable[str], *, session_date: date,
                    now: Optional[datetime] = None) -> dict[str, Decimal]:
    """Stored weights for the session, computing any missing strategy once."""
    now = now or datetime.now(UTC)
    types = sorted(set(strategy_types))
    if not settings.paper_allocation_weighting_enabled or not types:
        return {key: Decimal("1") for key in types}
    stored = {row.strategy_type: row for row in db.scalars(select(StockAllocationWeight).where(
        StockAllocationWeight.session_date == session_date,
        StockAllocationWeight.strategy_type.in_(types))).all()}
    missing = [key for key in types if key not in stored]
    if missing:
        computed = compute_weights(db, types, now=now)
        for key in missing:
            value = computed[key]
            row = StockAllocationWeight(
                session_date=session_date, strategy_type=key, multiplier=Decimal(str(value["multiplier"])),
                status=value["status"], evidence=value | {"policy": bounds(), "computed_at": now.isoformat()},
                paper_only=True,
            )
            try:
                with db.begin_nested():
                    db.add(row)
                    db.flush()
                stored[key] = row
            except IntegrityError:
                stored[key] = db.scalar(select(StockAllocationWeight).where(
                    StockAllocationWeight.session_date == session_date,
                    StockAllocationWeight.strategy_type == key))
    return {key: clamp(Decimal(str(stored[key].multiplier))) for key in types if stored.get(key) is not None}


def clamp(value: Decimal) -> Decimal:
    floor = Decimal(str(settings.paper_allocation_floor))
    ceiling = Decimal(str(settings.paper_allocation_ceiling))
    return min(ceiling, max(floor, value))


def allocation_projection(db: Session, *, limit_sessions: int = 10) -> dict:
    sessions = db.scalars(select(StockAllocationWeight.session_date).distinct()
                          .order_by(StockAllocationWeight.session_date.desc()).limit(limit_sessions)).all()
    history = []
    for day in sessions:
        rows = db.scalars(select(StockAllocationWeight).where(StockAllocationWeight.session_date == day)
                          .order_by(StockAllocationWeight.strategy_type)).all()
        history.append({"session_date": day.isoformat(), "weights": [
            {"strategy_type": row.strategy_type, "multiplier": float(row.multiplier), "status": row.status,
             "trades": (row.evidence or {}).get("trades"), "mean_return": (row.evidence or {}).get("mean_return"),
             "shrunk_return": (row.evidence or {}).get("shrunk_return")}
            for row in rows
        ]})
    return {
        "enabled": settings.paper_allocation_weighting_enabled,
        "policy": bounds(),
        "current": history[0] if history else None,
        "history": history,
        "paper_only": True,
        "live_sizing_affected": False,
    }
