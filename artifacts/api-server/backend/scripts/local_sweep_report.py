"""Tables and multiple-comparison corrections for ``scripts/local_sweep.py`` results.

Reads ``<LOCAL_SWEEP_DIR>/out/{results.json,oos_daily.npy,oos_daily_keys.json}``
and writes ``tables.md`` (markdown fragments the report quotes), ``summary.json``
(the headline numbers), ``candidates.csv`` (one row per trial and cost) and
``per_symbol.csv`` (one row per trial, cost and symbol).
"""
from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(os.environ.get("LOCAL_SWEEP_DIR") or (Path(__file__).resolve().parents[1] / "output" / "local_sweep"))
OUT = ROOT / "out"
SEED = 7
BOOTSTRAPS = 1000
MEAN_BLOCK = 20
MIN_FIT_ANCHORED = 30
MIN_FIT_6M = 10


def fmt(value, digits=1, suffix=""):
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "n/a"
    return f"{value:.{digits}f}{suffix}"


def table(rows: list[dict], columns: list[tuple[str, str]]) -> str:
    head = "| " + " | ".join(title for _, title in columns) + " |"
    sep = "|" + "|".join("---" for _ in columns) + "|"
    body = ["| " + " | ".join(str(row.get(key, "")) for key, _ in columns) + " |" for row in rows]
    return "\n".join([head, sep, *body])


# ------------------------------------------------------------------ load

def load() -> tuple[pd.DataFrame, list[dict], np.ndarray, list[tuple[str, float]]]:
    results = json.loads((OUT / "results.json").read_text())
    results = [r for r in results if "error" not in r]
    rows = []
    for r in results:
        o = r["oos"]
        rows.append({
            "id": r["id"], "base_id": r["base_id"], "family": r["family"], "universe": r["universe"], "gate": r["gate"],
            "description": r["description"], "open_timed": r["open_timed"], "cost_bps": r["cost_bps"],
            "stop": r["stop_loss_pct"], "target": r["take_profit_pct"], "hold": r["max_holding_sessions"],
            "trades": o["trades"], "mean_bps": o["mean_bps"], "gross_mean_bps": o["gross_mean_bps"], "pf": o["pf"],
            "win_rate": o["win_rate"], "max_dd": o["max_dd"], "net_sum": o["net_sum"],
            "excess_mean_bps": o["excess_mean_bps"], "excess_t": o["excess_t"], "clustered_t": o["clustered_t"],
            "nw_t_daily": o["nw_t_daily"], "ann_sharpe": o["ann_sharpe"], "mean_daily_bps": o["mean_daily_bps"],
            "windows_active": o["windows_active"], "positive_window_fraction": o["positive_window_fraction"],
            "usd_1sh_mean": o["usd_1sh_mean"], "next_day_mean_bps": o["next_day_mean_bps"], "mean_hold": o["mean_hold"],
            "symbols_traded": o["symbols_traded"],
            "pre2015_trades": r["pre2015"]["trades"], "pre2015_mean_bps": r["pre2015"]["mean_bps"],
            "last6m_trades": r["last6m"]["trades"], "last6m_mean_bps": r["last6m"]["mean_bps"],
            "skip_earnings_mean_bps": r["filters"]["skip_earnings_ahead"]["mean_bps"],
            "skip_earnings_trades": r["filters"]["skip_earnings_ahead"]["trades"],
            "skip_earnings_t": r["filters"]["skip_earnings_ahead"]["clustered_t"],
            "skip_macro_mean_bps": r["filters"]["skip_macro_ahead"]["mean_bps"],
            "skip_macro_trades": r["filters"]["skip_macro_ahead"]["trades"],
            "skip_macro_t": r["filters"]["skip_macro_ahead"]["clustered_t"],
            "only_macro_mean_bps": r["filters"]["only_macro_ahead"]["mean_bps"],
            "only_macro_trades": r["filters"]["only_macro_ahead"]["trades"],
        })
    frame = pd.DataFrame(rows)
    matrix = np.load(OUT / "oos_daily.npy")
    keys = [(k[0], float(k[1])) for k in json.loads((OUT / "oos_daily_keys.json").read_text())]
    return frame, results, matrix, keys


