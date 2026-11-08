"""Step 3's frozen rule: Designs A and B, the tranched backtest and the marginal-slice decision (pure).

``reports/Retail bot edge feasibility.md`` (build plan, step 3, "Settle the
defensive sleeve") pre-registered the test; ``reports/defensive-sleeve-20261107.md``
records the run and the outcome.  Everything here is pure pandas/numpy on a
frame of daily total-return closes (yfinance adjusted closes, the $0 data
budget), so the research script (``scripts/run_defensive_sleeve_check.py``) and
the in-app re-decision (``defensive_sleeve.redecide``) run the same code.

Designs, frozen as ``research_notes/Retail bot edge feasibility/etf_rotation.md``
(Q7) defines them and ``etf_rotation_backtest.py`` / ``verify_etf.py`` /
``blend_check.py`` implement them:

* **A, TSMOM-IV.**  Universe SPY, EFA, EEM, IEF, TLT, GLD, DBC, VNQ; cash BIL.
  Signal per asset: the fraction of the 21/63/126/252-session total returns
  that beat BIL's over the same window.  Weight: inverse 63-session realized
  volatility, normalized across the 8, times the signal; the rest in BIL.
* **B, GTAA5-ENS.**  20% each in SPY, EFA, IEF, DBC, VNQ, each held in
  proportion to the fraction of SMA lengths 126, 147, ..., 252 sessions for
  which the close is above its SMA; the rest in BIL.

Execution: the signal at one close is traded at the next close, weights drift
between rebalances, cost = ``cost_bps`` per unit of one-way turnover (sum of
|dw|).  Tranched: four sleeves of 25%, each rebalancing monthly on its own
offset (0, 5, 10 and 15 sessions before month-end), no cross-sleeve rebalance.

The arm: the passive arm at its live 20% equity weight, the rest in T-bills,
rebalanced monthly.  A design slice of x takes x of equity from the T-bill
leg: ``20% SPY + x design + (80% - x) BIL``.  ``RULE`` is the decision.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Optional

import numpy as np
import pandas as pd

REPORT = "reports/defensive-sleeve-20261107.md"
DESIGN_A = "tsmom_iv"
DESIGN_B = "gtaa5_ens"
DESIGN_NAMES = {DESIGN_A: "Design A, TSMOM-IV", DESIGN_B: "Design B, GTAA5-ENS"}
RISKY8 = ("SPY", "EFA", "EEM", "IEF", "TLT", "GLD", "DBC", "VNQ")
GTAA5 = ("SPY", "EFA", "IEF", "DBC", "VNQ")
CASH = "BIL"
EQUITY = "SPY"
SYMBOLS = RISKY8 + (CASH,)
TSMOM_LOOKBACKS = (21, 63, 126, 252)
VOL_WINDOW = 63
GTAA_SMA_LENGTHS = tuple(21 * k for k in range(6, 13))
TRANCHE_OFFSETS = (0, 5, 10, 15)
COSTS_BPS = (11.0, 25.0)
WINDOW_STARTS = {"2008-07+": "2008-07-01", "2015+": "2015-01-01"}
PASSIVE_EQUITY_WEIGHT = 0.20
SLICE = 0.20
SLICES_REPORTED = (0.10, 0.20, 0.30)
MAX_EXTRA_DRAWDOWN = 0.02
REDECIDE_MONTHS = 36
SPY_DRAWDOWN_TRIGGER = 0.20
RULE = (f"A defensive design (A TSMOM-IV or B GTAA5-ENS, frozen as the ETF note defines them, four monthly "
        f"tranches) takes {SLICE:.0%} of equity from the passive arm's T-bill leg only if, at both "
        f"{COSTS_BPS[0]:g} and {COSTS_BPS[1]:g} bps a side and in both windows (2008-07 to latest, 2015 to "
        f"latest), the {SLICE:.0%} slice, rebalanced monthly, raises the arm's Sharpe ({PASSIVE_EQUITY_WEIGHT:.0%} "
        f"SPY, rest T-bills) with no more than {MAX_EXTRA_DRAWDOWN * 100:g} extra points of maximum drawdown. "
        f"Otherwise the static arm keeps the capital and the design runs as a zero-capital shadow, re-decided once "
        f"under this rule at {REDECIDE_MONTHS} months or after the next SPY drawdown deeper than "
        f"{SPY_DRAWDOWN_TRIGGER:.0%}, whichever comes first.")

# The 2026-11-07 run (data through 2026-11-06, reports/defensive-sleeve-20261107.md).  The live
# sleeve reads its built outcome from here; ``passes`` is the rule above at each cell.
DESIGN_DECISIONS = {'tsmom_iv': {'passes': False,
              'passes_50_50': False,
              'cells': {'11bps|2008-07+': {'arm_sharpe': 0.6,
                                           'slice_sharpe': 0.648,
                                           'arm_max_drawdown': -0.1109,
                                           'slice_max_drawdown': -0.1072,
                                           'passes': True,
                                           'arm_50_50': {'arm_sharpe': 0.603,
                                                         'slice_sharpe': 0.627,
                                                         'arm_max_drawdown': -0.2644,
                                                         'slice_max_drawdown': -0.2608,
                                                         'passes': True}},
                        '11bps|2015+': {'arm_sharpe': 0.683,
                                        'slice_sharpe': 0.665,
                                        'arm_max_drawdown': -0.0712,
                                        'slice_max_drawdown': -0.0871,
                                        'passes': False,
                                        'arm_50_50': {'arm_sharpe': 0.684,
                                                      'slice_sharpe': 0.679,
                                                      'arm_max_drawdown': -0.1763,
                                                      'slice_max_drawdown': -0.1911,
                                                      'passes': False}},
                        '25bps|2008-07+': {'arm_sharpe': 0.6,
                                           'slice_sharpe': 0.62,
                                           'arm_max_drawdown': -0.1109,
                                           'slice_max_drawdown': -0.1077,
                                           'passes': True,
                                           'arm_50_50': {'arm_sharpe': 0.603,
                                                         'slice_sharpe': 0.615,
                                                         'arm_max_drawdown': -0.2644,
                                                         'slice_max_drawdown': -0.2613,
                                                         'passes': True}},
                        '25bps|2015+': {'arm_sharpe': 0.683,
                                        'slice_sharpe': 0.634,
                                        'arm_max_drawdown': -0.0712,
                                        'slice_max_drawdown': -0.0872,
                                        'passes': False,
                                        'arm_50_50': {'arm_sharpe': 0.684,
                                                      'slice_sharpe': 0.666,
                                                      'arm_max_drawdown': -0.1763,
                                                      'slice_max_drawdown': -0.1912,
                                                      'passes': False}}}},
 'gtaa5_ens': {'passes': False,
               'passes_50_50': False,
               'cells': {'11bps|2008-07+': {'arm_sharpe': 0.6,
                                            'slice_sharpe': 0.6,
                                            'arm_max_drawdown': -0.1109,
                                            'slice_max_drawdown': -0.1162,
                                            'passes': True,
                                            'arm_50_50': {'arm_sharpe': 0.603,
                                                          'slice_sharpe': 0.608,
                                                          'arm_max_drawdown': -0.2644,
                                                          'slice_max_drawdown': -0.2672,
                                                          'passes': True}},
                         '11bps|2015+': {'arm_sharpe': 0.683,
                                         'slice_sharpe': 0.63,
                                         'arm_max_drawdown': -0.0712,
                                         'slice_max_drawdown': -0.0981,
                                         'passes': False,
                                         'arm_50_50': {'arm_sharpe': 0.684,
                                                       'slice_sharpe': 0.663,
                                                       'arm_max_drawdown': -0.1763,
                                                       'slice_max_drawdown': -0.2013,
                                                       'passes': False}},
                         '25bps|2008-07+': {'arm_sharpe': 0.6,
                                            'slice_sharpe': 0.58,
                                            'arm_max_drawdown': -0.1109,
                                            'slice_max_drawdown': -0.1165,
                                            'passes': False,
                                            'arm_50_50': {'arm_sharpe': 0.603,
                                                          'slice_sharpe': 0.599,
                                                          'arm_max_drawdown': -0.2644,
                                                          'slice_max_drawdown': -0.2673,
                                                          'passes': False}},
                         '25bps|2015+': {'arm_sharpe': 0.683,
                                         'slice_sharpe': 0.607,
                                         'arm_max_drawdown': -0.0712,
                                         'slice_max_drawdown': -0.0983,
                                         'passes': False,
                                         'arm_50_50': {'arm_sharpe': 0.684,
                                                       'slice_sharpe': 0.653,
                                                       'arm_max_drawdown': -0.1763,
                                                       'slice_max_drawdown': -0.2015,
                                                       'passes': False}}}}}
DECISION = {
    "decided_on": "2026-11-07",
    "data_end": "2026-11-06",
    "arm": "20% SPY / 80% T-bills (the live passive arm); 50/50 shown beside it",
    "designs": DESIGN_DECISIONS,
    "funded": [name for name, body in DESIGN_DECISIONS.items() if body["passes"]],
    "shadow": DESIGN_A,
    "outcome": ("Both designs fail: every 2015+ cell lowers the arm's Sharpe, at both costs and under both arms. "
                "The static arm keeps the capital; Design A runs as a zero-capital shadow."),
}


# ------------------------------------------------------------------ signals

def _frame(closes: pd.DataFrame) -> pd.DataFrame:
    return closes.sort_index().ffill()


def design_a_weights(closes: pd.DataFrame) -> Optional[dict[str, float]]:
    """Design A's target weights from closes through the signal day (last row); None until 253 sessions."""
    px = _frame(closes)
    i = len(px) - 1
    if i < max(TSMOM_LOOKBACKS) + 1 or any(s not in px.columns for s in SYMBOLS):
        return None
    last = px.iloc[i]
    frac = pd.Series(0.0, index=list(RISKY8))
    for lookback in TSMOM_LOOKBACKS:
        r = last / px.iloc[i - lookback] - 1.0
        frac += (r[list(RISKY8)] > r[CASH]).astype(float)
    frac /= len(TSMOM_LOOKBACKS)
    rets = px[list(RISKY8)].iloc[i - VOL_WINDOW:i + 1].pct_change().iloc[1:]
    vol = rets.std() * np.sqrt(252)
    if not np.all(np.isfinite(vol)) or (vol <= 0).any():
        return None
    inv = (1 / vol) / (1 / vol).sum()
    weights = {symbol: float(inv[symbol] * frac[symbol]) for symbol in RISKY8}
    weights[CASH] = 1.0 - sum(weights.values())
    return weights


