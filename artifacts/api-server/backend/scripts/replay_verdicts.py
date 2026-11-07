"""Re-judge past and replayed changes under three forward rules, and deflate past adoptions (experiments 2 and 3).

Experiment 2: every change is judged three ways.

(i)   **fixed_20**, the engine's current rule (``experiment_engine.judge``):
      from 20 closed trades after the change, re-evaluated at every trade up
      to 60: revert when the mean is more than 10 bps below the mean of the
      20 trades before the change, keep when it is not lower, otherwise watch
      and close as inconclusive (kept) at 60.  The cause share is unknown in
      a replay, which the rule treats as "no rarer".
(ii)  **e_process**, ``verdict_stats.e_process_verdict``: betting e-processes
      on the paired difference per decision (candidate minus the value it
      replaced, SPY-adjusted except market-timing gates, averaged per entry
      date), harm at alpha 0.10, improvement at the e-LOND level of the
      change's place in the adoption stream, 120-trade cap with the lower
      confidence bound at -10 bps.
(iii) **obf_110**, a group-sequential design sized in the research for 80%
      power at 50 bps: 110 trades, 5 equally spaced looks, O'Brien-Fleming
      boundaries ``C sqrt(K / k)`` on the clustered z statistic of the same
      paired differences (revert below ``-C_harm sqrt(5/k)``, one-sided 0.10;
      confirm above ``C_imp sqrt(5/k)``, one-sided 0.05; ``C`` calibrated by
      Monte Carlo); neither by 110 = keep.

(iv)  **e_catoni**, a variant measured alongside (not the engine's judge): the
      same harm / e-LOND / cap rules on the Catoni-style process of Wang &
      Ramdas (2023) from the first trade, with sigma = the class's measured
      paired-difference std bound, so its growth scales with sigma instead of
      the loss bound B.

Three streams:

A. **The ledger** (``--database-url``): every ``change_adopted`` /
   ``change_verdict`` row.
B. **Parametric, at the measured inputs**: changes with a known true effect
   (-50 to +50 bps per trade), whose trades are bootstrapped by entry-date
   cluster from the replay's own paired differences (demeaned, shifted by the
   effect) and raw trade returns; per effect, the share reverted / kept /
   confirmed and the trades to a decision.  This is where false-revert and
   false-keep rates come from.
C. **Replayed adoptions**: at the start of every second year from 2008, each
   (strategy, template) is decided on the history before that date (the
   engine's rule without its drawdown check: in each of three windows, all,
   last 10 and last 5 years, at least 30 trades and expectancy and total
   profit not lower; largest minimum gain wins), and the winner is judged
   forward by all three rules.  The realised gain over everything after the
   adoption is the reference.

Experiment 3: for every adoption (the ledger's, and every adoption the
engine's rule makes on today's full-history replay), the multiplicity hurdle
(``verdict_stats.multiplicity_hurdle``) with ``trials`` = every candidate ever
tried for that strategy; which would not have passed.

**Pre-registered default for ``experiment_multiplicity_hurdle``** (fixed
before the first run): on, unless more than half of the adoptions the hurdle
blocks on today's replay are confirmed improvements by the full-history
paired test (the improvement e-process at alpha 0.10 over all of the
candidate-minus-live decisions), in which case the hurdle is blocking real
improvements and defaults off.  No blocked adoption = on (it costs nothing).

Usage (from artifacts/api-server/backend), after ``measure_verdict_inputs.py --cache``:

    PYTHONPATH=. python scripts/replay_verdicts.py --cache <pkl> --inputs <json> --output <json> \\
        --report ../../../reports/verdict-replay-<date>.md [--database-url <url>]
"""
from __future__ import annotations

import argparse
import json
import math
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.services import verdict_stats as vs  # noqa: E402

SEED = 20261107
EFFECTS = (-50, -25, -10, 0, 10, 25, 50)
REPLICATIONS = 2000
FIXED_MIN, FIXED_MAX, FIXED_MARGIN = 20, 60, 10.0
OBF_TRADES, OBF_LOOKS = 110, 5
OBF_HARM_ALPHA, OBF_IMPROVE_ALPHA = 0.10, 0.05
ADOPTION_YEARS = tuple(range(2008, 2025, 2))
MIN_WINDOW_TRADES = 30
SIM_ELOND_INDEX = 5          # the parametric stream judges each change as the 5th adoption with no earlier discovery
KEEP = ("working", "inconclusive")
RULES = ("fixed_20", "e_process", "obf_110", "e_catoni")


# ------------------------------------------------------------------ judges