# ------------------------------------------------------------------ reality check

def stationary_bootstrap_indices(n: int, samples: int, mean_block: int, rng: np.random.Generator) -> np.ndarray:
    """Politis-Romano stationary bootstrap: geometric block lengths, wrap-around."""
    p = 1.0 / mean_block
    out = np.empty((samples, n), dtype=np.int64)
    for b in range(samples):
        idx = np.empty(n, dtype=np.int64)
        pos = 0
        while pos < n:
            start = rng.integers(0, n)
            length = rng.geometric(p)
            length = min(length, n - pos)
            idx[pos: pos + length] = (start + np.arange(length)) % n
            pos += length
        out[b] = idx
    return out


def reality_check(matrix: np.ndarray, labels: list[str], *, samples: int = BOOTSTRAPS, seed: int = SEED) -> dict:
    """White (2000) reality check over the trials' OOS daily returns, plus per-trial bootstrap p-values.

    ``matrix`` is trials x days.  The statistic is ``sqrt(T) * max_k mean_k``
    (benchmark: cash, 0).  Also reports the step-down (Romano-Wolf style)
    survivors: trials whose statistic exceeds the bootstrap 95% quantile of the
    max over the trials not yet rejected.
    """
    rng = np.random.default_rng(seed)
    k, t = matrix.shape
    means = matrix.mean(axis=1).astype(np.float64)
    idx = stationary_bootstrap_indices(t, samples, MEAN_BLOCK, rng)
    boot = np.empty((samples, k), dtype=np.float64)
    m64 = matrix.astype(np.float32)
    for b in range(samples):
        boot[b] = m64[:, idx[b]].mean(axis=1)
    centered = boot - means[None, :]          # bootstrap distribution of (mean_b - mean) per trial
    root = math.sqrt(t)
    observed = root * means
    max_null = root * centered.max(axis=1)
    best = int(np.argmax(observed))
    p_rc = float((max_null >= observed[best]).mean())
    per_trial_p = (centered >= means[None, :]).mean(axis=0)   # one-sided: P(mean_b - mean >= mean) under H0
    # step-down
    remaining = np.ones(k, dtype=bool)
    survivors = []
    while remaining.any():
        quantile = np.quantile(root * centered[:, remaining].max(axis=1), 0.95)
        order = np.argsort(-observed)
        picked = next((i for i in order if remaining[i]), None)
        if picked is None or observed[picked] <= quantile:
            break
        survivors.append(labels[picked])
        remaining[picked] = False
    # With ``samples`` draws a per-trial p-value cannot be resolved below 1/samples, so it cannot be
    # compared with a Bonferroni threshold of 0.05 / k (about 1.6e-5 for 3,086 trials).  The per-trial
    # p is reported as a floor ("< 1/samples") and the familywise decisions use the max-statistic
    # bootstrap (reality check and step-down) and the normal approximation on the clustered t.
    floor = 1.0 / samples
    at_floor = int((per_trial_p < floor).sum())
    return {"trials": int(k), "days": int(t), "samples": samples, "mean_block": MEAN_BLOCK,
            "per_trial_p_floor": floor, "trials_at_p_floor": at_floor,
            "best": labels[best], "best_mean_daily_bps": float(means[best] * 1e4),
            "best_statistic": float(observed[best]), "null_max_95": float(np.quantile(max_null, 0.95)),
            "p_value": p_rc, "stepdown_survivors": survivors,
            "per_trial_p": {labels[i]: float(max(per_trial_p[i], floor)) for i in range(k)}}


# ------------------------------------------------------------------ walk-forward selection

