"""Rule mutation: operators, dedup across runs, cumulative trials, parents, budget, lineage."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models import AuditLog, Strategy
from app.services import rule_mutation as rm
from app.services import strategy_discovery as sd
from app.services.audit import write_audit_log
from app.services.strategies import rules

from tests.test_loss_feedback_loop import db  # noqa: F401  (fixture)

UTC = timezone.utc
SYMBOLS = ["SPY", "QQQ", "XLK", "XLF", "AAPL", "MSFT"]
GROUPS = sd.universes(SYMBOLS)
LIBRARY = sd.candidate_library(SYMBOLS)


def _reverting(rows=2200, seed=1, start="2013-01-02", phi=0.6, sigma=0.02):
    """A drifting price with a strongly mean-reverting daily component: the reversion families come close to the gate."""
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, sigma, rows)
    deviation = np.zeros(rows)
    for t in range(1, rows):
        deviation[t] = phi * deviation[t - 1] + noise[t]
    close = 50 * np.exp(np.cumsum(rng.normal(0.0004, 0.004, rows)) + deviation)
    open_ = np.r_[close[0], close[:-1]] * np.exp(rng.normal(0, 0.003, rows))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.005, rows)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.005, rows)))
    return pd.DataFrame({"date": pd.bdate_range(start, periods=rows).strftime("%Y-%m-%d"), "open": open_,
                         "high": high, "low": low, "close": close, "volume": 2_000_000.0})


def _frames(symbols=("SPY", "QQQ", "XLK")):
    return {s: _reverting(seed=i) for i, s in enumerate(symbols)}


# ---------------------------------------------------------------- operators

@pytest.mark.parametrize("operator", sorted(rm.OPERATORS))
def test_every_operator_yields_a_valid_rule_with_a_new_hash(operator):
    produced = 0
    for candidate in LIBRARY:
        child = rm.mutate(candidate, operator, seed=11, groups=GROUPS)
        if child is None:
            continue
        produced += 1
        rules.validate_rule(child["rule"])
        assert child["hash"] != rm.candidate_hash(candidate)
        assert child["id"] != candidate["id"]
        assert child["lineage"] == {"parent_id": candidate["id"], "operator": operator, "seed": 11,
                                    "generation": 1, "root_id": candidate["id"]}
        assert child["family"] == candidate["family"]
        assert (child["stop_loss_pct"], child["take_profit_pct"], child["max_holding_sessions"]) == \
            (candidate["stop_loss_pct"], candidate["take_profit_pct"], candidate["max_holding_sessions"])
        assert child["symbols"] == list(GROUPS[child["universe"]])
        if operator != "change_universe":
            assert child["universe"] == candidate["universe"]
        if operator == "shift_threshold":
            assert rm.canonical_rule(child["rule"])["entry"] != rm.canonical_rule(candidate["rule"])["entry"] or \
                rm.canonical_rule(child["rule"])["exit"] != rm.canonical_rule(candidate["rule"])["exit"]
        if operator == "drop_condition":
            assert len(child["rule"]["entry"]) >= 1
            assert len(child["rule"]["entry"]) + len(child["rule"].get("exit", [])) == \
                len(candidate["rule"]["entry"]) + len(candidate["rule"].get("exit", [])) - 1
        if operator == "add_condition":
            assert len(child["rule"]["entry"]) == len(candidate["rule"]["entry"]) + 1
            added = child["rule"]["entry"][-1]
            assert added["f"] not in {c["f"] for c in [*candidate["rule"]["entry"], *candidate["rule"].get("exit", [])]}
    assert produced > len(LIBRARY) * 0.8, operator


def test_mutation_is_deterministic_and_respects_grids_and_families():
    parent = next(c for c in LIBRARY if c["family"] == "pullback_in_uptrend" and c["universe"] == "index_etfs")
    first, second = (rm.mutate(parent, "shift_threshold", seed=3, groups=GROUPS) for _ in range(2))
    assert first == second
    shifted = {(c["f"], c["v"]) for c in [*first["rule"]["entry"], *first["rule"]["exit"]]} - \
        {(c["f"], c["v"]) for c in [*parent["rule"]["entry"], *parent["rule"]["exit"]]}
    (feature_name, value), = shifted
    step, low, high = rm.grid_for(feature_name)
    before = next(c["v"] for c in [*parent["rule"]["entry"], *parent["rule"]["exit"]] if c["f"] == feature_name)
    assert abs(abs(value - before) - step) < 1e-9 and low <= value <= high
    # Booleans have no grid; the earnings family stays on single stocks; an ETF rule never gains earn_surprise.
    assert rm.grid_for("at_low_10") is None and rm.grid_for("rsi_2") == (5, 5, 95)
    earnings = next(c for c in LIBRARY if c["family"] == "earnings_drift")
    assert rm.mutate(earnings, "change_universe", seed=1, groups=GROUPS) is None
    for seed in range(40):
        child = rm.mutate(parent, "add_condition", seed=seed, groups=GROUPS)
        assert not any(c["f"] == "earn_surprise" for c in child["rule"]["entry"])
    # An open-timed rule keeps its gap condition and gains no exit.
    gap = next(c for c in LIBRARY if c["family"] == "opening_gap_fill" and len(c["rule"]["entry"]) == 2)
    for seed in range(20):
        dropped = rm.mutate(gap, "drop_condition", seed=seed, groups=GROUPS)
        assert dropped is not None and rules.uses_feature(dropped["rule"], "gap") and not dropped["rule"]["exit"]
    assert rm.mutate(gap, "swap_exit", seed=1, groups=GROUPS) is None
    # Condition order is not a new trial.
    swapped = {"entry": list(reversed(parent["rule"]["entry"])), "exit": parent["rule"]["exit"]}
    assert rm.trial_hash(swapped, parent, parent["universe"]) == rm.candidate_hash(parent)


def test_evolve_dedups_against_seen_hashes_and_itself():
    seen = {rm.candidate_hash(c) for c in LIBRARY}
    parents = [c for c in LIBRARY if c["universe"] == "index_etfs"][:12]
    children = rm.evolve(parents, count=40, seed=7, seen=seen, groups=GROUPS)
    assert len(children) == 40
    hashes = [c["hash"] for c in children]
    assert len(set(hashes)) == 40 and not set(hashes) & seen
    assert [c["hash"] for c in rm.evolve(parents, count=40, seed=7, seen=seen, groups=GROUPS)] == hashes
    assert len(Counter(c["lineage"]["operator"] for c in children)) >= 3
    # Everything a second batch produces is new against the first.
    later = rm.evolve(parents, count=10, seed=8, seen=seen | set(hashes), groups=GROUPS)
    assert later and not {c["hash"] for c in later} & (seen | set(hashes))
    assert rm.evolve([], count=5, seed=1) == []


# ---------------------------------------------------------------- parents, budget, gate

def _row(cid, tier, failed=(), net_low=10.0, timing="close", lineage=None):
    checks = {key: key not in failed for key in ("enough_trades", "net_expectancy_ci_low_gt_0",
                                                 "excess_over_drift_ci_low_gt_0", "post_period_positive",
                                                 "deflated_sharpe", "holdout_positive")}
    rule = {"entry": [{"f": "gap", "op": "<", "v": -0.005}, {"f": "gap", "op": ">", "v": -0.03}], "timing": "open"} \
        if timing == "open" else {"entry": [{"f": "down_streak", "op": ">=", "v": 3}],
                                  "exit": [{"f": "ret_1", "op": ">", "v": 0}]}
    row = {"id": cid, "family": "down_streak", "universe": "index_etfs", "rule": rule,
           "description": rules.describe_rule(rule), "stop_loss_pct": 0.1, "take_profit_pct": 0.0,
           "max_holding_sessions": 10, "symbols": ["SPY", "QQQ"], "hash": cid,
           "gate": {"tier": tier, "passed": tier == "proven", "checks": checks, "deflated_sharpe": 0.1},
           "discovery": {"net": {"trades": 300, "expectancy_ci_bps": [net_low, net_low + 20]},
                         "excess_over_drift": {"expectancy_ci_bps": [net_low - 5, net_low + 15]}},
           "post_period": {"trades": 120, "expectancy_bps": 10.0},
           "holdout": {"trades": 30, "win_rate": 0.6, "expectancy_bps": 12.0}, "sharpe_per_period": 0.02, "observations": 3000, "skewness": 0.0, "kurtosis": 3.0}
    if lineage:
        row["lineage"] = lineage
    return row


def test_parent_selection_takes_near_misses_by_expectancy_bound_plus_explore_survivors():
    rows = [_row("far", "rejected", failed=("deflated_sharpe", "holdout_positive"), net_low=99.0),
            _row("open", "explore", failed=("deflated_sharpe",), net_low=90.0, timing="open"),
            *[_row(f"near{i}", "rejected", failed=("holdout_positive",), net_low=float(i)) for i in range(12)],
            _row("explore_low", "explore", failed=("deflated_sharpe",), net_low=-1.0),
            _row("proven", "proven", net_low=0.5)]
    parents = sd.select_parents(rows, k=3)
    assert [p["id"] for p in parents] == ["near11", "near10", "near9", "explore_low", "proven"]
    assert sd.improves_on(_row("a", "explore", net_low=1.0), _row("b", "rejected", net_low=50.0))
    assert sd.improves_on(_row("a", "rejected", net_low=2.0), _row("b", "rejected", net_low=1.0))
    assert not sd.improves_on(_row("a", "rejected", net_low=1.0), _row("b", "rejected", net_low=1.0))


def test_evolution_stops_at_the_wall_clock_budget_and_records_the_step():
    frames = _frames()
    library = [c for c in sd.candidate_library(frames) if c["family"] in ("down_streak", "n_day_low")]
    result = sd.run_discovery(frames, frames.keys(), library=library)
    ticks = iter(range(0, 10_000))
    step = sd.evolve_library(result, frames, seen_hashes=set(), seed=5, groups=sd.universes(frames.keys()),
                             parents=4, children=12, budget_seconds=4, clock=lambda: next(ticks))
    assert step["budget_exhausted"] and 0 < step["children_evaluated"] < step["children_generated"] == 12
    assert step["elapsed_seconds"] >= 4 and len(step["parents"]) >= 1
    assert sum(step["operators"].values()) == step["children_evaluated"]
    children = [row for row in result["candidates"] if row.get("lineage")]
    assert len(children) == step["children_evaluated"]
    for row in children:
        assert row["lineage"]["parent_id"] in step["parents"] and "improved_on_parent" in row["lineage"]
        assert row["gate"]["dsr_trials"] == len(library) + 12
    assert step["best_child"]["parent_id"] in step["parents"]
    # Without a budget every child is evaluated and gated.
    full = sd.run_discovery(frames, frames.keys(), library=library)
    whole = sd.evolve_library(full, frames, seen_hashes=set(), seed=5, groups=sd.universes(frames.keys()),
                              parents=4, children=12, budget_seconds=1e9)
    assert not whole["budget_exhausted"] and whole["children_evaluated"] == 12


def test_cumulative_trials_reach_the_deflated_sharpe():
    from tests.test_strategy_discovery import _summary

    sharpes = [0.0, 0.01, -0.01, 0.02, 0.03]
    few = sd.gate(_summary(sharpe_per_period=0.05), trials=5, trial_sharpes=sharpes)
    many = sd.gate(_summary(sharpe_per_period=0.05), trials=5000, trial_sharpes=sharpes)
    assert few["dsr_trials"] == 5 and many["dsr_trials"] == 5000
    assert many["deflated_sharpe"] < few["deflated_sharpe"]
    result = {"candidates": [_row("a", "rejected"), _row("b", "rejected")]}
    sd.apply_gate(result, trials=1234)
    assert all(row["gate"]["dsr_trials"] == 1234 for row in result["candidates"])


# ---------------------------------------------------------------- scheduled run

def _run(db, frames, now, **overrides):  # noqa: F811
    patches = {"paper_execution_symbols": list(frames), "strategy_discovery_history_start": "2013-01-01",
               "replay_point_in_time_universe": False, **overrides}
    with patch.multiple(settings, **patches):
        return sd.run_strategy_discovery(db, now=now, loader=lambda symbol: frames[symbol].copy())


def _trials_rows(db):  # noqa: F811
    return db.scalars(select(AuditLog).where(AuditLog.event_type == sd.TRIALS_EVENT).order_by(AuditLog.id)).all()


def test_trials_accumulate_across_runs_and_children_are_never_retried(db):  # noqa: F811
    frames = _frames()
    library_size = len(sd.candidate_library(frames))
    first = _run(db, frames, datetime(2026, 11, 8, 15, tzinfo=UTC))
    assert first["status"] == "complete" and first["mutation"]["status"] == "complete"
    assert first["library"] == library_size
    assert first["mutation"]["children_evaluated"] == sd.MUTATION_CHILDREN
    assert first["trials"] == library_size + sd.MUTATION_CHILDREN == first["mutation"]["trials_cumulative"]
    rows = _trials_rows(db)
    assert len(rows) == 1 and rows[0].payload["count"] == first["trials"] and rows[0].payload["runs"] == 1
    first_hashes = set(rows[0].payload["hashes"])
    assert len(first_hashes) == first["trials"]

    second = _run(db, frames, datetime(2026, 11, 15, 15, tzinfo=UTC))
    retained = len(rows[0].payload["retained"])
    assert second["library"] == library_size + retained
    assert second["mutation"]["children_evaluated"] == sd.MUTATION_CHILDREN
    assert second["trials"] == first["trials"] + sd.MUTATION_CHILDREN
    rows = _trials_rows(db)
    assert rows[1].payload["runs"] == 2 and set(rows[1].payload["hashes"]) > first_hashes
    assert len(set(rows[1].payload["hashes"]) - first_hashes) == sd.MUTATION_CHILDREN
    # Every candidate in the second run was charged the cumulative count.
    run_rows = db.scalars(select(AuditLog).where(AuditLog.event_type == "strategy_discovery",
                                                 AuditLog.action == "run").order_by(AuditLog.id)).all()
    assert len(run_rows) == 2
    assert run_rows[1].payload["mutation"]["trials_cumulative"] == second["trials"]
    assert run_rows[1].payload["mutation"]["stale_runs"] in (0, 1, 2)
    assert "children" in run_rows[1].message and "trials ever" in run_rows[1].message


def test_mutation_pauses_after_three_stale_runs_and_honours_the_setting(db):  # noqa: F811
    frames = _frames()
    library = [c for c in sd.candidate_library(frames) if c["family"] == "down_streak"]
    groups = sd.universes(frames.keys())

    def step(**overrides):
        result = sd.run_discovery(frames, frames.keys(), library=library)
        with patch.multiple(settings, **overrides):
            return sd.evolution_step(db, result, frames, groups=groups, seen_hashes=set())

    assert step(strategy_discovery_mutation_enabled=False) == {"status": "disabled", "stale_runs": 0}
    write_audit_log(db, event_type="strategy_discovery", action="run", status="complete", message="x",
                    payload={"mutation": {"status": "complete", "improved": False, "stale_runs": 2}})
    with patch.object(sd, "MUTATION_CHILDREN", 4):
        ran = step(strategy_discovery_mutation_budget_seconds=600)
    assert ran["status"] == "complete" and ran["stale_runs"] in (0, 3) and ran["budget_seconds"] == 600
    write_audit_log(db, event_type="strategy_discovery", action="run", status="complete", message="x",
                    payload={"mutation": {"status": "complete", "improved": False, "stale_runs": 3}})
    skipped = step(strategy_discovery_mutation_enabled=True)
    assert skipped["status"] == "skipped" and skipped["stale_runs"] == 0 and "3 runs" in skipped["reason"]


def test_admission_records_lineage(db):  # noqa: F811
    from tests.test_strategy_discovery import _row as library_row

    lineage = {"parent_id": "p1", "operator": "shift_threshold", "seed": 9, "generation": 1, "root_id": "p1",
               "improved_on_parent": True}
    child = library_row("c1", "explore")
    child["lineage"] = lineage
    outcome = sd.admit_survivors(db, {"candidates": [library_row("p1", "rejected"), child]},
                                 now=datetime(2026, 11, 8, 15, tzinfo=UTC))
    assert outcome["admitted"] == ["rule_c1"]
    strategy = db.scalars(select(Strategy).where(Strategy.strategy_type == "rule_c1")).one()
    assert strategy.parameters["discovery"]["lineage"] == lineage
    audit = db.scalars(select(AuditLog).where(AuditLog.event_type == "strategy_discovery",
                                              AuditLog.action == "admit")).one()
    assert audit.payload["lineage"] == lineage and audit.payload["slug"] == "rule_c1"
    assert sd.candidate_spec({**child, "lineage": lineage})["lineage"] == lineage
