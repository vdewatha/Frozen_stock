# Static SPY/T-bill blends, monthly rebalanced, no costs (turnover is about 0.2x a year).
# Source of the report's 50/50 figures (7.9% a year, -17.6% since 2015; -26.4% since 2008-07),
# its "1.1 points of return and 3.4 points of drawdown per 10 points of SPY" rule, and the
# drawdown per SPY weight. Copied from the report writer's session scratchpad on 2026-11-07 and
# extended with the 20% and 30% weights (the 50-100% rows are unchanged); the label now rounds
# the T-bill share, so "80% SPY / 20% BIL" no longer prints as 19%.
# Input: the px.csv price cache that etf_rotation_backtest.py writes to its output directory.
# Run: python3 -I static_check.py <dir>/px.csv   (output saved in static_check.out.txt)
import sys
sys.path.append('/Users/ainz_ool_gown/Library/Python/3.9/lib/python/site-packages')
import numpy as np, pandas as pd
px = pd.read_csv(sys.argv[1], index_col=0, parse_dates=True).dropna(how="all").ffill()
px = px.loc[px[["SPY","EFA","EEM","IEF","TLT","GLD","DBC","VNQ","BIL","AGG","VEU"]].dropna().index[0]:]
r = px.pct_change().fillna(0.0)
def stats(x, rf, t0, t1):
    x = x.loc[t0:t1]; rf = rf.loc[t0:t1]; n = len(x)/252
    c = (1+x).prod()**(1/n)-1; ex = x-rf; sh = ex.mean()/ex.std()*np.sqrt(252)
    eq = (1+x).cumprod(); dd = (eq/eq.cummax()-1).min(); return c, sh, dd
for w in (0.2, 0.3, 0.5, 0.6, 0.8, 1.0):
    # monthly rebalanced static blend, no costs (turnover ~0.2x/yr)
    me = set(pd.Series(px.index, index=px.index).groupby([px.index.year, px.index.month]).last())
    wt = np.array([w, 1-w]); out = []
    R = r[["SPY","BIL"]].values
    for j, t in enumerate(px.index):
        g = (wt*R[j]).sum(); wt = wt*(1+R[j]); wt = wt/wt.sum(); out.append(g)
        if t in me: wt = np.array([w, 1-w])
    s = pd.Series(out, index=px.index)
    for t0, t1 in (("2015-01-01","2026-10-31"), ("2008-07-01","2026-10-31")):
        c, sh, dd = stats(s, r["BIL"], t0, t1)
        print(f"{round(w*100)}% SPY / {round((1-w)*100)}% BIL {t0[:4]}+: CAGR {c:.2%} Sharpe {sh:.2f} MaxDD {dd:.1%}")
