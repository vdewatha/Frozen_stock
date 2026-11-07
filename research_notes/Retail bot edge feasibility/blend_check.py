# Marginal-contribution check, monthly-rebalanced mixes: 10-30% of equity in tranched Design A,
# taken either pro rata from the 50/50 SPY/T-bill arm or from its T-bill leg only, at 11 and
# 25 bps a side, 2015+ and 2008-07+. Written by the report's reviewer on 2026-11-07 and copied
# unchanged from the session scratchpad (the docstring's RSI(2) mention refers to
# blend_check_daily.py, which runs the proxy).
# Input: the px.csv price cache that etf_rotation_backtest.py writes to its output directory.
# Run: python3 -I blend_check.py <dir>/px.csv   (output saved in blend_check.out.txt)
"""Reviewer check: does adding tranched Design A (or the note's RSI(2) SPY proxy) to the 50/50 SPY/BIL passive arm
raise the book's Sharpe without a deeper drawdown (the report's step-4 portfolio test), per window and cost?"""
import sys
sys.path.append('/Users/ainz_ool_gown/Library/Python/3.9/lib/python/site-packages')
import numpy as np, pandas as pd
PX = sys.argv[1]
RISKY8 = ["SPY", "EFA", "EEM", "IEF", "TLT", "GLD", "DBC", "VNQ"]
px = pd.read_csv(PX, index_col=0, parse_dates=True).dropna(how="all").ffill()
start = px[RISKY8 + ["BIL", "AGG", "VEU"]].dropna().index[0]
px = px.loc[start:]
ret = px.pct_change().fillna(0.0)
dates = px.index

def month_end_days(offset=0):
    s = pd.Series(dates, index=dates); out = []
    for _, d in s.groupby([dates.year, dates.month]):
        if len(d) > offset: out.append(d.iloc[-1 - offset])
    return pd.DatetimeIndex(out)

def sig_tsmom(t, looks=(21, 63, 126, 252), univ=RISKY8, volwin=63):
    i = dates.get_loc(t)
    if i < max(looks) + 1: return None
    frac = pd.Series(0.0, index=univ)
    for L in looks:
        r = px.iloc[i] / px.iloc[i - L] - 1.0; frac += (r[univ] > r["BIL"]).astype(float)
    frac /= len(looks)
    vol = ret[univ].iloc[i - volwin + 1:i + 1].std() * np.sqrt(252)
    iv = (1 / vol) / (1 / vol).sum()
    w = pd.Series(0.0, index=px.columns); w[univ] = iv * frac; w["BIL"] = 1 - w.sum(); return w

def run(sigfun, sched, cost_bps):
    sched = set(sched); c = cost_bps / 1e4; cols = px.columns; R = ret.values
    w = np.zeros(len(cols)); w[cols.get_loc("BIL")] = 1.0; pending = None
    out = np.zeros(len(dates))
    for j, t in enumerate(dates):
        g = (w * R[j]).sum(); w = w * (1 + R[j]); w = w / w.sum() if w.sum() != 0 else w
        cost = 0.0
        if pending is not None:
            tw = pending.values; to = np.abs(tw - w).sum(); cost = to * c; w = tw.copy(); pending = None
        out[j] = (1 + g) * (1 - cost) - 1
        if t in sched:
            s = sigfun(t)
            if s is not None: pending = s.reindex(cols).fillna(0.0)
    return pd.Series(out, index=dates)

def tranche(cost):
    eqs = [(1 + run(sig_tsmom, month_end_days(o), cost)).cumprod() for o in (0, 5, 10, 15)]
    eq = pd.concat(eqs, axis=1).mean(axis=1)
    return eq.pct_change().fillna(0.0)

def rsi2_proxy(cost_bps):
    p = px["SPY"]; d = p.diff()
    up = d.clip(lower=0).ewm(alpha=1/2, adjust=False).mean(); dn = (-d.clip(upper=0)).ewm(alpha=1/2, adjust=False).mean()
    rsi = 100 - 100 / (1 + up / dn); sma200 = p.rolling(200).mean(); sma5 = p.rolling(5).mean()
    pos = np.zeros(len(p)); inpos = 0
    for j in range(len(p)):
        if inpos == 0 and rsi.iloc[j] < 10 and p.iloc[j] > sma200.iloc[j]: inpos = 1
        elif inpos == 1 and p.iloc[j] > sma5.iloc[j]: inpos = 0
        pos[j] = inpos
    pos = pd.Series(pos, index=p.index).shift(1).fillna(0); tr = pos.diff().abs().fillna(0)
    return pos * ret["SPY"] + (1 - pos) * ret["BIL"] - tr * cost_bps / 1e4

def stats(r, t0, t1):
    r = r.loc[t0:t1]; rf = ret["BIL"].loc[t0:t1]; n = len(r) / 252
    cagr = (1 + r).prod() ** (1 / n) - 1; ex = r - rf
    sh = ex.mean() / ex.std() * np.sqrt(252); eq = (1 + r).cumprod(); dd = (eq / eq.cummax() - 1).min()
    return cagr, sh, dd


spy, bil = ret["SPY"], ret["BIL"]
def monthly_mix(streams, weights):
    me = set(month_end_days(0)); wt = np.array(weights, float); out = []
    R = np.column_stack([s.values for s in streams])
    for j, t in enumerate(dates):
        gg = (wt * R[j]).sum(); wt = wt * (1 + R[j]); wt = wt / wt.sum(); out.append(gg)
        if t in me: wt = np.array(weights, float)
    return pd.Series(out, index=dates)
for cost in (11, 25):
    A = tranche(cost)
    for t0, t1 in [("2015-01-01", "2026-10-31"), ("2008-07-01", "2026-10-31")]:
        base = monthly_mix([spy, bil], [0.5, 0.5]); c0, s0, d0 = stats(base, t0, t1)
        print(f"[{t0[:4]}+ @{cost}bp] monthly 50/50: CAGR {c0:.2%} Sharpe {s0:.3f} DD {d0:.1%}")
        for x in (0.1, 0.2, 0.3):
            m = monthly_mix([spy, bil, A], [0.5*(1-x), 0.5*(1-x), x]); c, s, d = stats(m, t0, t1)
            m2 = monthly_mix([spy, bil, A], [0.5, 0.5 - x, x]); c2, s2, d2 = stats(m2, t0, t1)
            print(f"   pro-rata {x:.0%} A: CAGR {c:.2%} Sh {s:.3f} DD {d:.1%} | replace-BIL {x:.0%} A: CAGR {c2:.2%} Sh {s2:.3f} DD {d2:.1%}")
