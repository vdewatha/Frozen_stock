"""Quick, transparent backtest of long-only cross-asset ETF trend/rotation rules.
Data: yfinance adjusted closes (total-return proxy). Not a pre-registered test.
Signal at close of signal day, executed at NEXT trading day's close; weights drift between rebalances.
Costs: c per unit one-way turnover (sum |dw|).
"""
import sys, warnings
import numpy as np, pandas as pd, yfinance as yf
warnings.filterwarnings("ignore")
OUTDIR = sys.argv[1]

RISKY8 = ["SPY", "EFA", "EEM", "IEF", "TLT", "GLD", "DBC", "VNQ"]
GTAA5 = ["SPY", "EFA", "IEF", "DBC", "VNQ"]
SECT = ["XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB"]
ALL = sorted(set(RISKY8 + GTAA5 + SECT + ["BIL", "AGG", "VEU", "SHY", "LQD", "HYG", "TIP"]))
px = yf.download(ALL, start="2006-06-01", end="2026-11-07", auto_adjust=True, progress=False)["Close"]
px = px.dropna(how="all").ffill()
px.to_csv(f"{OUTDIR}/px.csv")
start_data = px[RISKY8 + ["BIL", "AGG", "VEU"]].dropna().index[0]
px = px.loc[start_data:]
ret = px.pct_change().fillna(0.0)
dates = px.index
print("data from", dates[0].date(), "to", dates[-1].date())

def month_end_days(offset=0):
    s = pd.Series(dates, index=dates)
    g = s.groupby([dates.year, dates.month])
    out = []
    for _, d in g:
        if len(d) > offset:
            out.append(d.iloc[-1 - offset])
    return pd.DatetimeIndex(out)

def weekly_days():
    s = pd.Series(dates, index=dates)
    return pd.DatetimeIndex(s.groupby([dates.isocalendar().year, dates.isocalendar().week]).last().values)

def trailing_ret(t, L):
    i = dates.get_loc(t)
    if i - L < 0:
        return None
    return px.iloc[i] / px.iloc[i - L] - 1.0

def sig_gtaa(t, sma_days=(210,)):
    i = dates.get_loc(t)
    if i < max(sma_days):
        return None
    w = pd.Series(0.0, index=px.columns)
    for a in GTAA5:
        f = np.mean([px[a].iloc[i] > px[a].iloc[i - L + 1:i + 1].mean() for L in sma_days])
        w[a] = 0.2 * f
    w["BIL"] = 1 - w.sum()
    return w

def sig_tsmom_ens(t, looks=(21, 63, 126, 252), univ=RISKY8, volwin=63):
    i = dates.get_loc(t)
    if i < max(looks) + 1:
        return None
    frac = pd.Series(0.0, index=univ)
    for L in looks:
        r = px.iloc[i] / px.iloc[i - L] - 1.0
        frac += (r[univ] > r["BIL"]).astype(float)
    frac /= len(looks)
    vol = ret[univ].iloc[i - volwin + 1:i + 1].std() * np.sqrt(252)
    iv = (1 / vol) / (1 / vol).sum()
    w = pd.Series(0.0, index=px.columns)
    w[univ] = iv * frac
    w["BIL"] = 1 - w.sum()
    return w

def sig_gem(t, L=252):
    r = trailing_ret(t, L)
    if r is None:
        return None
    w = pd.Series(0.0, index=px.columns)
    if r["SPY"] > r["BIL"]:
        w["SPY" if r["SPY"] >= r["VEU"] else "VEU"] = 1.0
    else:
        w["AGG"] = 1.0
    return w

def sig_xs_top3(t, univ=RISKY8, k=3):
    i = dates.get_loc(t)
    if i < 253:
        return None
    r = {L: px.iloc[i] / px.iloc[i - L] - 1.0 for L in (21, 63, 126, 252)}
    m = (12 * r[21] + 4 * r[63] + 2 * r[126] + r[252]) / 4
    m = m[univ].sort_values(ascending=False)
    w = pd.Series(0.0, index=px.columns)
    for a in m.index[:k]:
        if m[a] > 0:
            w[a] = 1.0 / k
    w["BIL"] = 1 - w.sum()
    return w

def sig_static(weights):
    def f(t):
        w = pd.Series(0.0, index=px.columns)
        for a, x in weights.items():
            w[a] = x
        return w
    return f

def run(sigfun, sched, cost_bps=5.0):
    sched = set(sched)
    c = cost_bps / 1e4
    cols = px.columns
    R = ret.values
    w = np.zeros(len(cols)); w[cols.get_loc("BIL")] = 1.0
    pending = None
    out = np.zeros(len(dates)); turn = np.zeros(len(dates))
    for j, t in enumerate(dates):
        # return of today with yesterday-close weights
        g = (w * R[j]).sum()
        w = w * (1 + R[j]); w = w / w.sum() if w.sum() != 0 else w
        cost = 0.0
        if pending is not None:  # execute at today's close
            tw = pending.values
            to = np.abs(tw - w).sum()
            cost = to * c; turn[j] = to
            w = tw.copy(); pending = None
        out[j] = (1 + g) * (1 - cost) - 1
        if t in sched:
            s = sigfun(t)
            if s is not None:
                pending = s.reindex(cols).fillna(0.0)
    return pd.Series(out, index=dates), pd.Series(turn, index=dates)