def walk_forward_selection(results: list[dict], cost: float, pool_key, *, lookback: str, min_trades: int) -> dict:
    """Pick, at each window start, the pool member with the best in-sample mean (anchored or trailing 6 months); test it next window."""
    by_pool: dict = {}
    for r in results:
        if r["cost_bps"] != cost or r["open_timed"]:
            continue
        by_pool.setdefault(pool_key(r), []).append(r)
    windows = list(next(iter(results))["windows"].keys())
    out = {}
    for pool, members in by_pool.items():
        picks, promised, realised_sum, realised_n, positive, active = [], [], 0.0, 0, 0, 0
        for w in windows:
            best, best_mean = None, None
            for r in members:
                f = r["fit"][w]
                n, mean = (f["anchored_trades"], f["anchored_mean_bps"]) if lookback == "anchored" else (f["m6_trades"], f["m6_mean_bps"])
                if n >= min_trades and mean is not None and (best_mean is None or mean > best_mean):
                    best, best_mean = r, mean
            if best is None:
                continue
            test = best["windows"][w]
            picks.append({"window": w, "id": best["id"], "promised_bps": best_mean, "oos_trades": test["trades"],
                          "oos_mean_bps": test["mean_bps"], "oos_sum": test["net_sum"]})
            promised.append(best_mean)
            if test["trades"]:
                active += 1
                positive += int(test["net_sum"] > 0)
                realised_sum += test["net_sum"]
                realised_n += test["trades"]
        out[pool] = {"windows_picked": len(picks), "windows_active": active,
                     "promised_mean_bps": float(np.mean(promised)) if promised else None,
                     "oos_trades": realised_n, "oos_mean_bps": (realised_sum / realised_n * 1e4) if realised_n else None,
                     "positive_window_fraction": (positive / active) if active else None, "picks": picks}
    return out


# ------------------------------------------------------------------ main

