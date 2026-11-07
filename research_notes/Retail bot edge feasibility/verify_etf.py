# Source of the report's tranched Design A / Design B table (5, 11 and 25 bps a side, 2015+ and
# 2008-07+), the volatility-matched static arms, the zero-yield cash-leg cost and the
# single-rebalance-day drawdown spread. Copied unchanged from the report writer's session
# scratchpad on 2026-11-07.
# Input: the px.csv price cache that etf_rotation_backtest.py writes to its output directory.
# Run: python3 -I verify_etf.py <dir>/px.csv   (output saved in verify_etf.out.txt, about 90 s)
"""Writer's check of the ETF-rotation note: tranches, cost levels, static SPY/T-bill benchmark, paper-style cash leg.
Replicates run()/signals from research_notes/.../etf_rotation_backtest.py on its cached prices (px.csv)."""
import sys
sys.path.append('/Users/ainz_ool_gown/Library/Python/3.9/lib/python/site-packages')
import numpy as np, pandas as pd
PX = sys.argv[1]
RISKY8 = ["SPY", "EFA", "EEM", "IEF", "TLT", "GLD", "DBC", "VNQ"]
GTAA5 = ["SPY", "EFA", "IEF", "DBC", "VNQ"]
px = pd.read_csv(PX, index_col=0, parse_dates=True).dropna(how="all").ffill()
start = px[RISKY8 + ["BIL", "AGG", "VEU"]].dropna().index[0]
px = px.loc[start:]
ret0 = px.pct_change().fillna(0.0)
dates = px.index
print("data", dates[0].date(), dates[-1].date())

def month_end_days(offset=0):
    s = pd.Series(dates, index=dates); out = []
    for _, d in s.groupby([dates.year, dates.month]):
        if len(d) > offset: out.append(d.iloc[-1 - offset])
    return pd.DatetimeIndex(out)

def sig_gtaa(t, sma_days):
    i = dates.get_loc(t)
    if i < max(sma_days): return None
    w = pd.Series(0.0, index=px.columns)
    for a in GTAA5:
        f = np.mean([px[a].iloc[i] > px[a].iloc[i - L + 1:i + 1].mean() for L in sma_days]); w[a] = 0.2 * f
    w["BIL"] = 1 - w.sum(); return w

def sig_tsmom(t, looks=(21, 63, 126, 252), univ=RISKY8, volwin=63):
    i = dates.get_loc(t)
    if i < max(looks) + 1: return None
    frac = pd.Series(0.0, index=univ)
    for L in looks:
        r = px.iloc[i] / px.iloc[i - L] - 1.0; frac += (r[univ] > r["BIL"]).astype(float)
    frac /= len(looks)
    vol = ret0[univ].iloc[i - volwin + 1:i + 1].std() * np.sqrt(252)
    iv = (1 / vol) / (1 / vol).sum()
    w = pd.Series(0.0, index=px.columns); w[univ] = iv * frac; w["BIL"] = 1 - w.sum(); return w

def run(sigfun, sched, cost_bps, ret):
    sched = set(sched); c = cost_bps / 1e4; cols = px.columns; R = ret.values
    w = np.zeros(len(cols)); w[cols.get_loc("BIL")] = 1.0; pending = None
    out = np.zeros(len(dates)); turn = np.zeros(len(dates))
    for j, t in enumerate(dates):
        g = (w * R[j]).sum(); w = w * (1 + R[j]); w = w / w.sum() if w.sum() != 0 else w
        cost = 0.0
        if pending is not None:
            tw = pending.values; to = np.abs(tw - w).sum(); cost = to * c; turn[j] = to; w = tw.copy(); pending = None
        out[j] = (1 + g) * (1 - cost) - 1
        if t in sched:
            s = sigfun(t)
            if s is not None: pending = s.reindex(cols).fillna(0.0)
    return pd.Series(out, index=dates), pd.Series(turn, index=dates)

def stats(r, t0, t1, rf):
    r = r.loc[t0:t1]; rf = rf.loc[t0:t1]; n = len(r) / 252
    cagr = (1 + r).prod() ** (1 / n) - 1; vol = r.std() * np.sqrt(252); ex = r - rf
    sh = ex.mean() / ex.std() * np.sqrt(252); eq = (1 + r).cumprod(); dd = (eq / eq.cummax() - 1).min()
    bil = (1 + rf).prod() ** (1 / n) - 1
    return cagr, vol, sh, dd, cagr - bil

