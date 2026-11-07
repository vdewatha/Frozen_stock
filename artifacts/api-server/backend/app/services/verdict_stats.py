"""Always-valid verdicts for adopted changes: betting e-processes, e-LOND, a 120-trade cap.

The experiment engine's forward verdict (``experiment_engine.judge``) compares
the mean of the first 20 trades after a change with the 20 before it and
reverts at 10 bps worse.  At the per-trade dispersion the bot's strategies
actually have (``reports/verdict-inputs-20261107.md``) that comparison cannot
see an edge of a few tens of bps, and repeated looks inflate its error rate.
This module is the judge that replaces it once its shadow record is good
enough (``experiment_verdict_method``):

1. **Observations.**  Per decision, the change minus the value it replaced
   (``setup_shadow``: the same BUY decision scored under both values, so the
   difference is the gate decision alone), SPY-adjusted over the same hold
   unless the template is a market-timing gate (``MARKET_TIMING_TEMPLATES``,
   whose whole mechanism is the market move a residual would subtract).
   Templates without a shadow (exits, scope, book) fall back to the live
   residual trades after the change minus the residual mean of the 20 before.
   Trades entered on the same date are averaged into one observation
   (``cluster_by_entry_date``): the measured residual same-date correlation
   is what makes them one bet, and the average of bounded values stays in
   the bound.  Observations enter the process when their last trade closes.
2. **Harm test** (``betting_e_process(side="less")``): the one-sided capital
   process of Waudby-Smith & Ramdas (2023) against "the change is not worse";
   alarm, and revert, once it reaches ``1 / HARM_ALPHA`` (Ville's inequality,
   valid at any stopping time: Howard et al. 2021, Lemma 3, p. 19).
3. **Improvement test** (``side="greater"``) at the e-LOND level of Xu &
   Ramdas (2024): the j-th adopted change is confirmed when its e-process
   reaches ``1 / (alpha * gamma_j * (D + 1))``, D = earlier changes already
   confirmed; false confirmations are FDR-controlled at ``IMPROVEMENT_FDR_ALPHA``
   under arbitrary dependence between changes (they share history).
4. **Loss bound and alarm.**  The bounded process needs every observation in
   ``[-B, B]``.  ``B`` is the measured realistic worst-case loss given the
   stops (gaps through the stop included).  An observation outside it raises
   ``bound_breach``; from then on both tests read the heavy-tailed Catoni-style
   process of Wang & Ramdas (2023), which needs only a variance bound.
5. **Cap** (``CAP_TRADES``): neither test crossed by 120 trades -> keep only
   if the one-sided lower confidence bound on the mean difference is above
   ``KEEP_LCB_BPS`` (-10 bps), else revert.

Measured inputs come from ``scripts/measure_verdict_inputs.py``: the
constants in ``MEASURED_INPUTS`` (dated), overridden by the latest
``verdict_inputs`` audit row when one is recorded.  e-LOND state is an
``e_lond`` audit row.  Audit rows only, no migration.  Paper only: nothing
here places, blocks or exits an order.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime, timezone
import logging
import math
from typing import Any, Iterable, Mapping, Optional, Sequence

import numpy as np

logger = logging.getLogger(__name__)
UTC = timezone.utc

EVENT = "verdict_stats"
ACTION_INPUTS = "verdict_inputs"
ACTION_ELOND = "e_lond"
METHOD_FIXED = "fixed_20"
METHOD_E_PROCESS = "e_process"
METHODS = (METHOD_FIXED, METHOD_E_PROCESS)

HARM_ALPHA = 0.10
IMPROVEMENT_FDR_ALPHA = 0.10
# The bet sizes' tuning level.  Any predictable bet keeps the process valid;
# a fixed constant keeps the bets independent of other changes' outcomes.
BET_ALPHA = 0.05
# c in Waudby-Smith & Ramdas Theorem 3 (their defaults 1/2 or 3/4).  3/4: with the loss bound
# B far above the per-trade std the truncation c/m binds, and 3/4 bought more power than 1/2 at
# the measured inputs with the null crossing rate still under alpha (tests/test_verdict_stats.py).
BET_TRUNCATION = 0.75
CAP_TRADES = 120
KEEP_LCB_BPS = -10.0
CAP_ALPHA = 0.10              # one-sided level of the cap's lower confidence bound
MARKET_TIMING_TEMPLATES = frozenset({"market_gate", "vix_gate", "breadth_gate"})
OPEN_EXIT_BARRIERS = frozenset({"vertical", "indicator", "stop_loss_gap", "take_profit_gap"})
REVERSION_CLASS = "reversion"
TECHNICAL_CLASS = "technical"

# Measured on 2026-11-07 (reports/verdict-inputs-20261107.md; replay of the live
# strategies on the point-in-time universe, the bot's own ledger was empty).
# ``loss_bound_bps``: the realistic worst-case per-trade SPY-adjusted loss given the stops
# (gaps through the stop included), per strategy class; ``sigma_bps``: an upper
# bound on the per-trade standard deviation of the paired difference (the
# Catoni fallback's variance bound); ``rho``: residual same-date correlation.
MEASURED_INPUTS: dict = {
    "as_of": "2026-11-07",
    "source": "replay_point_in_time",
    "loss_bound_bps": {TECHNICAL_CLASS: 1200.0, REVERSION_CLASS: 2500.0},
    "sigma_bps": {TECHNICAL_CLASS: 450.0, REVERSION_CLASS: 650.0},
    "rho": 0.10,
    "per_trade_std_bps": {},
    "paired_std_bps": {},
}


def _f(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def strategy_class(strategy_type: Optional[str]) -> str:
    from app.services.gate_replay import REVERSION

    return REVERSION_CLASS if strategy_type in REVERSION else TECHNICAL_CLASS


# ------------------------------------------------------- betting e-process

def predictable_bets(x: Sequence[float], alpha: float = BET_ALPHA, *, n_planned: Optional[int] = None,
                     known: Optional[Sequence[int]] = None) -> np.ndarray:
    """The predictable plug-in bets lambda~_t of Waudby-Smith & Ramdas (2023) for [0, 1] data.

    Time-uniform (``n_planned`` None): eq. (26), p. 17,
    ``sqrt(2 log(1/alpha) / (sigma^2_{t-1} t log(t + 1)))``; fixed horizon:
    eq. (27), p. 17, ``sqrt(2 log(1/alpha) / (n sigma^2_{t-1}))``.  Both with
    the regularised running moments ``mu_t = (1/2 + sum x_i) / (t + 1)`` and
    ``sigma^2_t = (1/4 + sum (x_i - mu_i)^2) / (t + 1)`` (eq. 26), and
    ``log(1/alpha)`` in place of ``log(2/alpha)`` because each test here is
    one-sided.  Any predictable choice is valid (Proposition 2 (d), p. 14);
    this one is the recommended tuning.

    ``known[k]`` (default ``k``) is how many earlier observations were closed
    when observation ``k`` was opened: its bet uses only those, as if it were
    the ``known[k] + 1``-th bet, so a trade's size never reads trades that
    closed while it was already open (overlapping holds).
    """
    x = np.asarray(x, dtype=float)
    n = x.size
    if n == 0:
        return np.zeros(0)
    t = np.arange(1, n + 1, dtype=float)
    mu = (0.5 + np.cumsum(x)) / (t + 1)
    sig2 = (0.25 + np.cumsum((x - mu) ** 2)) / (t + 1)
    p = np.arange(n) if known is None else np.minimum(np.asarray(known, dtype=int), np.arange(n))
    prev = np.where(p > 0, sig2[np.maximum(p - 1, 0)], 0.25)   # sigma^2 after the p known observations
    step = p.astype(float) + 1.0                                # the bet is the (p + 1)-th
    log_term = math.log(1.0 / alpha)
    if n_planned:
        return np.sqrt(2 * log_term / (float(n_planned) * prev))
    return np.sqrt(2 * log_term / (prev * step * np.log1p(step)))


def log_capital(x: Sequence[float], m: float, bets: Sequence[float], side: str,
                c: float = BET_TRUNCATION) -> np.ndarray:
    """log K_t(m), t = 0..n, of the one-sided capital process (Waudby-Smith & Ramdas 2023, eqs. 23-25, pp. 13-16).

    ``side="greater"`` bets that the mean exceeds ``m``:
    ``K+_t = prod (1 + lambda_i (x_i - m))`` with ``lambda_i = min(|lambda~_i|, c / m)``;
    ``side="less"`` bets that it falls short:
    ``K-_t = prod (1 - lambda_i (x_i - m))`` with ``lambda_i = min(|lambda~_i|, c / (1 - m))``
    (eq. 25).  Under ``H0: mean <= m`` (resp. ``>= m``) each is a nonnegative
    supermartingale starting at 1 (Proposition 2, with nonnegative bets the
    composite null only lowers the drift); the truncation at ``c < 1`` keeps
    every factor at least ``1 - c`` > 0.  This is Theorem 3's hedged process
    with ``theta = 1`` (resp. 0): one side only.
    """
    x = np.asarray(x, dtype=float)
    bets = np.abs(np.asarray(bets, dtype=float))
    if side == "greater":
        lam = np.minimum(bets, c / m) if m > 0 else bets
        factors = 1.0 + lam * (x - m)
    elif side == "less":
        lam = np.minimum(bets, c / (1.0 - m)) if m < 1 else bets
        factors = 1.0 - lam * (x - m)
    else:
        raise ValueError(f"side must be 'greater' or 'less', not {side!r}")
    return np.concatenate([[0.0], np.cumsum(np.log(np.maximum(factors, 1e-300)))])


def to_unit(values_bps: Sequence[float], bound_bps: float) -> np.ndarray:
    """Map ``[-B, B]`` bps to ``[0, 1]``."""
    return (np.asarray(values_bps, dtype=float) + bound_bps) / (2.0 * bound_bps)


def first_breach(values_bps: Sequence[float], bound_bps: float) -> Optional[int]:
    """Index of the first observation outside ``[-B, B]`` (the alarm), or None."""
    values = np.asarray(values_bps, dtype=float)
    out = np.flatnonzero(np.abs(values) > bound_bps)
    return int(out[0]) if out.size else None


def betting_e_process(values_bps: Sequence[float], *, bound_bps: float, side: str, null_bps: float = 0.0,
                      bet_alpha: float = BET_ALPHA, c: float = BET_TRUNCATION,
                      known: Optional[Sequence[int]] = None) -> np.ndarray:
    """e_t, t = 1..n, against ``H0: mean <= null`` (``greater``) or ``mean >= null`` (``less``).

    ``values_bps`` must lie in ``[-B, B]``; the caller stops at the first
    breach (``first_breach``).  Waudby-Smith & Ramdas (2023), eqs. 23-26.
    ``known``: see ``predictable_bets``.
    """
    x = to_unit(values_bps, bound_bps)
    m = (null_bps + bound_bps) / (2.0 * bound_bps)
    return np.exp(log_capital(x, m, predictable_bets(x, bet_alpha, known=known), side, c)[1:])


def known_before_open(clusters: Sequence["Observation"]) -> list[int]:
    """Per observation, how many earlier ones had closed before it was entered (strictly before its entry date)."""
    closes = [str(o.available) for o in clusters]
    out = []
    for k, obs in enumerate(clusters):
        out.append(min(k, bisect_left(closes, str(obs.entry_date), 0, k)))
    return out


def lower_confidence_bound_bps(values_bps: Sequence[float], *, bound_bps: float, alpha: float = CAP_ALPHA,
                               n_planned: Optional[int] = None, c: float = BET_TRUNCATION,
                               tol: float = 1e-7, known: Optional[Sequence[int]] = None) -> Optional[float]:
    """One-sided lower confidence bound (bps) from the capital process: ``inf {m : max_{i<=n} K+_i(m) < 1/alpha}``.

    Waudby-Smith & Ramdas (2023) Theorem 3 (p. 16) with ``theta = 1`` and
    the running intersection; with ``n_planned`` the bets are eq. (27)'s
    fixed-horizon ones (Remark 3, p. 17), the right tuning for a cap fixed in
    advance.  ``K+_i(m)`` is nonincreasing in ``m`` (each factor is), so the
    bound is found by bisection.  None without data.
    """
    values = np.asarray(values_bps, dtype=float)
    if values.size == 0:
        return None
    x = to_unit(values, bound_bps)
    bets = predictable_bets(x, alpha, n_planned=n_planned, known=known)
    threshold = math.log(1.0 / alpha)

    def rejected(m: float) -> bool:
        return float(np.max(log_capital(x, m, bets, "greater", c))) >= threshold

    lo, hi = 1e-9, 1.0 - 1e-9
    if not rejected(lo):
        return -bound_bps
    if rejected(hi):
        return bound_bps
    while hi - lo > tol:
        mid = 0.5 * (lo + hi)
        if rejected(mid):
            lo = mid
        else:
            hi = mid
    return float(hi * 2.0 * bound_bps - bound_bps)


# ------------------------------------------------------ heavy-tailed fallback

def catoni_influence(x: np.ndarray) -> np.ndarray:
    """phi(x) = log(1 + x + x^2/2) for x >= 0, -log(1 - x + x^2/2) below (Wang & Ramdas 2023, eq. 29, p. 8)."""
    x = np.asarray(x, dtype=float)
    return np.where(x >= 0, np.log1p(x + x * x / 2), -np.log1p(-x + x * x / 2))


def catoni_bets(n: int, sigma: float, alpha: float = BET_ALPHA) -> np.ndarray:
    """lambda_t of Wang & Ramdas (2023) eq. (33), p. 8, one-sided (log(1/alpha)), nonrandom.

    ``lambda_t = sqrt(2 L / (t (sigma^2 + eta_t^2)))``, ``eta_t = sqrt(2 sigma^2 L / (t - 2 L))``;
    as in the paper's experiments (footnote 4, p. 12) the index is floored
    (there at 9) so that ``t > 2 L``.
    """
    big_l = math.log(1.0 / alpha)
    floor = max(9, int(math.floor(2 * big_l)) + 2)
    t = np.maximum(np.arange(1, n + 1, dtype=float), floor)
    eta2 = 2 * sigma * sigma * big_l / (t - 2 * big_l)
    return np.sqrt(2 * big_l / (t * (sigma * sigma + eta2)))


def catoni_e_process(values_bps: Sequence[float], *, sigma_bps: float, side: str, null_bps: float = 0.0,
                     bet_alpha: float = BET_ALPHA) -> np.ndarray:
    """e_t of the Catoni supermartingales (Wang & Ramdas 2023, Lemma 8, eqs. 30-31, p. 8).

    ``greater`` (H0: mean <= null): ``prod exp(phi(lambda_i (X_i - m)) - lambda_i^2 sigma^2 / 2)``;
    ``less`` (H0: mean >= null): ``prod exp(-phi(lambda_i (X_i - m)) - lambda_i^2 sigma^2 / 2)``.
    Needs only ``E[(X_t - mu)^2 | F_{t-1}] <= sigma^2`` (their Assumption 2); a
    composite null keeps the supermartingale property because phi is increasing.
    """
    values = np.asarray(values_bps, dtype=float)
    if values.size == 0:
        return np.zeros(0)
    lam = catoni_bets(values.size, sigma_bps, bet_alpha)
    sign = 1.0 if side == "greater" else -1.0
    if side not in ("greater", "less"):
        raise ValueError(f"side must be 'greater' or 'less', not {side!r}")
    log_m = np.cumsum(sign * catoni_influence(lam * (values - null_bps)) - lam * lam * sigma_bps * sigma_bps / 2)
    return np.exp(np.minimum(log_m, 700.0))


def catoni_lower_bound_bps(values_bps: Sequence[float], *, sigma_bps: float, alpha: float = CAP_ALPHA,
                           tol: float = 1e-4) -> Optional[float]:
    """One-sided lower bound from eq. (32) (Wang & Ramdas 2023, Theorem 9): smallest m with M^C_t(m) < 1/alpha."""
    values = np.asarray(values_bps, dtype=float)
    if values.size == 0:
        return None
    lam = catoni_bets(values.size, sigma_bps, alpha)
    penalty = float(np.sum(lam * lam)) * sigma_bps * sigma_bps / 2 + math.log(1.0 / alpha)

    def rejected(m: float) -> bool:
        return float(np.sum(catoni_influence(lam * (values - m)))) >= penalty

    lo, hi = float(values.min()) - 10 * sigma_bps, float(values.max()) + 10 * sigma_bps
    if not rejected(lo):
        return lo
    while hi - lo > tol:
        mid = 0.5 * (lo + hi)
        if rejected(mid):
            lo = mid
        else:
            hi = mid
    return float(hi)


# ------------------------------------------------------------------ e-LOND

def elond_gamma(j: int) -> float:
    """The discount sequence ``gamma_j = 1 / (j (j + 1))`` (sums to 1), the choice in Xu & Ramdas (2024), App. B, p. 24."""
    j = int(j)
    if j < 1:
        raise ValueError("e-LOND indices start at 1")
    return 1.0 / (j * (j + 1))


def elond_level(j: int, discoveries_before: int, alpha: float = IMPROVEMENT_FDR_ALPHA) -> float:
    """``alpha_j = alpha gamma_j (|R_{j-1}| + 1)`` (Xu & Ramdas 2024, Section 3.2, p. 7; FDR <= alpha by Theorem 1)."""
    return float(alpha) * elond_gamma(j) * (int(discoveries_before) + 1)


# ------------------------------------------------------------ observations

@dataclass(frozen=True)
class Observation:
    value_bps: float
    entry_date: str
    available: str      # the date the trade closed (when its value becomes known)
    trades: int = 1


def cluster_by_entry_date(observations: Iterable[Observation]) -> list[Observation]:
    """One observation per entry date (the mean of that date's values), ordered by when its last trade closed."""
    groups: dict[str, list[Observation]] = {}
    for obs in observations:
        groups.setdefault(str(obs.entry_date), []).append(obs)
    out = []
    for entry, members in groups.items():
        trades = sum(o.trades for o in members)
        value = sum(o.value_bps * o.trades for o in members) / trades
        out.append(Observation(value, entry, max(str(o.available) for o in members), trades))
    out.sort(key=lambda o: (o.available, o.entry_date))
    return out


def effective_trades(clusters: Iterable[Observation], rho: float) -> float:
    """Sum of m / (1 + (m - 1) rho): trades as independent information at the measured same-date correlation."""
    return float(sum(o.trades / (1.0 + (o.trades - 1) * max(rho, 0.0)) for o in clusters))


# ----------------------------------------------------------------- verdict

def e_process_verdict(clusters: Sequence[Observation], *, bound_bps: float, sigma_bps: float,
                      improvement_level: float, harm_alpha: float = HARM_ALPHA, cap_trades: int = CAP_TRADES,
                      keep_lcb_bps: float = KEEP_LCB_BPS, levels: Optional[Sequence[float]] = None,
                      rho: float = 0.0, process: str = "betting") -> dict:
    """Walk the observations in order; the first test to cross decides, else the cap, else ``watching``.

    ``levels`` (optional, one per observation) is the e-LOND level in force
    when each observation was first seen; without it ``improvement_level``
    applies throughout.  Harm is checked before improvement at every step.
    Verdicts use the engine's words: ``not_helping`` (revert), ``working``
    (confirm), ``inconclusive`` (kept at the cap), ``watching``.
    ``process="catoni"`` reads the Catoni-style process from the first
    observation (no loss bound; ``sigma_bps`` must bound the conditional
    variance), the variant ``scripts/replay_verdicts.py`` measures beside it.
    """
    values = [float(o.value_bps) for o in clusters]
    n = len(values)
    trades = np.cumsum([int(o.trades) for o in clusters]) if n else np.zeros(0, dtype=int)
    catoni_only = process == "catoni"
    breach = 0 if catoni_only else first_breach(values, bound_bps)
    bounded_n = n if breach is None else breach
    known = known_before_open(clusters)
    harm_b = imp_b = np.zeros(0)
    if bounded_n:
        harm_b = betting_e_process(values[:bounded_n], bound_bps=bound_bps, side="less", known=known[:bounded_n])
        imp_b = betting_e_process(values[:bounded_n], bound_bps=bound_bps, side="greater", known=known[:bounded_n])
    harm_c = imp_c = None
    if breach is not None:
        harm_c = catoni_e_process(values, sigma_bps=sigma_bps, side="less")
        imp_c = catoni_e_process(values, sigma_bps=sigma_bps, side="greater")
    harm_threshold = 1.0 / harm_alpha
    base = {"method": METHOD_E_PROCESS, "observations": n, "trades": int(trades[-1]) if n else 0,
            "effective_trades": round(effective_trades(clusters, rho), 1), "rho": rho,
            "bound_bps": None if catoni_only else bound_bps, "sigma_bps": sigma_bps,
            "bound_breach": breach is not None and not catoni_only,
            "breach_at_observation": None if breach is None or catoni_only else breach + 1,
            "harm_alpha": harm_alpha, "harm_threshold": harm_threshold,
            "mean_difference_bps": round(float(np.mean(values)), 2) if n else None,
            "cap_trades": cap_trades, "keep_lcb_bps": keep_lcb_bps}
    e_harm = e_imp = 1.0
    level = float(improvement_level)
    for k in range(n):
        level = float(levels[k]) if levels is not None and k < len(levels) else float(improvement_level)
        use_catoni = breach is not None and k >= breach
        e_harm = float(harm_c[k] if use_catoni else harm_b[k])
        e_imp = float(imp_c[k] if use_catoni else imp_b[k])
        step = {"decided_at_observation": k + 1, "decided_at_trades": int(trades[k]),
                "e_harm": round(e_harm, 4), "e_improvement": round(e_imp, 4),
                "improvement_level": level, "improvement_threshold": round(1.0 / level, 2),
                "process": "catoni" if use_catoni else "betting"}
        if e_harm >= harm_threshold:
            return base | step | {"verdict": "not_helping", "reason": "harm test crossed",
                                  "lcb_bps": None}
        if e_imp >= 1.0 / level:
            return base | step | {"verdict": "working", "reason": "improvement test crossed the e-LOND level",
                                  "lcb_bps": None}
        if trades[k] >= cap_trades:
            seen = values[:k + 1]
            if use_catoni:
                lcb = catoni_lower_bound_bps(seen, sigma_bps=sigma_bps)
            else:
                lcb = lower_confidence_bound_bps(seen, bound_bps=bound_bps, n_planned=len(seen), known=known[:k + 1])
            keep = lcb is not None and lcb > keep_lcb_bps
            return base | step | {"verdict": "inconclusive" if keep else "not_helping",
                                  "reason": (f"{cap_trades}-trade cap: lower bound {lcb:+.1f} bps "
                                             f"{'>' if keep else '<='} {keep_lcb_bps:+.0f} bps"),
                                  "lcb_bps": None if lcb is None else round(lcb, 2), "kept_at_cap": keep}
    return base | {"verdict": "watching", "reason": "neither test crossed; under the cap",
                   "decided_at_observation": None, "decided_at_trades": None,
                   "e_harm": round(e_harm, 4), "e_improvement": round(e_imp, 4),
                   "improvement_level": level, "improvement_threshold": round(1.0 / level, 2),
                   "process": "catoni" if breach is not None else "betting", "lcb_bps": None}


# ---------------------------------------------------------- multiplicity hurdle

HURDLE_T = 3.0
HURDLE_DSR = 0.95


def hold_returns(trades, frame) -> np.ndarray:
    """``frame``'s return over each trade's hold (entry open to the exit session's open or close), vectorised."""
    out = np.full(len(trades), np.nan)
    if frame is None or trades is None or len(trades) == 0 or "open" not in frame:
        return out
    dates = frame["date"].astype(str).str[:10].to_numpy()
    opens, closes = frame["open"].to_numpy(float), frame["close"].to_numpy(float)
    entry = trades["entry_date"].astype(str).str[:10].to_numpy()
    exit_ = trades["exit_date"].astype(str).str[:10].to_numpy()
    i = np.searchsorted(dates, entry)
    j = np.searchsorted(dates, exit_, side="right") - 1
    ok = (i < len(dates)) & (j >= i) & (j >= 0)
    barrier = trades["barrier"].astype(str).to_numpy() if "barrier" in trades else np.full(len(trades), "")
    at_open = np.isin(barrier, list(OPEN_EXIT_BARRIERS))
    ii, jj = np.clip(i, 0, len(dates) - 1), np.clip(j, 0, len(dates) - 1)
    end = np.where(at_open, opens[jj], closes[jj])
    start = opens[ii]
    with np.errstate(divide="ignore", invalid="ignore"):
        out[ok] = end[ok] / start[ok] - 1.0
    return out


def paired_backtest(base, cand, *, spy=None, adjust: bool = True):
    """Candidate minus baseline per replayed decision (``symbol``, ``decision_date``) over the union of both setups.

    ``r_c 1[c trades] - r_b 1[b trades]``: zero where both take the same
    trade, the trade's return where only one does, the return difference
    where both trade it with different exits.  SPY-adjusted over each hold
    when ``adjust`` (a ``resid_spy`` column is used when present).  Returns
    a frame of ``value_bps``, ``entry_date``, ``exit_date``, ``decision_date``.
    """
    import pandas as pd

    def returns(trades):
        if not adjust:
            return trades["net_return"].to_numpy(float)
        if "resid_spy" in trades:
            return trades["resid_spy"].to_numpy(float)
        return trades["net_return"].to_numpy(float) - hold_returns(trades, spy)

    keys = ["symbol", "decision_date"]
    b = base[keys + ["entry_date", "exit_date"]].assign(rb=returns(base))
    c = cand[keys + ["entry_date", "exit_date"]].assign(rc=returns(cand))
    merged = b.merge(c, on=keys, how="outer", suffixes=("_b", "_c"))
    value = (merged["rc"].fillna(0.0) - merged["rb"].fillna(0.0)) * 10_000
    out = pd.DataFrame({"value_bps": value.to_numpy(float),
                        "entry_date": merged["entry_date_c"].fillna(merged["entry_date_b"]).astype(str).str[:10],
                        "exit_date": merged["exit_date_c"].fillna(merged["exit_date_b"]).astype(str).str[:10],
                        "decision_date": merged["decision_date"].astype(str).str[:10]})
    keep = np.isfinite(out["value_bps"].to_numpy()) & ~(merged["rc"].isna() & merged["rb"].isna()).to_numpy()
    return out.loc[keep].sort_values(["exit_date", "entry_date"]).reset_index(drop=True)


def paired_stats(frame) -> dict:
    """Mean, std, per-decision Sharpe, moments and the entry-date cluster-robust t of a paired-difference frame."""
    values = np.asarray(frame["value_bps"], dtype=float) if frame is not None and len(frame) else np.zeros(0)
    n = int(values.size)
    if n < 2:
        return {"decisions": n, "mean_bps": None, "std_bps": None, "sharpe": None, "t_stat": None}
    mean, std = float(values.mean()), float(values.std(ddof=1))
    centred = values - mean
    pop = float(np.sqrt(np.mean(centred ** 2)))
    skew = float(np.mean(centred ** 3) / pop ** 3) if pop > 0 else 0.0
    kurt = float(np.mean(centred ** 4) / pop ** 4) if pop > 0 else 3.0
    # CR0 cluster-robust variance of the mean, clusters = entry dates (the same-date dependence).
    sums = {}
    for value, entry in zip(centred, frame["entry_date"].astype(str)):
        sums[entry] = sums.get(entry, 0.0) + value
    var_mean = float(sum(v * v for v in sums.values())) / (n * n)
    t_stat = mean / math.sqrt(var_mean) if var_mean > 0 else None
    return {"decisions": n, "clusters": len(sums), "mean_bps": round(mean, 3), "std_bps": round(std, 3),
            "nonzero_share": round(float(np.mean(np.abs(values) > 1e-9)), 4),
            "sharpe": round(mean / std, 6) if std > 0 else None, "skewness": round(skew, 4),
            "kurtosis": round(kurt, 4), "t_stat": None if t_stat is None else round(t_stat, 3)}


def multiplicity_hurdle(stats: Optional[Mapping], *, trials: int, trial_sharpes: Optional[Sequence[float]] = None,
                        label: Optional[str] = None) -> dict:
    """Does the adopted candidate's backtest gain clear a hurdle that counts every candidate ever tried for the strategy?

    On the **paired difference** (candidate minus the live setup per replayed
    decision, ``paired_backtest``; SPY-adjusted except the market-timing
    gates): passes when its entry-date cluster-robust ``t >= 3`` (the Harvey,
    Liu & Zhu 2016 hurdle for a search over many candidates) or the deflated
    Sharpe ratio of the per-decision difference is above 0.95 (Bailey &
    Lopez de Prado 2014, ``backtest_statistics.deflated_sharpe_ratio``) with
    ``trials`` = all candidates ever tried for the strategy and the
    dispersion of their paired Sharpes.  An additional check beside the
    engine's ``RULE``; it is reported whether or not it acts.  No paired
    data = ``passed: None`` (unavailable).
    """
    from app.services.backtest_statistics import deflated_sharpe_ratio

    stats = dict(stats or {})
    base = {"label": label, "basis": "paired_difference", "trials": int(trials), "t_required": HURDLE_T,
            "dsr_required": HURDLE_DSR,
            "rule": (f"t >= {HURDLE_T:g} (entry-date clustered) on the backtest paired gain, or deflated Sharpe > "
                     f"{HURDLE_DSR:g} counting all {int(trials)} candidates ever tried for this strategy")}
    if not stats.get("decisions") or stats.get("decisions", 0) < 2 or stats.get("sharpe") is None:
        return base | {"t_stat": stats.get("t_stat"), "deflated_sharpe": None, "passed": None,
                       "mean_bps": stats.get("mean_bps"), "decisions": stats.get("decisions", 0),
                       "reason": "no paired backtest difference to test"}
    sharpes = [float(s) for s in (trial_sharpes or []) if s is not None and math.isfinite(float(s))]
    dsr = deflated_sharpe_ratio(float(stats["sharpe"]), int(stats["decisions"]), trials=max(int(trials), 1),
                                trial_sharpes=sharpes, skewness=float(stats.get("skewness") or 0.0),
                                kurtosis=float(stats.get("kurtosis") or 3.0))["deflated_sharpe_ratio"]
    t_stat = stats.get("t_stat")
    passed = bool((t_stat is not None and t_stat >= HURDLE_T) or (dsr is not None and dsr > HURDLE_DSR))
    return base | {"t_stat": t_stat, "deflated_sharpe": dsr, "passed": passed, "mean_bps": stats.get("mean_bps"),
                   "decisions": stats.get("decisions")}


# ------------------------------------------------------------ measured inputs

def inputs_row(db) -> Optional[dict]:
    from app.models import AuditLog

    row = db.query(AuditLog).filter(AuditLog.event_type == EVENT, AuditLog.action == ACTION_INPUTS).order_by(
        AuditLog.id.desc()).first()
    return dict(row.payload or {}) if row else None


def current_inputs(db=None) -> dict:
    """The measured inputs the judge reads: the latest ``verdict_inputs`` row, else ``MEASURED_INPUTS``."""
    out = {**MEASURED_INPUTS, "recorded": False}
    if db is None:
        return out
    try:
        row = inputs_row(db)
    except Exception as exc:
        db.rollback()
        logger.warning("verdict stats: inputs row unreadable, using the constants: %s", exc)
        return out
    if not row:
        return out
    merged = dict(out)
    for key in ("as_of", "source", "rho", "per_trade_std_bps", "paired_std_bps"):
        if row.get(key) is not None:
            merged[key] = row[key]
    for key in ("loss_bound_bps", "sigma_bps"):
        block = row.get(key)
        if isinstance(block, Mapping) and all(_f(block.get(c)) for c in (TECHNICAL_CLASS, REVERSION_CLASS)):
            merged[key] = {c: float(block[c]) for c in (TECHNICAL_CLASS, REVERSION_CLASS)}
    merged["recorded"] = True
    return merged


def record_inputs(db, measured: Mapping, *, now: Optional[datetime] = None) -> dict:
    """Store one ``verdict_inputs`` audit row (the measurement script's ``--record``)."""
    from app.services.audit import write_audit_log

    payload = {k: measured.get(k) for k in ("as_of", "source", "loss_bound_bps", "sigma_bps", "rho",
                                            "per_trade_std_bps", "paired_std_bps", "notes")}
    payload["recorded_at"] = (now or datetime.now(UTC)).isoformat()
    payload["paper_only"] = True
    write_audit_log(db, event_type=EVENT, entity_type="experiment_engine", action=ACTION_INPUTS, status="recorded",
                    message=(f"verdict inputs as of {payload.get('as_of')}: rho {payload.get('rho')}, loss bounds "
                             f"{payload.get('loss_bound_bps')}"), payload=payload)
    db.commit()
    return payload


# ------------------------------------------------------------- e-LOND state

def elond_state(db) -> dict:
    """The latest ``e_lond`` row: per change its stream index, the levels it was judged at, and whether it was confirmed."""
    from app.models import AuditLog

    empty = {"alpha": IMPROVEMENT_FDR_ALPHA, "gamma": "1/(j(j+1))", "changes": {}, "discoveries": 0}
    try:
        row = db.query(AuditLog).filter(AuditLog.event_type == EVENT, AuditLog.action == ACTION_ELOND).order_by(
            AuditLog.id.desc()).first()
    except Exception as exc:
        db.rollback()
        logger.warning("verdict stats: e-LOND state unreadable, starting empty: %s", exc)
        return empty
    if row is None:
        return empty
    payload = dict(row.payload or {})
    payload.setdefault("changes", {})
    payload.setdefault("alpha", IMPROVEMENT_FDR_ALPHA)
    return payload


def assign_indices(state: dict, changes: Iterable[Mapping]) -> dict:
    """Give every adopted change a stream index in adoption order (an index, once given, never moves)."""
    book = state.setdefault("changes", {})
    nxt = max([int(v.get("index") or 0) for v in book.values()] or [0]) + 1
    for change in sorted(changes, key=lambda c: (str(c.get("adopted_at")), str(c.get("change_id")))):
        cid = change.get("change_id")
        if cid and cid not in book:
            book[cid] = {"index": nxt, "levels": [], "discovered": False}
            nxt += 1
    return state


def level_for(state: Mapping, change_id: str) -> tuple[int, int, float]:
    """(index j, discoveries among earlier changes, e-LOND level) for a change."""
    book = state.get("changes") or {}
    entry = book.get(change_id) or {}
    j = int(entry.get("index") or (len(book) + 1))
    before = sum(1 for v in book.values() if v.get("discovered") and int(v.get("index") or 0) < j)
    return j, before, elond_level(j, before, float(state.get("alpha") or IMPROVEMENT_FDR_ALPHA))


def levels_by_observation(entry: Mapping, clusters: Sequence[Observation], current: float) -> list[float]:
    """The level in force when each observation was first seen (recorded per review), else the current one."""
    history = sorted((int(t), float(level)) for t, level in (entry.get("levels") or []))
    out, cumulative = [], 0
    for obs in clusters:
        cumulative += int(obs.trades)
        level = next((lv for seen, lv in history if seen >= cumulative), current)
        out.append(level)
    return out


def write_elond_state(db, state: Mapping, *, now: datetime) -> None:
    from app.services.audit import write_audit_log

    discoveries = sum(1 for v in (state.get("changes") or {}).values() if v.get("discovered"))
    payload = {**state, "discoveries": discoveries, "as_of": now.isoformat(), "paper_only": True}
    write_audit_log(db, event_type=EVENT, entity_type="experiment_engine", action=ACTION_ELOND, status="recorded",
                    message=f"e-LOND: {len(payload.get('changes') or {})} changes in the stream, {discoveries} confirmed",
                    payload=payload)


# ------------------------------------------------------- data from the ledger

def spy_hold_return(spy, entry_date: Any, exit_date: Any, barrier: Optional[str] = None) -> Optional[float]:
    """SPY over the same hold: open of the entry session to the exit session's open (exits at the open) or close."""
    if spy is None or entry_date is None or exit_date is None:
        return None
    try:
        dates = spy["date"].astype(str).str[:10].to_numpy()
        entry, exit_ = str(entry_date)[:10], str(exit_date)[:10]
        i = int(np.searchsorted(dates, entry))
        j = int(np.searchsorted(dates, exit_, side="right")) - 1
        if i >= len(dates) or j < i:
            return None
        start = float(spy["open"].iloc[i]) if "open" in spy else float(spy["close"].iloc[max(i - 1, 0)])
        end = float(spy["open"].iloc[j]) if (barrier in OPEN_EXIT_BARRIERS and "open" in spy) else float(spy["close"].iloc[j])
        return end / start - 1.0 if start > 0 else None
    except Exception:
        return None


def adjusts_for_spy(template: Optional[str]) -> bool:
    """Residual (SPY-adjusted) differences, except for the market-timing gates whose mechanism is the market move."""
    return template not in MARKET_TIMING_TEMPLATES


def paired_observations(decisions: Iterable[Mapping], template: str, before_label: str, *, spy=None,
                        adjust: bool = True) -> list[Observation]:
    """Per scored shadow decision: (adopted allowed - previous allowed) x return, SPY-adjusted when ``adjust``.

    A decision both values take or both skip is a zero; one neither value
    would have taken is not an observation.
    """
    out = []
    for decision in decisions:
        view = (decision.get("templates") or {}).get(template)
        if not view or decision.get("status") != "scored":
            continue
        new, old = view.get("live_allowed"), (view.get("candidates") or {}).get(before_label)
        if new is None or old is None or view.get("live") == before_label or not (new or old):
            continue
        net = _f(decision.get("net_return"))
        if net is None:
            continue
        value = net
        if adjust:
            spy_r = spy_hold_return(spy, decision.get("entry_date"), decision.get("exit_date"), decision.get("barrier"))
            if spy_r is None:
                continue
            value = net - spy_r
        diff = (int(bool(new)) - int(bool(old))) * value * 10_000
        out.append(Observation(diff, str(decision.get("entry_date") or decision.get("session")),
                               str(decision.get("exit_date") or decision.get("session"))))
    return out


def _day(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        from zoneinfo import ZoneInfo

        stamp = value if value.tzinfo else value.replace(tzinfo=UTC)
        return stamp.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def residual_trades(rows: Iterable[Any], *, spy=None, adjust: bool = True) -> list[tuple[str, str, float]]:
    """(entry day, exit day, return bps) per attribution row, SPY-adjusted when ``adjust``."""
    out = []
    for row in rows:
        net = _f(row.net_return if row.net_return is not None else row.gross_return)
        if net is None:
            continue
        entry, exit_ = _day(row.opened_at), _day(row.closed_at)
        if adjust:
            spy_r = spy_hold_return(spy, entry, exit_)
            if spy_r is None:
                continue
            net -= spy_r
        out.append((entry, exit_, net * 10_000))
    return out


def unpaired_observations(after: Iterable[Any], before: Iterable[Any], *, spy=None, adjust: bool = True,
                          baseline_trades: int = 20) -> tuple[list[Observation], Optional[float]]:
    """Live trades after the change minus the mean of the last ``baseline_trades`` before it (no shadow to pair with)."""
    prior = sorted(residual_trades(before, spy=spy, adjust=adjust), key=lambda r: r[1])[-baseline_trades:]
    if not prior:
        return [], None
    baseline = float(np.mean([r[2] for r in prior]))
    obs = [Observation(value - baseline, entry, exit_) for entry, exit_, value in
           residual_trades(after, spy=spy, adjust=adjust)]
    return obs, baseline