def rsi2_book(cost_bps=5.0):
    """Proxy for a short-horizon long-only equity mean-reversion book (Connors-style RSI(2) on SPY)."""
    p = px["SPY"]; d = p.diff()
    up = d.clip(lower=0).ewm(alpha=1 / 2, adjust=False).mean(); dn = (-d.clip(upper=0)).ewm(alpha=1 / 2, adjust=False).mean()
    rsi = 100 - 100 / (1 + up / dn)
    sma200 = p.rolling(200).mean(); sma5 = p.rolling(5).mean()
    pos = np.zeros(len(p)); inpos = 0
    for j in range(len(p)):
        if inpos == 0 and rsi.iloc[j] < 10 and p.iloc[j] > sma200.iloc[j]:
            inpos = 1
        elif inpos == 1 and p.iloc[j] > sma5.iloc[j]:
            inpos = 0
        pos[j] = inpos
    pos = pd.Series(pos, index=p.index).shift(1).fillna(0)  # trade at close, earn next day
    tr = pos.diff().abs().fillna(0)
    r = pos * ret["SPY"] + (1 - pos) * ret["BIL"] - tr * cost_bps / 1e4
    return r, tr

def stats(r, t0, t1, turn=None):
    r = r.loc[t0:t1]; rf = ret["BIL"].loc[t0:t1]
    n = len(r) / 252
    cagr = (1 + r).prod() ** (1 / n) - 1
    vol = r.std() * np.sqrt(252)
    ex = r - rf
    sh = ex.mean() / ex.std() * np.sqrt(252) if ex.std() > 0 else np.nan
    eq = (1 + r).cumprod(); dd = (eq / eq.cummax() - 1).min()
    y22 = (1 + r.loc["2022"]).prod() - 1 if len(r.loc["2022"]) else np.nan
    to = turn.loc[t0:t1].sum() / n if turn is not None else np.nan
    return dict(CAGR=cagr, Vol=vol, Sharpe=sh, MaxDD=dd, Y2022=y22, TurnPerYr=to)

ME = month_end_days(0)
strats = {
    "SPY B&H": (sig_static({"SPY": 1.0}), ME),
    "60/40 SPY/IEF": (sig_static({"SPY": .6, "IEF": .4}), ME),
    "EW8 B&H (monthly rebal)": (sig_static({a: 1 / 8 for a in RISKY8}), ME),
    "GTAA5 10m SMA (Faber)": (lambda t: sig_gtaa(t, (210,)), ME),
    "GTAA5 SMA ensemble 6-12m": (lambda t: sig_gtaa(t, tuple(21 * k for k in range(6, 13))), ME),
    "TSMOM-ens IV 8 ETFs (1/3/6/12m)": (sig_tsmom_ens, ME),
    "TSMOM-ens IV 8 ETFs weekly": (sig_tsmom_ens, weekly_days()),
    "GEM 12m (SPY/VEU/AGG)": (sig_gem, ME),
    "XS top3 of 8, 13612W + abs filter": (sig_xs_top3, ME),
}
res = {}; turns = {}
for k, (f, s) in strats.items():
    res[k], turns[k] = run(f, s, 5.0)
res["RSI2 SPY proxy book"], turns["RSI2 SPY proxy book"] = rsi2_book(5.0)

rows = []
for win in [("2008-07-01", "2026-10-31"), ("2015-01-01", "2026-10-31")]:
    for k in res:
        d = stats(res[k], *win, turns[k]); d["Strategy"] = k; d["Window"] = f"{win[0][:4]}-{win[1][:7]}"
        rows.append(d)
tab = pd.DataFrame(rows).set_index(["Window", "Strategy"])
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 20)
print("\n=== Net of 5 bps per one-way turnover ===")
print(tab.round(3).to_string())

# cost sensitivity 2015+
print("\n=== Cost sensitivity 2015-01..2026-10 (CAGR) ===")
for k in ["GTAA5 10m SMA (Faber)", "TSMOM-ens IV 8 ETFs (1/3/6/12m)", "TSMOM-ens IV 8 ETFs weekly", "XS top3 of 8, 13612W + abs filter", "GEM 12m (SPY/VEU/AGG)"]:
    f, s = strats[k]
    line = []
    for cb in (0, 5, 25):
        r, tr = run(f, s, cb); line.append(f"{cb}bp:{stats(r,'2015-01-01','2026-10-31')['CAGR']:.3%}")
    print(k, " | ".join(line))