def fixed_20(before_bps: np.ndarray, after_bps: np.ndarray) -> dict:
    """The engine's rule replayed trade by trade (cause share unknown = "no rarer")."""
    if len(before_bps) == 0:
        return {"verdict": "unavailable", "trades": 0}
    base = float(np.mean(before_bps))
    for n in range(FIXED_MIN, min(FIXED_MAX, len(after_bps)) + 1):
        mean = float(np.mean(after_bps[:n]))
        if mean < base - FIXED_MARGIN:
            return {"verdict": "not_helping", "trades": n}
        if mean >= base:
            return {"verdict": "working", "trades": n}
    if len(after_bps) >= FIXED_MAX:
        return {"verdict": "inconclusive", "trades": FIXED_MAX}
    return {"verdict": "watching", "trades": int(len(after_bps))}


def obf_constant(alpha: float, looks: int = OBF_LOOKS, sims: int = 200_000, seed: int = SEED) -> float:
    """C with P(max_k Z_k / sqrt(K/k) >= C) = alpha for a standard Brownian motion at K equal looks."""
    rng = np.random.default_rng(seed)
    z = np.cumsum(rng.standard_normal((sims, looks)), axis=1) / np.sqrt(np.arange(1, looks + 1))
    stat = np.max(z / np.sqrt(looks / np.arange(1, looks + 1)), axis=1)
    return float(np.quantile(stat, 1 - alpha))


C_HARM = obf_constant(OBF_HARM_ALPHA)
C_IMPROVE = obf_constant(OBF_IMPROVE_ALPHA)


def obf_110(clusters: list) -> dict:
    """O'Brien-Fleming, 5 looks at 22, 44, ... 110 trades, z on the entry-date cluster means."""
    values = np.array([c.value_bps for c in clusters], dtype=float)
    trades = np.cumsum([c.trades for c in clusters]) if clusters else np.zeros(0)
    for k in range(1, OBF_LOOKS + 1):
        need = OBF_TRADES * k / OBF_LOOKS
        idx = np.flatnonzero(trades >= need)
        if not idx.size:
            return {"verdict": "watching", "trades": int(trades[-1]) if len(trades) else 0}
        seen = values[:idx[0] + 1]
        sd = seen.std(ddof=1) if seen.size > 1 else 0.0
        z = seen.mean() / (sd / math.sqrt(seen.size)) if sd > 0 else 0.0
        scale = math.sqrt(OBF_LOOKS / k)
        if z <= -C_HARM * scale:
            return {"verdict": "not_helping", "trades": int(trades[idx[0]]), "look": k}
        if z >= C_IMPROVE * scale:
            return {"verdict": "working", "trades": int(trades[idx[0]]), "look": k}
    return {"verdict": "inconclusive", "trades": OBF_TRADES, "look": OBF_LOOKS}


def e_process(clusters: list, cls: str, inputs: dict, level: float) -> dict:
    out = vs.e_process_verdict(clusters, bound_bps=float(inputs["loss_bound_bps"][cls]),
                               sigma_bps=float(inputs["sigma_bps"][cls]), improvement_level=level,
                               rho=float(inputs.get("rho") or 0.0))
    return {"verdict": out["verdict"], "trades": out.get("decided_at_trades") or out["trades"],
            "breach": out["bound_breach"], "kept_at_cap": out.get("kept_at_cap"), "lcb_bps": out.get("lcb_bps")}


def e_catoni(clusters: list, cls: str, inputs: dict, level: float) -> dict:
    """Variant (not the engine's judge): the Catoni-style process from the first trade, sigma = the class's paired
    difference std bound (``paired_sigma_bps``), the same harm / e-LOND / cap rules."""
    sigma = float((inputs.get("paired_sigma_bps") or {}).get(cls) or inputs["sigma_bps"][cls])
    out = vs.e_process_verdict(clusters, bound_bps=0.0, sigma_bps=sigma, improvement_level=level,
                               rho=float(inputs.get("rho") or 0.0), process="catoni")
    return {"verdict": out["verdict"], "trades": out.get("decided_at_trades") or out["trades"],
            "breach": None, "kept_at_cap": out.get("kept_at_cap"), "lcb_bps": out.get("lcb_bps")}


def action(verdict: str) -> str:
    if verdict in KEEP:
        return "keep"
    if verdict == "not_helping":
        return "revert"
    return "undecided"


# ------------------------------------------------------------ data helpers

def setup_key(strategy: str, setup: dict) -> tuple:
    return (strategy, json.dumps(setup, sort_keys=True, default=str))


def paired_frame(base: pd.DataFrame, cand: pd.DataFrame, *, adjust: bool) -> pd.DataFrame:
    """Per decision over the union (``verdict_stats.paired_backtest``), with ``decision_date`` as a timestamp."""
    frame = vs.paired_backtest(base, cand, adjust=adjust)
    return frame.assign(decision_date=pd.to_datetime(frame["decision_date"]))


