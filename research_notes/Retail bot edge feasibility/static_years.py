# Calendar-year returns and worst drawdown (2008-07 on) of the static SPY/T-bill arms that the
# report's ETF table matches to Designs A (27-28% SPY) and B (40% SPY), monthly rebalanced.
# Copied unchanged from the report writer's session scratchpad on 2026-11-07.
# Input: the px.csv price cache that etf_rotation_backtest.py writes to its output directory.
# Run: python3 -I static_years.py <dir>/px.csv   (output saved in static_years.out.txt)
import sys
sys.path.append('/Users/ainz_ool_gown/Library/Python/3.9/lib/python/site-packages')
import numpy as np, pandas as pd
px = pd.read_csv(sys.argv[1], index_col=0, parse_dates=True).dropna(how="all").ffill()
px = px.loc[px[["SPY","EFA","EEM","IEF","TLT","GLD","DBC","VNQ","BIL","AGG","VEU"]].dropna().index[0]:]
r = px.pct_change().fillna(0.0)
me = set(pd.Series(px.index, index=px.index).groupby([px.index.year, px.index.month]).last())
for w in (0.27, 0.28, 0.40):
    wt = np.array([w, 1-w]); out = []; R = r[["SPY","BIL"]].values
    for j, t in enumerate(px.index):
        g = (wt*R[j]).sum(); wt = wt*(1+R[j]); wt = wt/wt.sum(); out.append(g)
        if t in me: wt = np.array([w, 1-w])
    s = pd.Series(out, index=px.index)
    y = (1+s).groupby(s.index.year).prod()-1
    print(f"{int(w*100)}% SPY:", " ".join(f"{k}:{v*100:+.1f}" for k, v in y.items() if k >= 2008))
    eq = (1+s.loc["2008-07-01":]).cumprod(); dd = eq/eq.cummax()-1
    print("   worst DD date", dd.idxmin().date(), f"{dd.min():.1%}")