def design_b_weights(closes: pd.DataFrame) -> Optional[dict[str, float]]:
    """Design B's target weights from closes through the signal day (last row); None until 252 sessions."""
    px = _frame(closes)
    i = len(px) - 1
    if i < max(GTAA_SMA_LENGTHS) or any(s not in px.columns for s in GTAA5 + (CASH,)):
        return None
    weights = {}
    for symbol in GTAA5:
        series = px[symbol].to_numpy(float)
        frac = np.mean([series[i] > series[i - length + 1:i + 1].mean() for length in GTAA_SMA_LENGTHS])
        weights[symbol] = 0.2 * float(frac)
    weights[CASH] = 1.0 - sum(weights.values())
    return weights


SIGNALS = {DESIGN_A: design_a_weights, DESIGN_B: design_b_weights}


# ---------------------------------------------------------------- backtest

def month_end_days(dates: pd.DatetimeIndex, offset: int = 0) -> pd.DatetimeIndex:
    """The session ``offset`` sessions before each month's last session."""
    s = pd.Series(dates, index=dates)
    out = [d.iloc[-1 - offset] for _, d in s.groupby([dates.year, dates.month]) if len(d) > offset]
    return pd.DatetimeIndex(out)


def signal_table(px: pd.DataFrame, design: str, days: Iterable[pd.Timestamp]) -> dict[pd.Timestamp, np.ndarray]:
    """Weights (in ``px.columns`` order) per signal day; days without enough history are skipped."""
    fn = SIGNALS[design]
    cols = list(px.columns)
    out = {}
    for day in sorted(set(days)):
        i = px.index.get_loc(day)
        weights = fn(px.iloc[:i + 1])
        if weights is not None:
            out[day] = np.array([weights.get(c, 0.0) for c in cols])
    return out