def clusters_of(frame: pd.DataFrame) -> list:
    return vs.cluster_by_entry_date(vs.Observation(float(v), e, x) for v, e, x in
                                    zip(frame["value_bps"], frame["entry_date"], frame["exit_date"]))


def by_exit(trades: pd.DataFrame) -> pd.DataFrame:
    return trades.sort_values(["exit_date", "entry_date"]).reset_index(drop=True)


# ------------------------------------------------------- B. parametric stream

def cluster_pool(frame: pd.DataFrame) -> list[np.ndarray]:
    """Entry-date clusters of demeaned values (the empirical same-date dependence and tails)."""
    frame = frame.assign(value_bps=frame["value_bps"] - frame["value_bps"].mean())
    return [g["value_bps"].to_numpy(float) for _, g in frame.groupby("entry_date", sort=False)]


def draw_stream(pool: list[np.ndarray], rng, n_trades: int, effect: float) -> list:
    out, total, day = [], 0, 0
    while total < n_trades:
        values = pool[int(rng.integers(len(pool)))] + effect
        day += 1
        stamp = f"{day:06d}"
        out.extend(vs.Observation(float(v), stamp, stamp) for v in values)
        total += len(values)
    return vs.cluster_by_entry_date(out)


def simulate_class(paired_pool: list[np.ndarray], raw_pool: list[np.ndarray], cls: str, inputs: dict, *,
                   replications: int, rng) -> list[dict]:
    level = vs.elond_level(SIM_ELOND_INDEX, 0)
    rows = []
    for effect in EFFECTS:
        tallies = {name: {"keep": 0, "revert": 0, "undecided": 0, "confirmed": 0, "trades": []}
                   for name in RULES}
        breaches = 0
        for _ in range(replications):
            stream = draw_stream(paired_pool, rng, vs.CAP_TRADES + 40, effect)
            before = np.concatenate([raw_pool[int(rng.integers(len(raw_pool)))] for _ in range(40)])[:FIXED_MIN]
            after = np.concatenate([raw_pool[int(rng.integers(len(raw_pool)))] for _ in range(120)])[:FIXED_MAX] + effect
            results = {"fixed_20": fixed_20(before, after), "e_process": e_process(stream, cls, inputs, level),
                       "obf_110": obf_110(stream), "e_catoni": e_catoni(stream, cls, inputs, level)}
            breaches += bool(results["e_process"].get("breach"))
            for name, result in results.items():
                tally = tallies[name]
                tally[action(result["verdict"])] += 1
                tally["confirmed"] += result["verdict"] == "working"
                if action(result["verdict"]) != "undecided":
                    tally["trades"].append(result["trades"])
        for name, tally in tallies.items():
            rows.append({"class": cls, "effect_bps": effect, "rule": name,
                         "revert": round(tally["revert"] / replications, 4),
                         "keep": round(tally["keep"] / replications, 4),
                         "confirmed": round(tally["confirmed"] / replications, 4),
                         "undecided": round(tally["undecided"] / replications, 4),
                         "median_trades": float(np.median(tally["trades"])) if tally["trades"] else None,
                         "breach_rate": round(breaches / replications, 4) if name == "e_process" else None})
    return rows


def error_rates(rows: list[dict]) -> dict:
    """False revert = P(revert | effect >= 0); false keep = P(keep | effect <= -10); averaged over the effect grid."""
    out = {}
    for cls in sorted({r["class"] for r in rows}):
        for rule in RULES:
            sub = [r for r in rows if r["class"] == cls and r["rule"] == rule]
            good = [r for r in sub if r["effect_bps"] > 0]
            null = [r for r in sub if r["effect_bps"] == 0]
            bad = [r for r in sub if r["effect_bps"] <= -10]
            out[f"{cls}/{rule}"] = {
                "false_revert_null": null[0]["revert"] if null else None,
                "false_revert_good": round(float(np.mean([r["revert"] for r in good])), 4) if good else None,
                "false_keep_bad": round(float(np.mean([r["keep"] for r in bad])), 4) if bad else None,
                "revert_at_minus_50": next((r["revert"] for r in sub if r["effect_bps"] == -50), None),
                "keep_at_plus_50": next((r["keep"] for r in sub if r["effect_bps"] == 50), None)}
    return out


# ------------------------------------------------------ C. replayed adoptions

def window_stats(trades: pd.DataFrame, start, end) -> dict:
    dates = pd.to_datetime(trades["decision_date"])
    sub = trades.loc[(dates < end) & ((dates >= start) if start is not None else True)]
    n = len(sub)
    mean = float(sub["net_return"].mean() * 10_000) if n else None
    return {"trades": n, "expectancy_bps": mean, "total_bps": float(sub["net_return"].sum() * 10_000) if n else 0.0}


