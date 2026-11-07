"""Mutation operators on the discovery rule language: how the bot grows its own library.

Strategy discovery (``strategy_discovery``) tests a fixed library of
hypotheses against a gate fixed in advance.  This module lets the library
evolve: every weekly run takes the candidates that came closest to the gate
as parents and produces children by one small, deterministic edit each.

Operators (``OPERATORS``), each a pure function of the parent and a seeded
random generator:

* ``shift_threshold``: move one condition's threshold by one step on its
  feature's natural grid (``GRIDS``: RSI by 5, distance to an SMA by 1%,
  streaks by 1, Bollinger %B by 0.1, ...), inside the grid's bounds.
* ``add_condition``: add one entry condition from ``ADDABLE`` (a feature the
  rule does not use yet; ``earn_surprise`` only on single stocks).
* ``drop_condition``: remove one condition; a rule keeps at least one entry
  condition and an open-timed rule keeps its gap condition.
* ``swap_exit``: replace the indicator exit with another exit from
  ``EXIT_LIBRARY`` (not for open-timed rules, which have none).
* ``change_universe``: evaluate the same rule on another universe (an
  earnings rule stays on single stocks).

A child records its parent id, the operator and its generation
(``lineage``); its id is the ordinary ``candidate_id`` so the admission path
treats it like any library row.  ``trial_hash`` is the dedup key: the
canonical rule (conditions sorted), the exit plan and the universe, so a
child that reproduces an existing candidate is never charged as a new trial.
``evolve`` produces a batch of distinct, never-tested children from a parent
set, deterministically given the seed.  Everything here is pure; the
evaluation, gate accounting and admission stay in ``strategy_discovery``.
"""
from __future__ import annotations

import hashlib
import json
from typing import Callable, Iterable, Mapping, Optional, Sequence

import numpy as np

from app.services.strategies import rules

# Feature grid: (step, lower bound, upper bound).  Keys are the feature kind
# (the name without its window numbers); booleans (at_low_N / at_high_N) have
# no grid and are never shifted.
GRIDS: dict[str, tuple[float, float, float]] = {
    "rsi": (5, 5, 95),
    "dist_sma": (0.01, -0.5, 0.5),
    "ret": (0.01, -0.5, 0.5),
    "down_streak": (1, 1, 10),
    "up_streak": (1, 1, 10),
    "bb_pctb": (0.1, -0.5, 1.5),
    "ibs": (0.1, 0.0, 1.0),
    "gap": (0.0025, -0.1, 0.1),
    "atr_pct": (0.005, 0.0, 0.2),
    "vol_ratio": (0.1, 0.2, 3.0),
    "tdom": (1, 1, 23),
    "tdays_left": (1, 0, 22),
    "dow": (1, 0, 4),
    "earn_surprise": (5, -50, 100),
}
# Entry conditions a child may gain.  One per (feature, direction) so a child
# differs from its parent by one readable idea.
ADDABLE: tuple[dict, ...] = tuple({"f": f, "op": op, "v": v} for f, op, v in (
    ("dist_sma_200", ">", 0), ("dist_sma_50", ">", 0), ("dist_sma_50", "<", 0),
    ("rsi_2", "<", 10), ("rsi_14", "<", 30), ("rsi_14", ">", 50),
    ("ret_1", "<", 0), ("ret_1", ">", 0), ("ret_5", "<", -0.03), ("ret_20", ">", 0),
    ("down_streak", ">=", 2), ("up_streak", ">=", 2),
    ("ibs", "<", 0.2), ("ibs", ">", 0.5), ("gap", "<", 0),
    ("bb_pctb_20", "<", 0.2), ("atr_pct_14", "<", 0.03), ("atr_pct_14", ">", 0.02),
    ("vol_ratio_20_100", "<", 1.0), ("vol_ratio_20_100", ">", 1.0),
    ("at_low_10", "==", 1), ("at_high_50", "==", 1), ("dow", "<", 2), ("tdays_left", "<=", 3),
    ("earn_surprise", ">", 0),
))
EXIT_LIBRARY: dict[str, list[dict]] = {
    "first_up_close": [{"f": "ret_1", "op": ">", "v": 0}],
    "close_above_sma5": [{"f": "dist_sma_5", "op": ">", "v": 0}],
    "close_above_sma10": [{"f": "dist_sma_10", "op": ">", "v": 0}],
    "pctb_mid": [{"f": "bb_pctb_20", "op": ">", "v": 0.5}],
    "rsi2_recovered": [{"f": "rsi_2", "op": ">", "v": 65}],
    "ten_day_high": [{"f": "at_high_10", "op": "==", "v": 1}],
    "below_sma50": [{"f": "dist_sma_50", "op": "<", "v": 0}],
    "time_stop_only": [],
}
UNIVERSES = ("index_etfs", "sector_etfs", "single_stocks")
SINGLE_STOCK_FEATURES = frozenset({"earn_surprise"})
MAX_ATTEMPTS_PER_CHILD = 25