def simulate(px: pd.DataFrame, signals: Mapping[pd.Timestamp, np.ndarray], schedule: Iterable[pd.Timestamp],
             cost_bps: float) -> pd.Series:
    """One sleeve: start in cash, trade each scheduled signal at the next close, weights drift, cost per side."""
    cols = list(px.columns)
    rets = px.pct_change().fillna(0.0).to_numpy(float)
    sched = set(schedule)
    c = cost_bps / 1e4
    w = np.zeros(len(cols))
    w[cols.index(CASH)] = 1.0
    pending = None
    out = np.zeros(len(px))
    for j, day in enumerate(px.index):
        g = float((w * rets[j]).sum())
        w = w * (1 + rets[j])
        total = w.sum()
        w = w / total if total != 0 else w
        cost = 0.0
        if pending is not None:
            cost = float(np.abs(pending - w).sum()) * c
            w = pending.copy()
            pending = None
        out[j] = (1 + g) * (1 - cost) - 1
        if day in sched and day in signals:
            pending = signals[day]
    return pd.Series(out, index=px.index)


def tranched(px: pd.DataFrame, design: str, cost_bps: float, *, start: Optional[str] = None,
             signals: Optional[Mapping] = None) -> pd.Series:
    """Four 25% sleeves on offsets 0/5/10/15 before month-end, equal at ``start`` (or the data start), no cross-rebalance."""
    days = {o: month_end_days(px.index, o) for o in TRANCHE_OFFSETS}
    if signals is None:
        signals = signal_table(px, design, [d for v in days.values() for d in v])
    curves = []
    for offset in TRANCHE_OFFSETS:
        r = simulate(px, signals, days[offset], cost_bps)
        if start is not None:
            r = r.loc[start:]
        curves.append((1 + r).cumprod())
    eq = pd.concat(curves, axis=1).mean(axis=1)
    out = eq.pct_change()
    out.iloc[0] = eq.iloc[0] - 1 if start is not None else 0.0
    return out