# calendar-year returns 2015+
yr = pd.DataFrame({k: (1 + v.loc["2015":"2026"]).groupby(v.loc["2015":"2026"].index.year).prod() - 1 for k, v in res.items()})
print("\n=== Calendar-year returns (2026 = YTD to early Nov) ===")
print((yr * 100).round(1).to_string())

# timing luck: monthly schedule offsets (k trading days before month end)
print("\n=== Rebalance-day sensitivity 2015-01..2026-10 (k = trading days before month-end), CAGR / MaxDD ===")
for k in ["GTAA5 10m SMA (Faber)", "TSMOM-ens IV 8 ETFs (1/3/6/12m)", "XS top3 of 8, 13612W + abs filter", "GEM 12m (SPY/VEU/AGG)"]:
    f, _ = strats[k]; cs = []
    for off in range(0, 16, 3):
        r, _t = run(f, month_end_days(off), 5.0); st = stats(r, "2015-01-01", "2026-10-31")
        cs.append((off, round(st["CAGR"] * 100, 2), round(st["MaxDD"] * 100, 1)))
    print(k, cs)

# correlations, monthly, 2015+
m = pd.DataFrame({k: (1 + v.loc["2015":"2026-10"]).resample("ME").prod() - 1 for k, v in res.items()})
print("\n=== Monthly return correlation 2015-01..2026-10 vs SPY and vs RSI2 proxy book ===")
print(m.corr()[["SPY B&H", "RSI2 SPY proxy book"]].round(2).to_string())
# conditional: strategy return in proxy-book's worst 10% months and SPY's worst 10% months
for ref in ["RSI2 SPY proxy book", "SPY B&H"]:
    q = m[ref].quantile(0.10); bad = m[m[ref] <= q]
    print(f"\nMean monthly return in {ref}'s worst-decile months (n={len(bad)}):")
    print((bad.mean() * 100).round(2).to_string())

# combos with proxy equity books
print("\n=== 50/50 combinations (daily rebalanced blend), 2015-01..2026-10 ===")
for book in ["RSI2 SPY proxy book", "SPY B&H"]:
    for k in ["GTAA5 10m SMA (Faber)", "TSMOM-ens IV 8 ETFs (1/3/6/12m)", "60/40 SPY/IEF"]:
        b = 0.5 * res[book] + 0.5 * res[k]
        st = stats(b, "2015-01-01", "2026-10-31"); s0 = stats(res[book], "2015-01-01", "2026-10-31")
        print(f"{book} alone: CAGR {s0['CAGR']:.2%} Sh {s0['Sharpe']:.2f} DD {s0['MaxDD']:.1%} || +50% {k}: CAGR {st['CAGR']:.2%} Sh {st['Sharpe']:.2f} DD {st['MaxDD']:.1%}")

# breadth: avg pairwise correlation and effective number of bets
def breadth(cols, t0="2015-01-01", t1="2026-10-31"):
    R = ret[cols].loc[t0:t1]
    wk = (1 + R).resample("W-FRI").prod() - 1
    C = wk.corr().values; n = len(cols)
    avg = (C.sum() - n) / (n * (n - 1))
    ev = np.linalg.eigvalsh(C); enb = ev.sum() ** 2 / (ev ** 2).sum()
    return avg, enb
for name, cols in [("8 cross-asset ETFs", RISKY8), ("9 SPDR sector ETFs", SECT)]:
    a, e = breadth(cols); print(f"\n{name}: avg pairwise weekly corr {a:.2f}, effective # bets {e:.1f} of {len(cols)}")
a, e = breadth(RISKY8, "2022-01-01", "2022-12-31"); print(f"8 cross-asset ETFs in 2022: avg corr {a:.2f}, eff bets {e:.1f}")
a, e = breadth(SECT, "2022-01-01", "2022-12-31"); print(f"9 sectors in 2022: avg corr {a:.2f}, eff bets {e:.1f}")

# stock-bond correlation by year
sb = pd.DataFrame({"SPY": ret["SPY"], "IEF": ret["IEF"], "TLT": ret["TLT"]}).loc["2015":"2026"]
print("\n=== SPY-IEF daily return correlation by year ===")
print(sb.groupby(sb.index.year).apply(lambda d: d["SPY"].corr(d["IEF"])).round(2).to_string())

# average invested fraction / BIL share for trend strategies
for k in ["GTAA5 10m SMA (Faber)", "TSMOM-ens IV 8 ETFs (1/3/6/12m)"]:
    f, s = strats[k]
    bil = [f(t)["BIL"] for t in s if t >= pd.Timestamp("2015-01-01") and f(t) is not None]
    print(f"{k}: avg cash (BIL) weight 2015+ = {np.mean(bil):.0%}, months with >50% cash = {np.mean(np.array(bil) > .5):.0%}")