def tranche(sigfun, offs, cost, ret, t0):
    # four sleeves of 25%, each rebalancing monthly on its own offset; sleeves drift (no cross-sleeve rebalance)
    eqs = []
    for o in offs:
        r, _ = run(sigfun, month_end_days(o), cost, ret)
        eqs.append((1 + r.loc[t0:]).cumprod())
    eq = pd.concat(eqs, axis=1).mean(axis=1)
    return eq.pct_change().fillna(eq.iloc[0] - 1)

A = sig_tsmom
B = lambda t: sig_gtaa(t, tuple(21 * k for k in range(6, 13)))
rf_true = ret0["BIL"]
W = [("2015-01-01", "2026-10-31"), ("2008-07-01", "2026-10-31")]
for name, f in [("A TSMOM-IV", A), ("B GTAA5-ENS", B)]:
    for t0, t1 in W:
        dds = []; cg = []; shs = []
        for o in range(16):
            r, _ = run(f, month_end_days(o), 5.0, ret0); c_, v_, s_, d_, e_ = stats(r, t0, t1, rf_true)
            dds.append(d_); cg.append(c_); shs.append(s_)
        print(f"{name} {t0[:4]}+ single-day offsets 0-15 @5bp: CAGR {min(cg):.2%}..{max(cg):.2%} Sharpe {min(shs):.2f}..{max(shs):.2f} MaxDD {min(dds):.1%}..{max(dds):.1%} median {np.median(dds):.1%}")
        for cost in (5, 11, 25):
            r = tranche(f, (0, 5, 10, 15), cost, ret0, t0)
            c_, v_, s_, d_, e_ = stats(r, t0, t1, rf_true)
            print(f"  {name} {t0[:4]}+ 4 tranches @{cost}bp/side: CAGR {c_:.2%} vol {v_:.1%} Sharpe {s_:.2f} MaxDD {d_:.1%} excess over BIL {e_*100:+.2f} pts/yr")
        # paper-style: cash leg (BIL) earns zero
        retp = ret0.copy(); retp["BIL"] = 0.0
        r = tranche(f, (0, 5, 10, 15), 11, retp, t0); c_, v_, s_, d_, e_ = stats(r, t0, t1, rf_true)
        print(f"  {name} {t0[:4]}+ 4 tranches @11bp, BIL earning 0 in ledger: CAGR {c_:.2%}")
# static SPY/BIL blends, vol matched to each tranche sleeve, monthly rebalanced at 5 bp
for t0, t1 in W:
    bil_c = stats(rf_true, t0, t1, rf_true)[0]
    spy = stats(ret0["SPY"], t0, t1, rf_true)
    print(f"{t0[:4]}+ BIL CAGR {bil_c:.2%}; SPY CAGR {spy[0]:.2%} vol {spy[1]:.1%} Sharpe {spy[2]:.2f} MaxDD {spy[3]:.1%}")
    for name, f in [("A", A), ("B", B)]:
        r = tranche(f, (0, 5, 10, 15), 11, ret0, t0); vol = stats(r, t0, t1, rf_true)[1]
        w = vol / spy[1]
        sb, _ = run(lambda t, w=w: pd.Series({**{c: 0.0 for c in px.columns}, "SPY": w, "BIL": 1 - w}), month_end_days(0), 5.0, ret0)
        c_, v_, s_, d_, e_ = stats(sb, t0, t1, rf_true)
        print(f"  static {w:.0%} SPY/{1-w:.0%} BIL (vol-matched to {name}): CAGR {c_:.2%} vol {v_:.1%} Sharpe {s_:.2f} MaxDD {d_:.1%} excess {e_*100:+.2f}")
    # yearly 2022/2023 for tranche sleeves
for name, f in [("A", A), ("B", B)]:
    r = tranche(f, (0, 5, 10, 15), 11, ret0, "2015-01-01")
    y = (1 + r).groupby(r.index.year).prod() - 1
    print(name, "tranched @11bp yearly:", " ".join(f"{k}:{v*100:+.1f}" for k, v in y.items()))