def decide_before(rows: dict) -> str | None:
    """The engine's rule without the drawdown check, on windows ending at the adoption date."""
    base = rows["baseline"]
    best, best_gain = None, None
    for label, windows in rows.items():
        if label == "baseline":
            continue
        gains = []
        for name, cand in windows.items():
            b = base[name]
            if cand["trades"] < MIN_WINDOW_TRADES or cand["expectancy_bps"] is None or b["expectancy_bps"] is None:
                gains = None
                break
            gain = cand["expectancy_bps"] - b["expectancy_bps"]
            if gain < 0 or cand["total_bps"] < b["total_bps"]:
                gains = None
                break
            gains.append(gain)
        if gains and (best_gain is None or min(gains) > best_gain):
            best, best_gain = label, min(gains)
    return best


def replay_stream(cache: dict, inputs: dict) -> list[dict]:
    from app.services import experiment_engine as engine

    adoptions = []
    for year in ADOPTION_YEARS:
        cut = pd.Timestamp(f"{year}-01-01")
        windows = {"all": None, "10y": cut - pd.DateOffset(years=10), "5y": cut - pd.DateOffset(years=5)}
        for experiment in cache["experiments"]:
            strategy = experiment["strategy_type"]
            base = cache["trades"][setup_key(strategy, experiment["setups"]["baseline"])]
            rows = {}
            for label, setup in experiment["setups"].items():
                trades = cache["trades"][setup_key(strategy, setup)]
                rows[label] = {name: window_stats(trades, start, cut) for name, start in windows.items()}
            label = decide_before(rows)
            if label is None:
                continue
            cand = cache["trades"][setup_key(strategy, experiment["setups"][label])]
            adjust = vs.adjusts_for_spy(experiment["template"])
            pairs = paired_frame(base, cand, adjust=adjust)
            after_pairs = pairs.loc[pairs["decision_date"] >= cut]
            if len(after_pairs) < 10:
                continue
            base_sorted = by_exit(base)
            before_raw = base_sorted.loc[pd.to_datetime(base_sorted["exit_date"]) < cut, "net_return"].to_numpy()[-FIXED_MIN:] * 10_000
            cand_sorted = by_exit(cand)
            after_raw = cand_sorted.loc[pd.to_datetime(cand_sorted["decision_date"]) >= cut, "net_return"].to_numpy() * 10_000
            adoptions.append({"year": year, "strategy_type": strategy, "template": experiment["template"],
                              "candidate": label, "class": vs.strategy_class(strategy), "adjusted_for_spy": adjust,
                              "clusters": clusters_of(after_pairs), "before_raw": before_raw, "after_raw": after_raw,
                              "realised_gain_bps": round(float(after_pairs["value_bps"].mean()), 2),
                              "realised_decisions": int(len(after_pairs)),
                              "template_kind": engine.TEMPLATE_BY_NAME[experiment["template"]].kind})
    adoptions.sort(key=lambda a: (a["year"], a["strategy_type"], a["template"]))
    discoveries = 0
    out = []
    for j, adoption in enumerate(adoptions, start=1):
        level = vs.elond_level(j, discoveries)
        e = e_process(adoption["clusters"], adoption["class"], inputs, level)
        discoveries += e["verdict"] == "working"
        verdicts = {"fixed_20": fixed_20(adoption["before_raw"], adoption["after_raw"]), "e_process": e,
                    "obf_110": obf_110(adoption["clusters"]),
                    "e_catoni": e_catoni(adoption["clusters"], adoption["class"], inputs, level)}
        out.append({k: v for k, v in adoption.items() if k not in ("clusters", "before_raw", "after_raw")}
                   | {"elond_index": j, "elond_level": level, "verdicts": verdicts})
    return out


def stream_summary(stream: list[dict]) -> dict:
    rules = RULES
    out = {"adoptions": len(stream)}
    truth_good = [a for a in stream if a["realised_gain_bps"] >= 0]
    truth_bad = [a for a in stream if a["realised_gain_bps"] <= -FIXED_MARGIN]
    for rule in rules:
        acts = [action(a["verdicts"][rule]["verdict"]) for a in stream]
        out[rule] = {"keep": acts.count("keep"), "revert": acts.count("revert"), "undecided": acts.count("undecided"),
                     "false_revert": (round(sum(action(a["verdicts"][rule]["verdict"]) == "revert" for a in truth_good)
                                            / len(truth_good), 4) if truth_good else None),
                     "false_keep": (round(sum(action(a["verdicts"][rule]["verdict"]) == "keep" for a in truth_bad)
                                          / len(truth_bad), 4) if truth_bad else None),
                     "median_trades": float(np.median([a["verdicts"][rule]["trades"] for a in stream
                                                       if action(a["verdicts"][rule]["verdict"]) != "undecided"]))
                     if any(action(a["verdicts"][rule]["verdict"]) != "undecided" for a in stream) else None}
    agreement = {}
    for i, a in enumerate(rules):
        for b in rules[i + 1:]:
            both = [(action(s["verdicts"][a]["verdict"]), action(s["verdicts"][b]["verdict"])) for s in stream]
            decided = [(x, y) for x, y in both if x != "undecided" and y != "undecided"]
            agreement[f"{a} vs {b}"] = {"both_decided": len(decided),
                                        "agree": round(sum(x == y for x, y in decided) / len(decided), 4) if decided else None}
    out["agreement"] = agreement
    out["truth_good"], out["truth_bad"] = len(truth_good), len(truth_bad)
    return out