def monthly_mix(streams: list[pd.Series], weights: list[float], dates: pd.DatetimeIndex) -> pd.Series:
    """Daily return of a mix of return streams reset to ``weights`` at each month-end close (no cost)."""
    me = set(month_end_days(dates, 0))
    target = np.array(weights, float)
    wt = target.copy()
    R = np.column_stack([s.reindex(dates).fillna(0.0).to_numpy(float) for s in streams])
    out = np.zeros(len(dates))
    for j, day in enumerate(dates):
        out[j] = float((wt * R[j]).sum())
        wt = wt * (1 + R[j])
        wt = wt / wt.sum()
        if day in me:
            wt = target.copy()
    return pd.Series(out, index=dates)


def stats(r: pd.Series, rf: pd.Series, start: str, end: Optional[str] = None) -> dict:
    """CAGR, volatility, Sharpe (daily excess over T-bills, annualized), max drawdown over the window."""
    r = r.loc[start:end]
    rf = rf.reindex(r.index).fillna(0.0)
    n = len(r) / 252
    ex = r - rf
    eq = (1 + r).cumprod()
    return {"cagr": float((1 + r).prod() ** (1 / n) - 1), "vol": float(r.std() * np.sqrt(252)),
            "sharpe": float(ex.mean() / ex.std() * np.sqrt(252)) if ex.std() > 0 else float("nan"),
            "max_drawdown": float((eq / eq.cummax() - 1).min()), "start": str(r.index[0].date()),
            "end": str(r.index[-1].date()), "sessions": int(len(r))}