def main() -> None:
    frame, results, matrix, keys = load()
    windows = list(results[0]["windows"].keys())
    frame.to_csv(OUT / "candidates.csv", index=False)
    per_symbol_rows = []
    for r in results:
        for symbol, s in r["per_symbol"].items():
            per_symbol_rows.append({"id": r["id"], "cost_bps": r["cost_bps"], "family": r["family"], "gate": r["gate"],
                                    "symbol": symbol, **s})
    per_symbol = pd.DataFrame(per_symbol_rows)
    per_symbol.to_csv(OUT / "per_symbol.csv", index=False)
    window_rows = [{"id": r["id"], "cost_bps": r["cost_bps"], "family": r["family"], "universe": r["universe"], "gate": r["gate"],
                    "window": w, **stats, **{f"fit_{k}": v for k, v in r["fit"][w].items()}}
                   for r in results for w, stats in r["windows"].items()]
    pd.DataFrame(window_rows).to_csv(OUT / "per_window.csv", index=False)
    summary: dict = {"windows": windows}
    md: list[str] = []

    # trial counts
    f11 = frame[frame["cost_bps"] == 11.0]
    f25 = frame[frame["cost_bps"] == 25.0].set_index("id")
    traded = f11[f11["trades"] > 0]
    summary["trials"] = {"configured": int(len(f11)), "with_oos_trades": int(len(traded)),
                         "close_timed": int((~f11["open_timed"]).sum()), "open_timed": int(f11["open_timed"].sum()),
                         "hand_written": int(f11["family"].str.startswith("hand:").sum()),
                         "library_rules": int(f11["base_id"].nunique() - 9), "gates": int(f11["gate"].nunique())}

    # reality check at each cost, over trials with any OOS trade
    rc = {}
    index = {k: i for i, k in enumerate(keys)}
    for cost in (11.0, 25.0):
        ids = [i for i in frame[(frame["cost_bps"] == cost) & (frame["trades"] > 0)]["id"]]
        rows = [index[(i, cost)] for i in ids]
        rc[cost] = reality_check(matrix[rows], ids)
        for i in ids:
            frame.loc[(frame["id"] == i) & (frame["cost_bps"] == cost), "boot_p"] = rc[cost]["per_trial_p"][i]
        # excess-over-drift Bonferroni on month-clustered t (normal approximation)
        sub = frame[(frame["cost_bps"] == cost) & (frame["trades"] > 0)]
        n = len(sub)
        from statistics import NormalDist
        z_bonf = NormalDist().inv_cdf(1 - 0.05 / n)
        rc[cost]["bonferroni_z"] = z_bonf
        rc[cost]["net_t_survivors"] = int((sub["clustered_t"].fillna(-9) > z_bonf).sum())
        rc[cost]["excess_t_survivors"] = int((sub["excess_t"].fillna(-9) > z_bonf).sum())
        rc[cost]["net_t_ge_2"] = int((sub["clustered_t"].fillna(-9) >= 2).sum())
        rc[cost]["excess_t_ge_2"] = int((sub["excess_t"].fillna(-9) >= 2).sum())
        rc[cost].pop("per_trial_p")
    summary["reality_check"] = rc
    frame.to_csv(OUT / "candidates.csv", index=False)
    f11 = frame[frame["cost_bps"] == 11.0]
    f25 = frame[frame["cost_bps"] == 25.0].set_index("id")
    traded = f11[f11["trades"] > 0]

    # top trials at 11 bps by clustered t, with 25 bps alongside
    def enrich(row):
        r25 = f25.loc[row["id"]]
        return {"family": row["family"], "universe": row["universe"], "gate": row["gate"],
                "description": row["description"][:90], "trades": row["trades"], "mean11": fmt(row["mean_bps"]),
                "mean25": fmt(r25["mean_bps"]), "pf": fmt(row["pf"], 2), "dd": fmt(row["max_dd"] * 100 if row["max_dd"] is not None else None, 1, "%"),
                "posw": fmt(row["positive_window_fraction"] * 100 if row["positive_window_fraction"] is not None else None, 0, "%"),
                "excess": fmt(row["excess_mean_bps"]), "t": fmt(row["clustered_t"], 2), "t25": fmt(r25["clustered_t"], 2),
                "ex_t": fmt(row["excess_t"], 2),
                "boot_p": ("< 0.001" if row.get("boot_p") is not None and row.get("boot_p") <= 0.001 else fmt(row.get("boot_p"), 3)),
                "hold": fmt(row["mean_hold"], 1), "usd": fmt(row["usd_1sh_mean"], 2), "l6": f"{row['last6m_trades']} / {fmt(row['last6m_mean_bps'])}"}

    cols = [("family", "Family"), ("universe", "Universe"), ("gate", "Gate"), ("description", "Rule"), ("trades", "Trades"),
            ("mean11", "Net bps @11"), ("mean25", "Net bps @25"), ("pf", "PF"), ("dd", "Max DD"), ("posw", "Windows +"),
            ("excess", "Excess bps"), ("t", "t (net)"), ("ex_t", "t (excess)"), ("boot_p", "boot p (floor 0.001)")]
    top = traded.sort_values("clustered_t", ascending=False).head(25)
    md.append("## Top 25 trials by month-clustered t of net OOS trade returns (2015-01 → 2026-11, 11 bps a side)\n")
    md.append(table([enrich(r) for _, r in top.iterrows()], cols))
    top_excess = traded[traded["trades"] >= 100].sort_values("excess_t", ascending=False).head(15)
    md.append("\n## Top 15 trials (≥100 trades) by t of excess over the symbol's own causal drift\n")
    md.append(table([enrich(r) for _, r in top_excess.iterrows()], cols))
    paper = traded[(~traded["open_timed"]) & (traded["trades"] >= 100)].sort_values("clustered_t", ascending=False).head(15)
    md.append("\n## Top 15 paper-executable trials (close-timed, ≥100 trades) by net t\n")
    md.append(table([enrich(r) for _, r in paper.iterrows()], cols))

    # hand-written strategies: no gate and best gate
    md.append("\n## The bot's hand-written strategies on the 45-symbol paper universe (OOS 2015 →)\n")
    hand_rows = []
    for name in ("rsi2_pullback", "ibs_mean_reversion", "rsi_mean_reversion", "bollinger_mean_reversion", "channel_breakout",
                 "trend_pullback", "moving_average_crossover", "macd_momentum", "ensemble"):
        group = traded[traded["base_id"] == name]
        none = group[group["gate"] == "none"]
        if none.empty:
            continue
        best = group.sort_values("clustered_t", ascending=False).iloc[0]
        for label, row in (("none", none.iloc[0]), (f"best: {best['gate']}", best)):
            e = enrich(row)
            e["strategy"], e["gate"] = name, label
            hand_rows.append(e)
    ungated = traded[(traded["family"].str.startswith("hand:")) & (traded["gate"] == "none")]
    u25 = f25.loc[ungated["id"]]
    summary["hand_written"] = {
        "strategies": int(len(ungated)),
        "positive_at_11": int((ungated["mean_bps"] > 0).sum()), "positive_at_25": int((u25["mean_bps"] > 0).sum()),
        "negative_at_25": int((u25["mean_bps"] < 0).sum()), "within_1bp_of_zero_at_25": int((u25["mean_bps"].abs() < 1).sum()),
        "t_ge_2_at_11": int((ungated["clustered_t"] >= 2).sum()), "t_ge_2_at_25": int((u25["clustered_t"] >= 2).sum()),
        "excess_negative": int((ungated["excess_mean_bps"] < 0).sum()),
        "rows": {r["base_id"]: {"mean11": round(r["mean_bps"], 1), "mean25": round(f25.loc[r["id"], "mean_bps"], 1),
                                "t11": round(r["clustered_t"], 2), "t25": round(f25.loc[r["id"], "clustered_t"], 2),
                                "excess": round(r["excess_mean_bps"], 1)} for _, r in ungated.iterrows()}}
    md.append(table(hand_rows, [("strategy", "Strategy"), ("gate", "Gate"), ("trades", "Trades"), ("mean11", "Net bps @11"),
                                ("mean25", "Net bps @25"), ("pf", "PF"), ("dd", "Max DD"), ("posw", "Windows +"),
                                ("excess", "Excess bps"), ("t", "t (net)"), ("t25", "t @25"), ("ex_t", "t (excess)"),
                                ("usd", "$ / trade, 1 share"), ("l6", "Last 6m: trades / bps")]))

    # family summary
    md.append("\n## Families (11 bps, trials with ≥1 OOS trade)\n")
    fam_rows = []
    for family, group in traded.groupby("family"):
        g25 = f25.loc[group["id"]]
        fam_rows.append({"family": family, "n": len(group), "median": fmt(group["mean_bps"].median()),
                         "median25": fmt(g25["mean_bps"].median()),
                         "positive": fmt((group["mean_bps"] > 0).mean() * 100, 0, "%"),
                         "positive25": fmt((g25["mean_bps"] > 0).mean() * 100, 0, "%"),
                         "excess_pos": fmt((group["excess_mean_bps"] > 0).mean() * 100, 0, "%"),
                         "best_t": fmt(group["clustered_t"].max(), 2),
                         "posw": fmt(group["positive_window_fraction"].median() * 100, 0, "%")})
    fam_rows.sort(key=lambda r: -float(r["best_t"]) if r["best_t"] != "n/a" else 0)
    md.append(table(fam_rows, [("family", "Family"), ("n", "Trials"), ("median", "Median net bps @11"), ("median25", "@25"),
                               ("positive", "Trials > 0 @11"), ("positive25", "@25"), ("excess_pos", "Excess > 0"),
                               ("best_t", "Best t"), ("posw", "Median windows +")]))

    # gates: effect on each family (none vs each gate), paired on base_id
    md.append("\n## Gates: paired change in net bps per trade against the ungated rule (11 bps, close-timed trials)\n")
    gate_rows = []
    closed = traded[~traded["open_timed"]]
    base_none = closed[closed["gate"] == "none"].set_index("base_id")
    for gate in [g for g in closed["gate"].unique() if g != "none"]:
        sub = closed[closed["gate"] == gate].set_index("base_id")
        common = sub.index.intersection(base_none.index)
        delta = sub.loc[common, "mean_bps"] - base_none.loc[common, "mean_bps"]
        kept = sub.loc[common, "trades"] / base_none.loc[common, "trades"]
        dd_delta = sub.loc[common, "max_dd"] - base_none.loc[common, "max_dd"]
        better_t = (sub.loc[common, "clustered_t"].fillna(-9) > base_none.loc[common, "clustered_t"].fillna(-9)).mean()
        gate_rows.append({"gate": gate, "n": len(common), "delta_median": fmt(delta.median()), "delta_mean": fmt(delta.mean()),
                          "improved": fmt((delta > 0).mean() * 100, 0, "%"), "kept": fmt(kept.median() * 100, 0, "%"),
                          "dd": fmt(dd_delta.median() * 100, 1, " pts"), "better_t": fmt(better_t * 100, 0, "%")})
    md.append(table(gate_rows, [("gate", "Gate"), ("n", "Rules"), ("delta_median", "Median Δ bps"), ("delta_mean", "Mean Δ bps"),
                                ("improved", "Rules improved"), ("kept", "Trades kept (median)"), ("dd", "Median Δ max DD"),
                                ("better_t", "t improved")]))

    # walk-forward selection
    md.append("\n## Walk-forward selection: pick the best in-sample variant, trade it the next 6 months\n")
    sel_rows = []
    selections = {}
    for cost, lookback, min_trades in ((c, lb, mt) for c in (11.0, 25.0) for lb, mt in (("anchored", MIN_FIT_ANCHORED), ("6m", MIN_FIT_6M))):
        for pool_name, key in (("all close-timed trials", lambda r: "all"),
                               ("per family × universe", lambda r: f"{r['family']} / {r['universe']}")):
            sel = walk_forward_selection(results, cost, key, lookback=lookback, min_trades=min_trades)
            selections[(lookback, pool_name, cost)] = sel
            if pool_name.startswith("all"):
                s = sel["all"]
                sel_rows.append({"cost": cost, "lookback": lookback, "pool": pool_name, "picked": s["windows_picked"],
                                 "promised": fmt(s["promised_mean_bps"]), "oos": fmt(s["oos_mean_bps"]), "trades": s["oos_trades"],
                                 "posw": fmt(s["positive_window_fraction"] * 100 if s["positive_window_fraction"] is not None else None, 0, "%")})
            else:
                vals = [s for s in sel.values() if s["oos_trades"]]
                promised = np.mean([s["promised_mean_bps"] for s in vals])
                oos_n = sum(s["oos_trades"] for s in vals)
                oos = sum(s["oos_mean_bps"] * s["oos_trades"] for s in vals) / oos_n if oos_n else None
                posw = np.mean([s["positive_window_fraction"] for s in vals if s["positive_window_fraction"] is not None])
                sel_rows.append({"cost": cost, "lookback": lookback, "pool": f"{pool_name} ({len(vals)} pools, trade-weighted)",
                                 "picked": int(np.mean([s["windows_picked"] for s in vals])), "promised": fmt(promised),
                                 "oos": fmt(oos), "trades": oos_n, "posw": fmt(posw * 100, 0, "%")})
    md.append(table(sel_rows, [("cost", "Cost bps/side"), ("lookback", "Fit window"), ("pool", "Selection pool"), ("picked", "Windows picked"),
                               ("promised", "In-sample bps (promised)"), ("oos", "Next-window bps (realised)"),
                               ("trades", "OOS trades"), ("posw", "Windows +")]))
    summary["selection"] = {f"{k[0]} | {k[1]} | {k[2]:g} bps": {p: {kk: vv for kk, vv in v.items() if kk != "picks"} for p, v in sel.items()}
                            for k, sel in selections.items()}
    # the global chains in detail
    for lookback in ("anchored", "6m"):
        picks = selections[(lookback, "all close-timed trials", 11.0)]["all"]["picks"]
        md.append(f"\n### Global pick per window, {lookback} fit (11 bps)\n")
        md.append(table([{"window": p["window"], "id": p["id"], "promised": fmt(p["promised_bps"]), "n": p["oos_trades"],
                          "oos": fmt(p["oos_mean_bps"])} for p in picks],
                        [("window", "Window"), ("id", "Pick"), ("promised", "In-sample bps"), ("n", "OOS trades"), ("oos", "OOS bps")]))

    # last 6 months leaderboard vs walk-forward
    md.append("\n## What a 6-month lookback would pick today (2026-05 → 2026-11) vs its full walk-forward record (11 bps)\n")
    recent = traded[(traded["last6m_trades"] >= MIN_FIT_6M) & (~traded["open_timed"])].sort_values("last6m_mean_bps", ascending=False).head(15)
    md.append(table([{"id": r["id"], "family": r["family"], "universe": r["universe"], "l6n": r["last6m_trades"],
                      "l6": fmt(r["last6m_mean_bps"]), "wf": fmt(r["mean_bps"]), "wfn": r["trades"],
                      "posw": fmt(r["positive_window_fraction"] * 100, 0, "%"), "t": fmt(r["clustered_t"], 2),
                      "pre": fmt(r["pre2015_mean_bps"])} for _, r in recent.iterrows()],
                    [("id", "Trial"), ("family", "Family"), ("universe", "Universe"), ("l6n", "6m trades"), ("l6", "6m net bps"),
                     ("wf", "WF 2015→ net bps"), ("wfn", "WF trades"), ("posw", "Windows +"), ("t", "WF t"), ("pre", "2000–14 bps")]))
    ratio = traded[(traded["last6m_trades"] >= MIN_FIT_6M) & (traded["trades"] >= 100)]
    summary["six_month_vs_wf"] = {
        "trials": int(len(ratio)),
        "mean_last6m_bps_top15": float(recent["last6m_mean_bps"].mean()) if len(recent) else None,
        "mean_wf_bps_top15": float(recent["mean_bps"].mean()) if len(recent) else None,
        "corr_last6m_vs_wf": float(ratio["last6m_mean_bps"].corr(ratio["mean_bps"])) if len(ratio) > 2 else None,
        "share_sign_agrees": float(((ratio["last6m_mean_bps"] > 0) == (ratio["mean_bps"] > 0)).mean()) if len(ratio) else None,
        "abs_last6m_over_abs_wf_median": float((ratio["last6m_mean_bps"].abs() / ratio["mean_bps"].abs().replace(0, np.nan)).median()) if len(ratio) else None,
    }

    # events
    md.append("\n## Event check (OOS trades of ungated close-timed trials, 11 bps): net bps with vs without an event inside the holding window\n")
    ev_rows = []
    pool = [r for r in results if r["cost_bps"] == 11.0 and r["gate"] == "none" and not r["open_timed"] and r["oos"]["trades"] > 0]
    for kind in ("earnings_held", "fomc_held", "cpi_held", "jobs_held", "macro_held", "earnings_ahead", "macro_ahead"):
        n_in = sum(r["events"][kind]["in"]["trades"] for r in pool)
        n_out = sum(r["events"][kind]["out"]["trades"] for r in pool)
        s_in = sum(r["events"][kind]["in"]["net_sum"] for r in pool)
        s_out = sum(r["events"][kind]["out"]["net_sum"] for r in pool)
        better = np.mean([(r["events"][kind]["in"]["mean_bps"] or 0) > (r["events"][kind]["out"]["mean_bps"] or 0)
                          for r in pool if r["events"][kind]["in"]["trades"] >= 20 and r["events"][kind]["out"]["trades"] >= 20])
        ev_rows.append({"kind": kind, "n_in": n_in, "in": fmt(s_in / n_in * 1e4 if n_in else None), "n_out": n_out,
                        "out": fmt(s_out / n_out * 1e4 if n_out else None), "better": fmt(better * 100, 0, "%")})
    md.append(table(ev_rows, [("kind", "Event"), ("n_in", "Trades with event"), ("in", "Net bps with"), ("n_out", "Trades without"),
                              ("out", "Net bps without"), ("better", "Trials where 'with' beats 'without'")]))
    summary["events"] = ev_rows
    md.append("\n### Causal event filters on the top paper-executable trials and the hand-written strategies (11 bps)\n")
    filt_rows = []
    chosen = pd.concat([paper.head(8), traded[(traded["family"].str.startswith("hand:")) & (traded["gate"] == "none")]])
    for _, r in chosen.iterrows():
        filt_rows.append({"id": r["id"][:40], "n": r["trades"], "base": fmt(r["mean_bps"]), "t": fmt(r["clustered_t"], 2),
                          "se_n": r["skip_earnings_trades"], "se": fmt(r["skip_earnings_mean_bps"]), "se_t": fmt(r["skip_earnings_t"], 2),
                          "sm_n": r["skip_macro_trades"], "sm": fmt(r["skip_macro_mean_bps"]), "sm_t": fmt(r["skip_macro_t"], 2),
                          "om_n": r["only_macro_trades"], "om": fmt(r["only_macro_mean_bps"])})
    md.append(table(filt_rows, [("id", "Trial"), ("n", "Trades"), ("base", "Net bps"), ("t", "t"), ("se_n", "Skip-earnings trades"),
                                ("se", "bps"), ("se_t", "t"), ("sm_n", "Skip-macro trades"), ("sm", "bps"), ("sm_t", "t"),
                                ("om_n", "Only-macro trades"), ("om", "bps")]))

    # per-symbol: best symbols for the top paper-executable trial and symbol consistency
    md.append("\n## Per-symbol view of the top three paper-executable trials (11 bps)\n")
    for _, r in paper.head(3).iterrows():
        sub = per_symbol[(per_symbol["id"] == r["id"]) & (per_symbol["cost_bps"] == 11.0)].sort_values("mean_bps", ascending=False)
        md.append(f"\n**{r['id']}** ({r['description'][:80]}): {int((sub['mean_bps'] > 0).sum())} of {len(sub)} symbols positive; "
                  f"median symbol windows+ {fmt(sub['positive_window_fraction'].median() * 100, 0, '%')}\n")
        md.append(table([{"symbol": s["symbol"], "n": s["trades"], "bps": fmt(s["mean_bps"]), "pf": fmt(s["pf"], 2),
                          "dd": fmt(s["max_dd"] * 100 if s["max_dd"] is not None else None, 1, "%"),
                          "posw": fmt(s["positive_window_fraction"] * 100 if s["positive_window_fraction"] is not None else None, 0, "%"),
                          "ex": fmt(s["excess_mean_bps"])} for _, s in sub.iterrows()],
                        [("symbol", "Symbol"), ("n", "Trades"), ("bps", "Net bps"), ("pf", "PF"), ("dd", "Max DD"), ("posw", "Windows +"), ("ex", "Excess bps")]))

    # pre-registration shortlist
    short = traded[(~traded["open_timed"]) & (traded["trades"] >= 100) & (traded["positive_window_fraction"] >= 0.6)
                   & (traded["excess_mean_bps"] > 0) & (traded["clustered_t"] >= 2.0)].copy()
    short["t25"] = [f25.loc[i, "clustered_t"] for i in short["id"]]
    short["mean25"] = [f25.loc[i, "mean_bps"] for i in short["id"]]
    short = short[short["t25"] >= 1.5].sort_values("clustered_t", ascending=False)
    summary["shortlist"] = short[["id", "family", "universe", "gate", "description", "trades", "mean_bps", "mean25", "clustered_t", "t25",
                                  "excess_mean_bps", "excess_t", "positive_window_fraction", "max_dd", "boot_p"]].to_dict("records")
    md.append("\n## Shortlist: close-timed, ≥100 trades, windows+ ≥60%, excess over drift >0, t≥2 @11 and t≥1.5 @25\n")
    md.append(table([enrich(r) for _, r in short.iterrows()], cols) if len(short) else "_none_")

    (OUT / "tables.md").write_text("\n".join(md))
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    print(json.dumps({k: v for k, v in summary.items() if k in ("trials", "reality_check", "six_month_vs_wf")}, indent=1, default=str))
    print("shortlist", len(short))


if __name__ == "__main__":
    main()