# ---------------------------------------------------------- A. the ledger

def ledger_rows(database_url: str | None) -> dict:
    if not database_url:
        return {"reachable": False, "reason": "no --database-url"}
    from app.core.config import settings

    settings.database_url = database_url
    try:
        from app.db.session import SessionLocal
        from app.services import experiment_engine as engine

        db = SessionLocal()
    except Exception as exc:
        return {"reachable": False, "reason": f"{exc.__class__.__name__}: {exc}"}
    try:
        changes = engine.adopted_changes(db)
        state = vs.assign_indices({"alpha": vs.IMPROVEMENT_FDR_ALPHA, "changes": {}}, changes)
        inputs = vs.current_inputs(db)
        spy = engine._spy_frame(db)
        from app.services.trade_attribution import current_attributions

        rows = []
        for change in changes:
            scope = None if change["strategy_type"] == engine.BOOK else [change["strategy_type"]]
            after_rows = [r for r in current_attributions(db, since=engine._parse(change["adopted_at"]),
                                                          sources=engine.SOURCES, strategy_types=scope)]
            e = engine.e_process_judgement(db, change, after_rows=after_rows, now=datetime.now(timezone.utc),
                                           state=state, inputs=inputs, spy=spy)
            rows.append({"change_id": change["change_id"], "strategy_type": change["strategy_type"],
                         "template": change["template"], "fixed_20": change.get("verdict") or "watching",
                         "e_process": e.get("verdict"), "trades": e.get("trades")})
        db.rollback()  # read-only: nothing re-judged here is written
        return {"reachable": True, "changes": rows,
                "verdict_rows": sum(len(c.get("verdicts") or []) for c in changes)}
    except Exception as exc:
        db.rollback()
        return {"reachable": False, "reason": f"{exc.__class__.__name__}: {exc}"}
    finally:
        db.close()


# ---------------------------------------------------------- experiment 3

def deflate(cache: dict, ledger: dict) -> dict:
    """The multiplicity hurdle on the paired gain of every adoption today's full-history replay makes.

    Trials = every candidate tried for the strategy across its templates;
    the trial Sharpes are all those candidates' paired Sharpes.
    """
    tried: dict[str, int] = {}
    sharpes: dict[str, list] = {}
    paired_by: dict[tuple, tuple] = {}
    for experiment in cache["experiments"]:
        strategy = experiment["strategy_type"]
        base = cache["trades"][setup_key(strategy, experiment["setups"]["baseline"])]
        adjust = vs.adjusts_for_spy(experiment["template"])
        for label, setup in experiment["setups"].items():
            if label == "baseline":
                continue
            tried[strategy] = tried.get(strategy, 0) + 1
            frame = paired_frame(base, cache["trades"][setup_key(strategy, setup)], adjust=adjust)
            stats = vs.paired_stats(frame)
            paired_by[(strategy, experiment["template"], label)] = (frame, stats)
            if stats.get("sharpe") is not None:
                sharpes.setdefault(strategy, []).append(stats["sharpe"])
    rows = []
    for experiment in cache["experiments"]:
        label = experiment["decision"]["adopted"]
        if not label:
            continue
        strategy = experiment["strategy_type"]
        frame, stats = paired_by[(strategy, experiment["template"], label)]
        hurdle = vs.multiplicity_hurdle(stats, trials=tried[strategy], trial_sharpes=sharpes.get(strategy), label=label)
        clusters = clusters_of(frame)
        values = [c.value_bps for c in clusters]
        cls = vs.strategy_class(strategy)
        bound = float(cache["inputs"]["loss_bound_bps"][cls])
        breach = vs.first_breach(values, bound)
        known = vs.known_before_open(clusters)
        usable = values if breach is None else values[:breach]
        e_imp = (vs.betting_e_process(usable, bound_bps=bound, side="greater", known=known[:len(usable)])
                 if usable else np.array([1.0]))
        if breach is not None:
            e_imp = vs.catoni_e_process(values, sigma_bps=float(cache["inputs"]["sigma_bps"][cls]), side="greater")
        confirmed = bool(np.max(e_imp) >= 1.0 / 0.10) if len(e_imp) else False
        rows.append({"strategy_type": strategy, "template": experiment["template"], "adopted": label,
                     "baseline_expectancy_bps": experiment["rows"]["baseline"]["all"]["expectancy_bps"],
                     "adopted_expectancy_bps": experiment["rows"][label]["all"]["expectancy_bps"],
                     "gain_min_bps": experiment["decision"]["eligible"].get(label),
                     "trades": experiment["rows"][label]["all"]["trades"], "trials": hurdle["trials"],
                     "t_stat": hurdle["t_stat"], "deflated_sharpe": hurdle["deflated_sharpe"],
                     "passed": hurdle["passed"], "paired_decisions": stats.get("decisions"),
                     "paired_mean_bps": stats.get("mean_bps"), "paired_std_bps": stats.get("std_bps"),
                     "paired_confirmed": confirmed, "paired_max_e": round(float(np.max(e_imp)), 3) if len(e_imp) else None})
    blocked = [r for r in rows if r["passed"] is False]
    confirmed_blocked = [r for r in blocked if r["paired_confirmed"]]
    default = not blocked or len(confirmed_blocked) <= len(blocked) / 2
    return {"adoptions": rows, "blocked": len(blocked), "confirmed_among_blocked": len(confirmed_blocked),
            "trials_by_strategy": tried, "default_on": bool(default),
            "rule": ("on unless more than half of the adoptions the hurdle blocks on today's replay are confirmed "
                     "improvements by the full-history paired improvement e-process at alpha 0.10; none blocked = on"),
            "ledger": ledger.get("changes") if ledger.get("reachable") else None}