def arm(px: pd.DataFrame, design_returns: Optional[pd.Series], *, equity_weight: float, slice_: float) -> pd.Series:
    """``equity_weight`` SPY + ``slice_`` design + the rest BIL, rebalanced monthly."""
    rets = px.pct_change().fillna(0.0)
    if design_returns is None or slice_ == 0:
        return monthly_mix([rets[EQUITY], rets[CASH]], [equity_weight, 1 - equity_weight], px.index)
    return monthly_mix([rets[EQUITY], rets[CASH], design_returns], [equity_weight, 1 - equity_weight - slice_, slice_],
                       px.index)


def slice_passes(base: Mapping, sliced: Mapping) -> bool:
    """The rule at one cell: Sharpe strictly higher, drawdown no more than 2 points deeper."""
    return bool(sliced["sharpe"] > base["sharpe"]
                and sliced["max_drawdown"] >= base["max_drawdown"] - MAX_EXTRA_DRAWDOWN)


def evaluate(px: pd.DataFrame, *, end: Optional[str] = None, equity_weight: float = PASSIVE_EQUITY_WEIGHT,
             designs: Iterable[str] = (DESIGN_A, DESIGN_B), costs: Iterable[float] = COSTS_BPS,
             windows: Mapping[str, str] = WINDOW_STARTS, slices: Iterable[float] = SLICES_REPORTED) -> dict:
    """The pre-registered test: per design, cost and window, the base arm and each slice; ``passes`` per design."""
    px = px[list(SYMBOLS)].dropna(how="all").ffill().dropna()
    rf = px[CASH].pct_change().fillna(0.0)
    base = arm(px, None, equity_weight=equity_weight, slice_=0)
    all_days = [d for o in TRANCHE_OFFSETS for d in month_end_days(px.index, o)]
    out = {"equity_weight": equity_weight, "data_end": str(px.index[-1].date()), "designs": {}}
    for design in designs:
        sig = signal_table(px, design, all_days)
        cells, ok = {}, True
        for cost in costs:
            stream = tranched(px, design, cost, signals=sig)
            for label, start in windows.items():
                b = stats(base, rf, start, end)
                cell = {"base": b, "standalone": stats(tranched(px, design, cost, start=start, signals=sig), rf,
                                                       start, end), "slices": {}}
                for x in slices:
                    s = stats(arm(px, stream, equity_weight=equity_weight, slice_=x), rf, start, end)
                    cell["slices"][f"{x:.2f}"] = s | {"passes": slice_passes(b, s),
                                                      "sharpe_change": s["sharpe"] - b["sharpe"],
                                                      "extra_drawdown_points": (b["max_drawdown"] - s["max_drawdown"]) * 100}
                cell["passes"] = cell["slices"][f"{SLICE:.2f}"]["passes"]
                ok = ok and cell["passes"]
                cells[f"{cost:g}bps|{label}"] = cell
        out["designs"][design] = {"cells": cells, "passes": ok}
    return out


def spy_drawdown(closes: pd.Series) -> dict:
    """SPY's current drawdown from its running peak and the deepest since the series starts."""
    s = closes.dropna().astype(float)
    if s.empty:
        return {"current": None, "deepest": None}
    dd = s / s.cummax() - 1
    return {"current": float(dd.iloc[-1]), "deepest": float(dd.min()), "peak": float(s.max()),
            "last": float(s.iloc[-1]), "as_of": str(pd.Timestamp(s.index[-1]).date())}
