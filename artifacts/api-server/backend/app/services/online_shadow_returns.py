"""Predeclared price-proxy experiments, never broker fills or portfolio returns."""
from datetime import datetime, timedelta
from math import isfinite
from statistics import mean

from app.services.intraday_data import _aware_utc

VERSION = "iex-long-cash-shadow-v1"
COST_BPS = (0, 1, 5, 10)


def plan(issued_at, target_at, probabilities):
    issued_at, target_at = _aware_utc(issued_at), _aware_utc(target_at)
    entry_at = issued_at.replace(second=0, microsecond=0) + timedelta(minutes=1)
    if entry_at >= target_at or not probabilities or any(
        not isfinite(p) or not 0 <= p <= 1 for p in probabilities.values()
    ):
        raise ValueError("Invalid forward shadow declaration")
    return {
        "version": VERSION, "entry_at": entry_at.isoformat(), "exit_at": target_at.isoformat(),
        "entry_price": "next_minute_open", "exit_price": "target_minute_close",
        "long_threshold": 0.55, "cost_bps_per_side": list(COST_BPS),
        "actions": {**{name: int(p > 0.55) for name, p in probabilities.items()}, "always_long": 1},
        "missing_policy": "unavailable_at_first_target_scoring_no_backfill",
    }


def scores(declaration, entry, exit_price):
    if not isfinite(entry) or not isfinite(exit_price) or entry <= 0 or exit_price <= 0:
        raise ValueError("Invalid shadow prices")
    ratio = exit_price / entry
    gross = (ratio - 1) * 10000
    net = {str(cost): gross - cost * (1 + ratio) for cost in COST_BPS}
    if not isfinite(gross) or any(not isfinite(value) for value in net.values()):
        raise ValueError("Invalid shadow return")
    return {name: {
        "action": "long" if action else "cash",
        "gross_bps": action * gross,
        # Hypothetical costs apply to entry AND exit notionals, not a known fee.
        "net_bps_by_cost": {cost: action * value for cost, value in net.items()},
    } for name, action in declaration["actions"].items()}


def observe(declaration, entry_bar, exit_price, *, now):
    entry_at = datetime.fromisoformat(declaration["entry_at"])
    eligible = (entry_bar is not None
        and _aware_utc(entry_bar.opened_at) == entry_at
        and entry_at + timedelta(minutes=1) <= _aware_utc(entry_bar.ingested_at)
        <= min(now, entry_at + timedelta(minutes=16)))
    base = {"version": VERSION, "observed_at": now.isoformat(), "broker_fills": False}
    if not eligible:
        return {**base, "status": "unavailable", "reason": "entry_bar_not_observed_in_time"}
    entry = float(entry_bar.open)
    try:
        result = scores(declaration, entry, exit_price)
    except ValueError:
        return {**base, "status": "unavailable", "reason": "invalid_price"}
    return {**base, "status": "observed", "entry_open": entry, "exit_close": exit_price,
            "entry_ingested_at": _aware_utc(entry_bar.ingested_at).isoformat(), "scores": result}


def summarize(rows):
    paired, declared, unavailable, invalid = [], 0, 0, 0
    for row in rows:
        declaration = row.prediction.get("shadow")
        if declaration is None:
            continue
        declared += 1
        observation = row.outcome.get("shadow", {})
        try:
            expected = plan(row.issued_at, row.target_at, row.prediction["comparators"]["probabilities"])
            if declaration != expected or observation.get("version") != VERSION:
                raise ValueError("Invalid declaration")
            if observation.get("status") == "unavailable":
                unavailable += 1
                continue
            entry_at = datetime.fromisoformat(declaration["entry_at"])
            ingested = datetime.fromisoformat(observation["entry_ingested_at"])
            observed = datetime.fromisoformat(observation["observed_at"])
            if (ingested.tzinfo is None or observed.tzinfo is None
                    or not entry_at + timedelta(minutes=1) <= ingested <= min(observed, entry_at + timedelta(minutes=16))
                    or observation["exit_close"] != row.outcome["close"]
                    or observation.get("broker_fills") is not False
                    or observation.get("status") != "observed"
                    or observed != datetime.fromisoformat(row.outcome["observed_at"])):
                raise ValueError("Invalid observation")
            recomputed = scores(declaration, observation["entry_open"], observation["exit_close"])
            if recomputed != observation["scores"]:
                raise ValueError("Invalid metrics")
            paired.append(recomputed)
        except (KeyError, TypeError, ValueError, OverflowError):
            invalid += 1
    names = list(paired[0]) if paired else []
    return {
        "version": VERSION, "declared_scored_forecasts": declared,
        "paired_observations": len(paired), "unavailable_observations": unavailable,
        "invalid_observations": invalid, "cost_bps_per_side": list(COST_BPS),
        "research_only": True, "broker_fills": False, "execution_eligible": False,
        "profitability_proven": False, "overlapping_windows": True,
        "aggregation": "mean_per_forecast_not_portfolio_pnl",
        "price_proxy": "IEX next-minute open to target-minute close; not executable quotes",
        "strategies": [{"name": name,
            "long_observations": sum(row[name]["action"] == "long" for row in paired),
            "mean_gross_bps": mean(row[name]["gross_bps"] for row in paired),
            "mean_net_bps_by_cost": {str(cost): mean(row[name]["net_bps_by_cost"][str(cost)] for row in paired)
                                     for cost in COST_BPS},
        } for name in names],
    }