# ----------------------------------------------------------------- report

def _p(x) -> str:
    return "n/a" if x is None else f"{100 * float(x):.1f}%"


def _n(x, d=0, sign=False) -> str:
    if x is None:
        return "n/a"
    return f"{float(x):+.{d}f}" if sign else f"{float(x):.{d}f}"


def render(result: dict) -> str:
    inp = result["inputs"]
    day = result["ran_at"][:10]
    lines = [f"# Verdict replay: {day}", "",
             "**Question:** how do the engine's current 20-trade verdict, the always-valid e-process judge "
             "(`app/services/verdict_stats.py`) and a 110-trade O'Brien-Fleming design compare, at the measured "
             "inputs (`reports/verdict-inputs-" + inp["as_of"].replace("-", "") + ".md`), and which past adoptions "
             "survive a multiplicity hurdle? Paper only; nothing here changed a strategy.", "",
             f"Inputs: loss bound {inp['loss_bound_bps']} bps, variance bound {inp['sigma_bps']} bps, rho {inp['rho']}. "
             f"O'Brien-Fleming constants (Monte Carlo, 5 looks): harm {C_HARM:.3f} (one-sided 0.10), improvement "
             f"{C_IMPROVE:.3f} (one-sided 0.05).", ""]
    led = result["ledger"]
    lines += ["## A. The ledger", ""]
    if led.get("reachable"):
        lines.append(f"{len(led['changes'])} adopted changes, {led['verdict_rows']} verdict rows.")
        lines += ["", "| Change | Strategy | Template | fixed_20 | e_process | Trades |", "|---|---|---|---|---|---|"]
        for row in led["changes"]:
            lines.append(f"| {row['change_id']} | {row['strategy_type']} | {row['template']} | {row['fixed_20']} | "
                         f"{row['e_process']} | {row['trades']} |")
    else:
        lines.append(f"Not reachable from where this ran ({led.get('reason')}). The engine's ledger on the host had no "
                     "adoption when the engine audit last looked (reports/engine-audit-20261106.md), so there is "
                     "nothing to re-judge yet; streams B and C stand in. Rerun with `--database-url` on the host.")
    lines += ["", "## B. Known effects at the measured inputs", "",
              f"{result['replications']} changes per effect and class; trades bootstrapped by entry-date cluster from "
              "the replay's own paired differences (demeaned and shifted by the effect) for the e-process and "
              "O'Brien-Fleming rules, and from the raw per-trade returns for the 20-trade rule (which compares the "
              "trades after with the 20 before, unpaired). The effect is the same number of bps on both scales, "
              f"which treats a per-decision paired gain as a per-trade expectancy gain. e-LOND level of the "
              f"{SIM_ELOND_INDEX}th adoption with no earlier confirmation: {vs.elond_level(SIM_ELOND_INDEX, 0):.4f} "
              f"(threshold {1 / vs.elond_level(SIM_ELOND_INDEX, 0):.0f}).", "",
              "| Class | Effect bps | Rule | Revert | Keep | of which confirmed | Undecided | Median trades to decision | Bound breach |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in result["parametric"]:
        lines.append(f"| {r['class']} | {r['effect_bps']:+d} | {r['rule']} | {_p(r['revert'])} | {_p(r['keep'])} | "
                     f"{_p(r['confirmed'])} | {_p(r['undecided'])} | {_n(r['median_trades'])} | "
                     f"{_p(r['breach_rate']) if r['breach_rate'] is not None else ''} |")
    lines += ["", "Implied error rates (false revert of a null change, mean false revert over the positive effects, "
              "mean false keep over the effects of -10 bps or worse):", "",
              "| Class / rule | False revert at 0 | False revert, good | False keep, bad | Revert at -50 | Keep at +50 |",
              "|---|---|---|---|---|---|"]
    for key, e in result["error_rates"].items():
        lines.append(f"| {key} | {_p(e['false_revert_null'])} | {_p(e['false_revert_good'])} | {_p(e['false_keep_bad'])} | "
                     f"{_p(e['revert_at_minus_50'])} | {_p(e['keep_at_plus_50'])} |")
    s = result["stream_summary"]
    lines += ["", "## C. Replayed adoptions", "",
              f"{s['adoptions']} adoptions (every second year from {ADOPTION_YEARS[0]}, each (strategy, template) decided "
              "on the history before the date), judged forward by all three rules; the reference is the realised "
              f"paired gain over everything after the adoption ({s['truth_good']} at or above 0, {s['truth_bad']} at "
              "-10 bps or worse).", "",
              "| Rule | Keep | Revert | Undecided | False revert (realised >= 0) | False keep (realised <= -10) | Median trades |",
              "|---|---|---|---|---|---|---|"]
    for rule in RULES:
        r = s[rule]
        lines.append(f"| {rule} | {r['keep']} | {r['revert']} | {r['undecided']} | {_p(r['false_revert'])} | "
                     f"{_p(r['false_keep'])} | {_n(r['median_trades'])} |")
    lines += ["", "Agreement on the adoptions both rules decided:", ""]
    for pair, a in s["agreement"].items():
        lines.append(f"* {pair}: {_p(a['agree'])} of {a['both_decided']}")
    lines += ["", "<details><summary>Every replayed adoption</summary>", "",
              "| Year | Strategy | Template | Candidate | Realised gain | Decisions | fixed_20 | e_process | obf_110 | e_catoni |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for a in result["stream"]:
        v = a["verdicts"]
        lines.append(f"| {a['year']} | {a['strategy_type']} | {a['template']} | {a['candidate']} | "
                     f"{_n(a['realised_gain_bps'], 1, True)} | {a['realised_decisions']} | "
                     f"{v['fixed_20']['verdict']} ({v['fixed_20']['trades']}) | {v['e_process']['verdict']} "
                     f"({v['e_process']['trades']}) | {v['obf_110']['verdict']} ({v['obf_110']['trades']}) | "
                     f"{v['e_catoni']['verdict']} ({v['e_catoni']['trades']}) |")
    lines += ["", "</details>", ""]
    d = result["deflate"]
    lines += ["## Experiment 3: deflating the adoptions", "",
              "Every adoption the engine's rule makes on today's full-history point-in-time replay (gate and exit "
              "templates, live setups), with the multiplicity hurdle on the paired gain (candidate minus live per "
              "replayed decision, SPY-adjusted except market-timing gates) counting every candidate tried for the "
              "strategy across templates: entry-date clustered t >= 3, or deflated Sharpe > 0.95 of the per-decision "
              "difference with those trials. `Paired confirmed` = the full-history paired improvement e-process "
              "reaches 10 (alpha 0.10).", "",
              "| Strategy | Template | Adopted | Baseline bps | Adopted bps | Min gain bps | Trades | Trials | Paired mean | Paired std | t | DSR | Hurdle | Paired confirmed |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in d["adoptions"]:
        lines.append(f"| {r['strategy_type']} | {r['template']} | {r['adopted']} | {_n(r['baseline_expectancy_bps'], 1, True)} | "
                     f"{_n(r['adopted_expectancy_bps'], 1, True)} | {_n(r['gain_min_bps'], 1)} | {r['trades']} | {r['trials']} | "
                     f"{_n(r['paired_mean_bps'], 1, True)} | {_n(r['paired_std_bps'])} | {_n(r['t_stat'], 2)} | "
                     f"{_n(r['deflated_sharpe'], 3)} | {'pass' if r['passed'] else ('**fail**' if r['passed'] is False else 'n/a')} | "
                     f"{'yes' if r['paired_confirmed'] else 'no'} |")
    lines += ["", f"Blocked by the hurdle: {d['blocked']} of {len(d['adoptions'])}; of those, confirmed improvements by "
              f"the paired test: {d['confirmed_among_blocked']}.", "",
              f"**Pre-registered default:** {d['rule']}. Result: `experiment_multiplicity_hurdle = {d['default_on']}`.", ""]
    lines += result.get("readings", [])
    return "\n".join(lines) + "\n"


def readings(result: dict) -> list[str]:
    er = result["error_rates"]
    out = ["## Readings", ""]
    for cls in ("technical", "reversion"):
        f, e, o = er.get(f"{cls}/fixed_20"), er.get(f"{cls}/e_process"), er.get(f"{cls}/obf_110")
        if not f:
            continue
        out.append(f"* {cls}: the 20-trade rule reverts {_p(f['false_revert_null'])} of null changes and "
                   f"{_p(f['false_revert_good'])} of good ones, and keeps {_p(f['false_keep_bad'])} of bad ones; the "
                   f"e-process judge {_p(e['false_revert_null'])} / {_p(e['false_revert_good'])} / {_p(e['false_keep_bad'])}; "
                   f"O'Brien-Fleming {_p(o['false_revert_null'])} / {_p(o['false_revert_good'])} / {_p(o['false_keep_bad'])}.")
    out.append("")
    return out


def paired_sigma(measured: dict) -> dict:
    """Per class: 1.1 x the largest paired-difference std over the class's candidates, rounded up to 25 bps."""
    out = {}
    for cls in ("technical", "reversion"):
        stds = [r["std_bps"] for r in measured.get("paired") or [] if r.get("class") == cls and r.get("std_bps")]
        out[cls] = float(math.ceil(1.1 * max(stds) / 25.0) * 25.0) if stds else None
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--inputs", type=Path, help="measure_verdict_inputs.py JSON (defaults to the cache's inputs)")
    parser.add_argument("--database-url")
    parser.add_argument("--replications", type=int, default=REPLICATIONS)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    with open(args.cache, "rb") as fh:
        cache = pickle.load(fh)
    inputs = json.loads(args.inputs.read_text())["inputs"] if args.inputs else cache["inputs"]
    measured = (json.loads(args.inputs.read_text()) if args.inputs else cache.get("measured") or {}).get("all") or {}
    inputs.setdefault("paired_sigma_bps", paired_sigma(measured))
    cache["inputs"] = inputs
    rng = np.random.default_rng(SEED)
    pools: dict[str, dict[str, list]] = {"technical": {"paired": [], "raw": []}, "reversion": {"paired": [], "raw": []}}
    seen_base = set()
    for experiment in cache["experiments"]:
        strategy = experiment["strategy_type"]
        cls = vs.strategy_class(strategy)
        base = cache["trades"][setup_key(strategy, experiment["setups"]["baseline"])]
        if strategy not in seen_base and len(base):
            seen_base.add(strategy)
            raw = base.assign(value_bps=base["net_return"] * 10_000)
            pools[cls]["raw"] += cluster_pool(raw)
        for label, setup in experiment["setups"].items():
            if label == "baseline":
                continue
            pairs = paired_frame(base, cache["trades"][setup_key(strategy, setup)],
                                 adjust=vs.adjusts_for_spy(experiment["template"]))
            nonzero = pairs.loc[pairs["value_bps"].abs() > 1e-9]
            if len(nonzero) >= 30:
                pools[cls]["paired"] += cluster_pool(pairs)
    parametric = []
    for cls, pool in pools.items():
        if pool["paired"] and pool["raw"]:
            parametric += simulate_class(pool["paired"], pool["raw"], cls, inputs, replications=args.replications,
                                         rng=rng)
    stream = replay_stream(cache, inputs)
    ledger = ledger_rows(args.database_url)
    result = {"ran_at": datetime.now(timezone.utc).isoformat(), "paper_only": True, "inputs": inputs,
              "replications": args.replications, "parametric": parametric, "error_rates": error_rates(parametric),
              "stream": stream, "stream_summary": stream_summary(stream), "ledger": ledger,
              "deflate": deflate(cache, ledger), "obf_constants": {"harm": C_HARM, "improvement": C_IMPROVE}}
    result["readings"] = readings(result)
    args.output.write_text(json.dumps(result, indent=2, default=str))
    if args.report:
        args.report.write_text(render(result))
    print(json.dumps({"error_rates": result["error_rates"], "stream": result["stream_summary"],
                      "hurdle_default_on": result["deflate"]["default_on"],
                      "blocked": result["deflate"]["blocked"]}, indent=2, default=str))


if __name__ == "__main__":
    main()