# ---------------------------------------------------------------- hashing

def canonical_rule(rule: Mapping) -> dict:
    """The rule with its conditions sorted, so condition order never makes a new trial."""
    def sort(conditions):
        return sorted(({"f": c["f"], "op": c["op"], "v": c["v"]} for c in conditions),
                      key=lambda c: (c["f"], c["op"], float(c["v"])))

    out = {"entry": sort(rule.get("entry", [])), "exit": sort(rule.get("exit", []))}
    if rules.timing(rule) != "close":
        out["timing"] = rules.timing(rule)
    return out


def trial_hash(rule: Mapping, plan: Mapping, universe: str) -> str:
    payload = {"rule": canonical_rule(rule), "universe": universe,
               "plan": {k: plan[k] for k in ("stop_loss_pct", "take_profit_pct", "max_holding_sessions")}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def candidate_hash(candidate: Mapping) -> str:
    return trial_hash(candidate["rule"], candidate, candidate["universe"])


# ---------------------------------------------------------------- operators

def _kind(name: str) -> str:
    match = rules._FEATURE.match(name)
    return match["name"] if match else name


def grid_for(name: str) -> Optional[tuple[float, float, float]]:
    return GRIDS.get(_kind(name))


def _snap(value: float, step: float) -> float:
    snapped = round(round(value / step) * step, 6)
    return int(snapped) if float(step).is_integer() else snapped


def _conditions(rule: Mapping) -> list[tuple[str, int]]:
    """(section, index) of every condition."""
    return [*(("entry", i) for i in range(len(rule.get("entry", [])))),
            *(("exit", i) for i in range(len(rule.get("exit", []))))]


def _copy(rule: Mapping) -> dict:
    out = {"entry": [dict(c) for c in rule.get("entry", [])], "exit": [dict(c) for c in rule.get("exit", [])]}
    if rules.timing(rule) != "close":
        out["timing"] = rules.timing(rule)
    return out


def shift_threshold(candidate: Mapping, rng: np.random.Generator, context: Mapping) -> Optional[dict]:
    rule = _copy(candidate["rule"])
    slots = [(section, i) for section, i in _conditions(rule) if grid_for(rule[section][i]["f"])]
    if not slots:
        return None
    for section, i in _shuffled(slots, rng):
        condition = rule[section][i]
        step, low, high = grid_for(condition["f"])
        current = float(condition["v"])
        for direction in _shuffled([1, -1], rng):
            value = _snap(min(max(current + direction * step, low), high), step)
            if abs(float(value) - current) > step / 2:
                condition["v"] = value
                return {"rule": rule, "universe": candidate["universe"]}
    return None


def add_condition(candidate: Mapping, rng: np.random.Generator, context: Mapping) -> Optional[dict]:
    rule = _copy(candidate["rule"])
    used = {c["f"] for c in [*rule["entry"], *rule["exit"]]}
    pool = [c for c in ADDABLE if c["f"] not in used
            and (candidate["universe"] == "single_stocks" or c["f"] not in SINGLE_STOCK_FEATURES)]
    if not pool:
        return None
    rule["entry"] = [*rule["entry"], dict(pool[int(rng.integers(len(pool)))])]
    return {"rule": rule, "universe": candidate["universe"]}


def drop_condition(candidate: Mapping, rng: np.random.Generator, context: Mapping) -> Optional[dict]:
    rule = _copy(candidate["rule"])
    open_timed = rules.timing(rule) == "open"
    slots = [(section, i) for section, i in _conditions(rule)
             if not (section == "entry" and len(rule["entry"]) == 1)
             and not (open_timed and rule[section][i]["f"] == "gap"
                      and sum(c["f"] == "gap" for c in rule["entry"]) == 1)]
    if not slots:
        return None
    section, i = slots[int(rng.integers(len(slots)))]
    del rule[section][i]
    return {"rule": rule, "universe": candidate["universe"]}


def swap_exit(candidate: Mapping, rng: np.random.Generator, context: Mapping) -> Optional[dict]:
    rule = _copy(candidate["rule"])
    if rules.timing(rule) == "open":
        return None
    current = canonical_rule(rule)["exit"]
    library = context.get("exits") or EXIT_LIBRARY
    options = [name for name, conditions in library.items()
               if canonical_rule({"entry": [], "exit": conditions})["exit"] != current]
    if not options:
        return None
    rule["exit"] = [dict(c) for c in library[options[int(rng.integers(len(options)))]]]
    return {"rule": rule, "universe": candidate["universe"]}


def change_universe(candidate: Mapping, rng: np.random.Generator, context: Mapping) -> Optional[dict]:
    groups = context.get("groups") or {}
    needs_stocks = any(c["f"] in SINGLE_STOCK_FEATURES
                       for c in [*candidate["rule"].get("entry", []), *candidate["rule"].get("exit", [])])
    options = [u for u in UNIVERSES if u != candidate["universe"] and groups.get(u)
               and (u == "single_stocks" or not needs_stocks)]
    if not options:
        return None
    return {"rule": _copy(candidate["rule"]), "universe": options[int(rng.integers(len(options)))]}


OPERATORS: dict[str, Callable[[Mapping, np.random.Generator, Mapping], Optional[dict]]] = {
    "shift_threshold": shift_threshold,
    "add_condition": add_condition,
    "drop_condition": drop_condition,
    "swap_exit": swap_exit,
    "change_universe": change_universe,
}


def _shuffled(items: Sequence, rng: np.random.Generator) -> list:
    order = rng.permutation(len(items))
    return [items[int(i)] for i in order]


# ---------------------------------------------------------------- children

def mutate(candidate: Mapping, operator: str, seed: int, *, groups: Optional[Mapping[str, Sequence[str]]] = None,
           exits: Optional[Mapping[str, list]] = None) -> Optional[dict]:
    """One child of ``candidate`` by ``operator``; None when the operator does not apply.

    Deterministic given ``seed``.  The child carries the parent's exit plan
    and family, its own ``id`` / ``hash`` and a ``lineage`` record.
    """
    from app.services.strategy_discovery import candidate_id

    rng = np.random.default_rng(seed)
    edit = OPERATORS[operator](candidate, rng, {"groups": groups or {}, "exits": exits})
    if edit is None:
        return None
    rule, universe = edit["rule"], edit["universe"]
    try:
        rules.validate_rule(rule)
    except ValueError:
        return None
    plan = {k: candidate[k] for k in ("stop_loss_pct", "take_profit_pct", "max_holding_sessions")}
    symbols = list((groups or {}).get(universe) or candidate["symbols"]) if universe != candidate["universe"] \
        else list(candidate["symbols"])
    parent_lineage = candidate.get("lineage") or {}
    child = {"id": candidate_id(rule, plan, universe), "family": candidate["family"],
             "hypothesis": f"Mutation ({operator}) of {candidate['id']}: {candidate.get('hypothesis', '')}".strip(),
             "rule": rule, "universe": universe, "symbols": symbols, **plan,
             "lineage": {"parent_id": candidate["id"], "operator": operator, "seed": int(seed),
                         "generation": int(parent_lineage.get("generation", 0)) + 1,
                         "root_id": parent_lineage.get("root_id", candidate["id"])}}
    child["hash"] = candidate_hash(child)
    return child


def evolve(parents: Sequence[Mapping], *, count: int, seed: int, seen: Iterable[str] = (),
           groups: Optional[Mapping[str, Sequence[str]]] = None,
           operators: Sequence[str] = tuple(OPERATORS)) -> list[dict]:
    """Up to ``count`` distinct children of ``parents`` whose hash is not in ``seen``.

    Parents are visited round-robin and the operator is drawn from the seeded
    generator, so the same parents, seed and ``seen`` set give the same
    children.  Stops early when the parents are exhausted.
    """
    parents = [p for p in parents if p]
    if not parents or count <= 0:
        return []
    rng = np.random.default_rng(seed)
    taken = set(seen)
    children: list[dict] = []
    attempts, limit = 0, count * MAX_ATTEMPTS_PER_CHILD
    while len(children) < count and attempts < limit:
        parent = parents[attempts % len(parents)]
        operator = operators[int(rng.integers(len(operators)))]
        attempts += 1
        child = mutate(parent, operator, int(rng.integers(2 ** 31 - 1)), groups=groups)
        if child is None or child["hash"] in taken:
            continue
        taken.add(child["hash"])
        children.append(child)
    return children
