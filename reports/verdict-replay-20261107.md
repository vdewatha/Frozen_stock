# Verdict replay: 2026-11-07

Experiments 2 and 3 of `reports/Frozen stock research priorities.md` (ranked table, rows 2 and 3), at the inputs
measured in `reports/verdict-inputs-20261107.md`, and the e-process judge put into shadow mode. Paper only;
nothing here changed a strategy.

## Summary

* **The 20-trade rule is a coin flip, now measured.** At the measured dispersion it reverts **48% of changes that
  do nothing, 34-43% of changes that are truly 10-50 bps better, and keeps 40-46% of changes 10-50 bps worse**
  (stream B). The research's 41% at an assumed 2% std was, if anything, optimistic.
* **The always-valid judge, as specified, almost never keeps anything.** Harm at alpha 0.10 and e-LOND
  confirmation are valid and never keep a bad change (false keep 0.0-0.3%), but at the measured inputs neither
  test crosses within 120 trades for effects under ~50 bps, so the **120-trade cap decides**, and its rule (keep
  only if the lower confidence bound is above -10 bps) needs a true effect of roughly **+50 bps** to pass: the
  median lower bound at the cap sits ~45-50 bps below the true effect even with one trade per date, and hundreds
  of bps below it on the point-in-time replay's 30-names-a-day clusters. It reverts 98% of null changes and 53-69%
  of good ones (one trade per date), and every one of the 322 replayed adoptions (stream C).
* **Why.** Two measured inputs bind. (1) The loss bound B (1100-1600 bps) is 5-10x the paired std; a bounded
  betting process cannot stake more than about c / B per bps without risking ruin on a -B trade, so its growth per
  trade is capped near c delta / B whatever the variance. (2) Same-date clustering turns 120 trades into few
  observations when a strategy enters many names a day. A variance-scaled alternative (the Catoni process from the
  first trade, `e_catoni` below) is no better: its sigma bound must cover the worst candidate's paired std.
* **The 110-trade O'Brien-Fleming design** is the most efficient of the three (false revert of good changes
  ~1%, null changes 10-16%) but keeps 30-50% of bad ones, and its error rates hold only at its five planned looks.
* **Decision: shadow mode, `experiment_verdict_method = "fixed_20"` stays the default.** The e-process verdict is
  recorded beside the acting one in every `change_verdict` payload and pending entry (`verdict_e_process`), the
  engine audit (`judges`) and `GET /stock-paper/learning-experiments` (`verdict_methods`). Switching it to act as
  specified would make the engine revert nearly every change after 120 trades; that is a decision for the owner
  (see "What would make it act" below), not one this replay supports on its own.
* **Multiplicity hurdle (experiment 3): 13 of the 15 adoptions the engine's rule makes on today's full-history
  replay fail it** (paired-gain t >= 3 or deflated Sharpe > 0.95, 18 candidates per strategy). Two pass: the 5%
  stop for MACD (t 7.2) and for Trend Pullback (t 3.2). Of the 13 blocked, 4 are confirmed improvements by the
  full-history paired e-process (all of +1 to +15 bps); three are no-ops (IBS: identical trades). By the
  pre-registered rule (on unless more than half of the blocked adoptions are confirmed), **`experiment_multiplicity_
  hurdle = True`**, the default now in `config.py`.

## How the judge is built (verdict_stats.py), checked against the papers

* **Betting e-process** (Waudby-Smith & Ramdas 2023, JRSSB; arXiv 2010.09686): capital process
  `prod (1 +/- lambda_i (x_i - m))`, eq. 23 p. 13, one side of the hedged process of Theorem 3 p. 16 (theta = 1 or
  0), bets truncated at `c / m` (eq. 25) with **c = 3/4** (the paper's other default; at the measured B it bought
  more power than 1/2 with the null rate still under alpha), predictable plug-in bets eq. 26 p. 17 with
  `log(1/alpha)` for a one-sided test. Each observation's bet uses only observations that had **closed before it
  was opened** (the research's partial-path rule). Data are the paired differences mapped from `[-B, B]` to `[0, 1]`.
* **Alarm and heavy tails** (Wang & Ramdas 2023; arXiv 2202.01250): a value beyond B switches both tests to the
  Catoni supermartingales, Lemma 8 eqs. 30-31 p. 8, influence function eq. 29, bets eq. 33 (index floored as in
  their footnote 4), lower bound by root-finding on eq. 32 (Theorem 9).
* **Validity at any stopping time** (Howard et al. 2021, Ann. Statist.; arXiv 1810.08240): Ville's inequality and
  Lemma 3 p. 19 (crossing at any stopping time = crossing at all times), which is what lets the bot look daily.
* **e-LOND** (Xu & Ramdas, AISTATS 2024; arXiv 2311.06412): the j-th adopted change is confirmed when its e-value
  reaches `1 / alpha_j`, `alpha_j = alpha gamma_j (|R_{j-1}| + 1)` (Section 3.2 p. 7; FDR <= alpha under arbitrary
  dependence, Theorem 1), with `gamma_j = 1/(j(j+1))`, the discount sequence of their own simulations (App. B
  p. 24). Thresholds at alpha 0.10 with no earlier confirmation: 20, 60, 120, 300 (5th), 1100 (10th), 4200 (20th);
  the research's "first-test weight near 0.07" would start at 140. A Javanmard-Montanari sequence only beats this
  one after about the 30th adoption. The state (stream index per change, the level at each review, confirmations,
  the frozen first decision) is an `e_lond` audit row.
* **Cap**: at 120 trades, the one-sided 90% lower bound from the same capital process with fixed-horizon bets
  (Remark 3, eq. 27), the right tuning for a look fixed in advance.
* **Observations**: per BUY decision from the setup shadow, the adopted value's outcome minus the replaced value's,
  SPY-adjusted over the same hold **except for the market, VIX and breadth gates** (their mechanism is the market
  move a residual would subtract; a residual judge could never see their value). Exit, scope and book changes have
  no shadow: live trades after the change minus the residual mean of the 20 before. Same-date trades are averaged
  into one observation, ordered by the last close.
* **Validity checks** (`tests/test_verdict_stats.py`, fixed seeds): null crossing rate of each test under alpha by
  simulation (Student-t(3) tails, composite nulls, Catoni with unbounded data), crossing within 50-60 trades for
  +/-150 bps, e-LOND bookkeeping and FDR over streams of null changes, the cap both ways, the alarm switch.

## What would make it act

At the measured inputs no valid method reads +/-25 bps in 120 trades; the choices are about which error to pay.
The options the numbers below support, for the owner:

1. **Keep `fixed_20` acting, e-process in shadow** (this PR). Its verdicts are a coin flip, but a revert of a null
   change costs nothing and the shadow record accumulates.
2. **Switch to `e_process` with a looser cap** (for example keep unless the lower bound is below -50 bps, or a cap
   of 240 trades): harm stays bounded at 10% and bad changes are still cut, null changes stay instead of rotating.
   Not built: the cap rule is the research's, and changing it is a new pre-registered decision.
3. **Raise the evidence rate** (ranked experiments 4 and 7): with one trade per date and more trades, the same
   judge starts confirming and keeping real +25-50 bps changes.
## Detail


Inputs: loss bound {'technical': 1600.0, 'reversion': 1100.0} bps, variance bound {'technical': 450.0, 'reversion': 300.0} bps, rho 0.0696. O'Brien-Fleming constants (Monte Carlo, 5 looks): harm 1.430 (one-sided 0.10), improvement 1.752 (one-sided 0.05).

## A. The ledger

Not reachable from where this ran (no --database-url). The engine's ledger on the host had no adoption when the engine audit last looked (reports/engine-audit-20261106.md), so there is nothing to re-judge yet; streams B and C stand in. Rerun with `--database-url` on the host.

## B. Known effects at the measured inputs

2000 changes per effect and class; trades bootstrapped by entry-date cluster from the replay's own paired differences (demeaned and shifted by the effect) for the e-process and O'Brien-Fleming rules, and from the raw per-trade returns for the 20-trade rule (which compares the trades after with the 20 before, unpaired). The effect is the same number of bps on both scales, which treats a per-decision paired gain as a per-trade expectancy gain. e-LOND level of the 5th adoption with no earlier confirmation: 0.0033 (threshold 300).

| Class | Effect bps | Rule | Revert | Keep | of which confirmed | Undecided | Median trades to decision | Decided by a test (not the cap) | Median LCB at cap | Bound breach |
|---|---|---|---|---|---|---|---|---|---|---|
| technical | -50 | fixed_20 | 55.5% | 44.5% | 44.5% | 0.0% | 20 |  |  |  |
| technical | -50 | e_process | 100.0% | 0.0% | 0.0% | 0.0% | 133 | 0.0% | -555 | 0.0% |
| technical | -50 | obf_110 | 75.2% | 24.8% | 0.3% | 0.0% | 74 |  |  |  |
| technical | -50 | e_catoni | 100.0% | 0.0% | 0.0% | 0.0% | 133 | 0.0% | -422 |  |
| technical | -25 | fixed_20 | 54.4% | 45.6% | 45.6% | 0.0% | 20 |  |  |  |
| technical | -25 | e_process | 100.0% | 0.0% | 0.0% | 0.0% | 134 | 0.0% | -554 | 0.1% |
| technical | -25 | obf_110 | 60.7% | 39.3% | 0.6% | 0.0% | 104 |  |  |  |
| technical | -25 | e_catoni | 100.0% | 0.0% | 0.0% | 0.0% | 134 | 0.0% | -404 |  |
| technical | -10 | fixed_20 | 52.0% | 47.9% | 47.9% | 0.0% | 20 |  |  |  |
| technical | -10 | e_process | 100.0% | 0.0% | 0.0% | 0.0% | 133 | 0.0% | -535 | 0.0% |
| technical | -10 | obf_110 | 45.1% | 54.9% | 1.7% | 0.0% | 110 |  |  |  |
| technical | -10 | e_catoni | 100.0% | 0.0% | 0.0% | 0.0% | 133 | 0.0% | -384 |  |
| technical | +0 | fixed_20 | 48.7% | 51.3% | 51.3% | 0.0% | 20 |  |  |  |
| technical | +0 | e_process | 100.0% | 0.0% | 0.0% | 0.0% | 133 | 0.0% | -528 | 0.0% |
| technical | +0 | obf_110 | 16.2% | 83.8% | 3.9% | 0.0% | 110 |  |  |  |
| technical | +0 | e_catoni | 100.0% | 0.0% | 0.0% | 0.0% | 133 | 0.0% | -371 |  |
| technical | +10 | fixed_20 | 44.2% | 55.8% | 55.8% | 0.0% | 20 |  |  |  |
| technical | +10 | e_process | 100.0% | 0.0% | 0.0% | 0.0% | 133 | 0.0% | -518 | 0.1% |
| technical | +10 | obf_110 | 3.3% | 96.7% | 31.4% | 0.0% | 110 |  |  |  |
| technical | +10 | e_catoni | 100.0% | 0.0% | 0.0% | 0.0% | 133 | 0.0% | -363 |  |
| technical | +25 | fixed_20 | 44.0% | 56.0% | 56.0% | 0.0% | 20 |  |  |  |
| technical | +25 | e_process | 100.0% | 0.0% | 0.0% | 0.0% | 134 | 0.0% | -512 | 0.0% |
| technical | +25 | obf_110 | 0.9% | 99.1% | 52.1% | 0.0% | 110 |  |  |  |
| technical | +25 | e_catoni | 100.0% | 0.0% | 0.0% | 0.0% | 134 | 0.0% | -349 |  |
| technical | +50 | fixed_20 | 41.1% | 58.9% | 58.9% | 0.0% | 20 |  |  |  |
| technical | +50 | e_process | 100.0% | 0.0% | 0.0% | 0.0% | 133 | 0.0% | -489 | 0.1% |
| technical | +50 | obf_110 | 0.1% | 99.9% | 75.1% | 0.0% | 82 |  |  |  |
| technical | +50 | e_catoni | 100.0% | 0.0% | 0.0% | 0.0% | 133 | 0.0% | -322 |  |
| technical, one trade per date | -50 | fixed_20 | 62.2% | 37.9% | 37.9% | 0.0% | 20 |  |  |  |
| technical, one trade per date | -50 | e_process | 100.0% | 0.0% | 0.0% | 0.0% | 120 | 47.7% | -86 | 4.3% |
| technical, one trade per date | -50 | obf_110 | 84.4% | 15.7% | 0.1% | 0.0% | 66 |  |  |  |
| technical, one trade per date | -50 | e_catoni | 100.0% | 0.0% | 0.0% | 0.0% | 120 | 1.8% | -148 |  |
| technical, one trade per date | -25 | fixed_20 | 56.6% | 43.4% | 43.4% | 0.0% | 20 |  |  |  |
| technical, one trade per date | -25 | e_process | 100.0% | 0.1% | 0.0% | 0.0% | 120 | 11.9% | -72 | 3.8% |
| technical, one trade per date | -25 | obf_110 | 46.2% | 53.8% | 0.3% | 0.0% | 110 |  |  |  |
| technical, one trade per date | -25 | e_catoni | 100.0% | 0.0% | 0.0% | 0.0% | 120 | 0.2% | -124 |  |
| technical, one trade per date | -10 | fixed_20 | 52.6% | 47.4% | 47.4% | 0.0% | 20 |  |  |  |
| technical, one trade per date | -10 | e_process | 99.2% | 0.9% | 0.0% | 0.0% | 120 | 2.9% | -59 | 3.4% |
| technical, one trade per date | -10 | obf_110 | 20.6% | 79.4% | 2.2% | 0.0% | 110 |  |  |  |
| technical, one trade per date | -10 | e_catoni | 100.0% | 0.0% | 0.0% | 0.0% | 120 | 0.1% | -109 |  |
| technical, one trade per date | +0 | fixed_20 | 50.1% | 49.9% | 49.9% | 0.0% | 20 |  |  |  |
| technical, one trade per date | +0 | e_process | 97.8% | 2.2% | 0.0% | 0.0% | 120 | 0.4% | -51 | 4.5% |
| technical, one trade per date | +0 | obf_110 | 10.0% | 90.0% | 5.4% | 0.0% | 110 |  |  |  |
| technical, one trade per date | +0 | e_catoni | 100.0% | 0.0% | 0.0% | 0.0% | 120 | 0.1% | -99 |  |
| technical, one trade per date | +10 | fixed_20 | 45.6% | 54.4% | 54.4% | 0.0% | 20 |  |  |  |
| technical, one trade per date | +10 | e_process | 92.8% | 7.2% | 0.0% | 0.0% | 120 | 0.1% | -41 | 3.6% |
| technical, one trade per date | +10 | obf_110 | 3.6% | 96.4% | 13.9% | 0.0% | 110 |  |  |  |
| technical, one trade per date | +10 | e_catoni | 100.0% | 0.0% | 0.0% | 0.0% | 120 | 0.1% | -88 |  |
| technical, one trade per date | +25 | fixed_20 | 40.9% | 59.1% | 59.1% | 0.0% | 20 |  |  |  |
| technical, one trade per date | +25 | e_process | 79.0% | 21.0% | 0.0% | 0.0% | 120 | 0.0% | -26 | 5.1% |
| technical, one trade per date | +25 | obf_110 | 0.9% | 99.2% | 31.1% | 0.0% | 110 |  |  |  |
| technical, one trade per date | +25 | e_catoni | 99.8% | 0.2% | 0.0% | 0.0% | 120 | 0.0% | -74 |  |
| technical, one trade per date | +50 | fixed_20 | 33.3% | 66.7% | 66.7% | 0.0% | 20 |  |  |  |
| technical, one trade per date | +50 | e_process | 34.4% | 65.5% | 0.1% | 0.0% | 120 | 0.1% | -1 | 3.8% |
| technical, one trade per date | +50 | obf_110 | 0.0% | 100.0% | 74.2% | 0.0% | 88 |  |  |  |
| technical, one trade per date | +50 | e_catoni | 97.0% | 3.0% | 0.0% | 0.0% | 120 | 0.0% | -48 |  |
| reversion | -50 | fixed_20 | 64.6% | 35.4% | 35.4% | 0.0% | 20 |  |  |  |
| reversion | -50 | e_process | 100.0% | 0.0% | 0.0% | 0.0% | 120 | 22.9% | -103 | 9.2% |
| reversion | -50 | obf_110 | 92.8% | 7.2% | 0.0% | 0.0% | 44 |  |  |  |
| reversion | -50 | e_catoni | 100.0% | 0.0% | 0.0% | 0.0% | 120 | 1.9% | -132 |  |
| reversion | -25 | fixed_20 | 56.5% | 43.5% | 43.5% | 0.0% | 20 |  |  |  |
| reversion | -25 | e_process | 100.0% | 0.0% | 0.0% | 0.0% | 120 | 1.0% | -83 | 9.3% |
| reversion | -25 | obf_110 | 71.0% | 29.0% | 0.0% | 0.0% | 68 |  |  |  |
| reversion | -25 | e_catoni | 100.0% | 0.0% | 0.0% | 0.0% | 120 | 0.3% | -107 |  |
| reversion | -10 | fixed_20 | 52.3% | 47.6% | 47.6% | 0.0% | 20 |  |  |  |
| reversion | -10 | e_process | 100.0% | 0.0% | 0.0% | 0.0% | 120 | 0.1% | -70 | 11.2% |
| reversion | -10 | obf_110 | 36.5% | 63.5% | 0.4% | 0.0% | 110 |  |  |  |
| reversion | -10 | e_catoni | 100.0% | 0.0% | 0.0% | 0.0% | 120 | 0.1% | -92 |  |
| reversion | +0 | fixed_20 | 47.8% | 52.2% | 52.2% | 0.0% | 20 |  |  |  |
| reversion | +0 | e_process | 99.8% | 0.2% | 0.0% | 0.0% | 121 | 0.0% | -59 | 11.1% |
| reversion | +0 | obf_110 | 10.2% | 89.8% | 5.2% | 0.0% | 110 |  |  |  |
| reversion | +0 | e_catoni | 100.0% | 0.1% | 0.0% | 0.0% | 121 | 0.0% | -81 |  |
| reversion | +10 | fixed_20 | 44.5% | 55.5% | 55.5% | 0.0% | 20 |  |  |  |
| reversion | +10 | e_process | 99.6% | 0.4% | 0.0% | 0.0% | 120 | 0.0% | -51 | 12.0% |
| reversion | +10 | obf_110 | 1.8% | 98.2% | 25.8% | 0.0% | 110 |  |  |  |
| reversion | +10 | e_catoni | 99.9% | 0.1% | 0.0% | 0.0% | 120 | 0.1% | -72 |  |
| reversion | +25 | fixed_20 | 39.2% | 60.8% | 60.8% | 0.0% | 20 |  |  |  |
| reversion | +25 | e_process | 95.3% | 4.7% | 0.0% | 0.0% | 120 | 0.0% | -36 | 11.5% |
| reversion | +25 | obf_110 | 0.1% | 99.9% | 62.5% | 0.0% | 90 |  |  |  |
| reversion | +25 | e_catoni | 99.4% | 0.7% | 0.0% | 0.0% | 120 | 0.0% | -56 |  |
| reversion | +50 | fixed_20 | 29.6% | 70.4% | 70.4% | 0.0% | 20 |  |  |  |
| reversion | +50 | e_process | 55.4% | 44.6% | 0.0% | 0.0% | 120 | 0.0% | -12 | 11.3% |
| reversion | +50 | obf_110 | 0.0% | 100.0% | 92.0% | 0.0% | 46 |  |  |  |
| reversion | +50 | e_catoni | 90.9% | 9.1% | 0.1% | 0.0% | 120 | 0.1% | -32 |  |
| reversion, one trade per date | -50 | fixed_20 | 70.1% | 29.9% | 29.9% | 0.0% | 20 |  |  |  |
| reversion, one trade per date | -50 | e_process | 100.0% | 0.0% | 0.0% | 0.0% | 78 | 77.9% | -80 | 21.6% |
| reversion, one trade per date | -50 | obf_110 | 97.4% | 2.6% | 0.0% | 0.0% | 44 |  |  |  |
| reversion, one trade per date | -50 | e_catoni | 100.0% | 0.0% | 0.0% | 0.0% | 120 | 13.9% | -112 |  |
| reversion, one trade per date | -25 | fixed_20 | 60.2% | 39.9% | 39.9% | 0.0% | 20 |  |  |  |
| reversion, one trade per date | -25 | e_process | 100.0% | 0.1% | 0.0% | 0.0% | 120 | 24.2% | -56 | 20.5% |
| reversion, one trade per date | -25 | obf_110 | 76.4% | 23.5% | 0.0% | 0.0% | 66 |  |  |  |
| reversion, one trade per date | -25 | e_catoni | 100.0% | 0.0% | 0.0% | 0.0% | 120 | 1.2% | -89 |  |
| reversion, one trade per date | -10 | fixed_20 | 51.3% | 48.6% | 48.6% | 0.0% | 20 |  |  |  |
| reversion, one trade per date | -10 | e_process | 99.8% | 0.2% | 0.0% | 0.0% | 120 | 3.0% | -45 | 19.3% |
| reversion, one trade per date | -10 | obf_110 | 38.5% | 61.5% | 0.3% | 0.0% | 110 |  |  |  |
| reversion, one trade per date | -10 | e_catoni | 100.0% | 0.0% | 0.0% | 0.0% | 120 | 0.1% | -74 |  |
| reversion, one trade per date | +0 | fixed_20 | 48.1% | 51.9% | 51.9% | 0.0% | 20 |  |  |  |
| reversion, one trade per date | +0 | e_process | 97.8% | 2.2% | 0.0% | 0.0% | 120 | 0.4% | -35 | 20.1% |
| reversion, one trade per date | +0 | obf_110 | 11.5% | 88.5% | 3.4% | 0.0% | 110 |  |  |  |
| reversion, one trade per date | +0 | e_catoni | 100.0% | 0.0% | 0.0% | 0.0% | 120 | 0.1% | -64 |  |
| reversion, one trade per date | +10 | fixed_20 | 44.0% | 56.0% | 56.0% | 0.0% | 20 |  |  |  |
| reversion, one trade per date | +10 | e_process | 89.5% | 10.4% | 0.0% | 0.0% | 120 | 0.1% | -27 | 23.5% |
| reversion, one trade per date | +10 | obf_110 | 2.1% | 98.0% | 18.6% | 0.0% | 110 |  |  |  |
| reversion, one trade per date | +10 | e_catoni | 99.9% | 0.1% | 0.0% | 0.0% | 120 | 0.1% | -54 |  |
| reversion, one trade per date | +25 | fixed_20 | 32.6% | 67.5% | 67.5% | 0.0% | 20 |  |  |  |
| reversion, one trade per date | +25 | e_process | 56.0% | 44.0% | 0.0% | 0.0% | 120 | 0.0% | -12 | 24.1% |
| reversion, one trade per date | +25 | obf_110 | 0.1% | 99.9% | 65.0% | 0.0% | 88 |  |  |  |
| reversion, one trade per date | +25 | e_catoni | 98.2% | 1.8% | 0.0% | 0.0% | 120 | 0.0% | -39 |  |
| reversion, one trade per date | +50 | fixed_20 | 24.3% | 75.6% | 75.6% | 0.0% | 20 |  |  |  |
| reversion, one trade per date | +50 | e_process | 14.4% | 85.6% | 0.9% | 0.0% | 120 | 0.9% | +13 | 22.8% |
| reversion, one trade per date | +50 | obf_110 | 0.0% | 100.0% | 96.9% | 0.0% | 44 |  |  |  |
| reversion, one trade per date | +50 | e_catoni | 62.4% | 37.6% | 0.4% | 0.0% | 120 | 0.4% | -14 |  |

Implied error rates (false revert of a null change, mean false revert over the positive effects, mean false keep over the effects of -10 bps or worse):

| Class / rule | False revert at 0 | False revert, good | False keep, bad | Revert at -50 | Keep at +50 |
|---|---|---|---|---|---|
| reversion/fixed_20 | 47.8% | 37.8% | 42.2% | 64.6% | 70.4% |
| reversion/e_process | 99.8% | 83.4% | 0.0% | 100.0% | 44.6% |
| reversion/obf_110 | 10.2% | 0.6% | 33.2% | 92.8% | 100.0% |
| reversion/e_catoni | 100.0% | 96.7% | 0.0% | 100.0% | 9.1% |
| reversion, one trade per date/fixed_20 | 48.1% | 33.6% | 39.5% | 70.1% | 75.6% |
| reversion, one trade per date/e_process | 97.8% | 53.3% | 0.1% | 100.0% | 85.6% |
| reversion, one trade per date/obf_110 | 11.5% | 0.7% | 29.2% | 97.4% | 100.0% |
| reversion, one trade per date/e_catoni | 100.0% | 86.8% | 0.0% | 100.0% | 37.6% |
| technical/fixed_20 | 48.7% | 43.1% | 46.0% | 55.5% | 58.9% |
| technical/e_process | 100.0% | 100.0% | 0.0% | 100.0% | 0.0% |
| technical/obf_110 | 16.2% | 1.5% | 39.6% | 75.2% | 99.9% |
| technical/e_catoni | 100.0% | 100.0% | 0.0% | 100.0% | 0.0% |
| technical, one trade per date/fixed_20 | 50.1% | 40.0% | 42.9% | 62.2% | 66.7% |
| technical, one trade per date/e_process | 97.8% | 68.7% | 0.3% | 100.0% | 65.5% |
| technical, one trade per date/obf_110 | 10.0% | 1.5% | 49.6% | 84.4% | 100.0% |
| technical, one trade per date/e_catoni | 100.0% | 98.9% | 0.0% | 100.0% | 3.0% |

## C. Replayed adoptions

322 adoptions (every second year from 2008, each (strategy, template) decided on the history before the date), judged forward by all three rules; the reference is the realised paired gain over everything after the adoption (173 at or above 0, 16 at -10 bps or worse).

| Rule | Keep | Revert | Undecided | False revert (realised >= 0) | False keep (realised <= -10) | Median trades |
|---|---|---|---|---|---|---|
| fixed_20 | 78 | 244 | 0 | 67.6% | 6.2% | 20 |
| e_process | 0 | 322 | 0 | 100.0% | 0.0% | 125 |
| obf_110 | 279 | 43 | 0 | 12.1% | 87.5% | 110 |
| e_catoni | 1 | 321 | 0 | 100.0% | 0.0% | 125 |

Agreement on the adoptions both rules decided:

* fixed_20 vs e_process: 75.8% of 322
* fixed_20 vs obf_110: 34.5% of 322
* fixed_20 vs e_catoni: 75.5% of 322
* e_process vs obf_110: 13.4% of 322
* e_process vs e_catoni: 99.7% of 322
* obf_110 vs e_catoni: 13.7% of 322

<details><summary>Every replayed adoption</summary>

| Year | Strategy | Template | Candidate | Realised gain | Decisions | fixed_20 | e_process | obf_110 | e_catoni |
|---|---|---|---|---|---|---|---|---|---|
| 2008 | bollinger_mean_reversion | breadth_gate | breadth40 | +9.0 | 15572 | not_helping (20) | not_helping (133) | working (66) | not_helping (133) |
| 2008 | bollinger_mean_reversion | market_gate | sma50 | -0.2 | 15686 | not_helping (20) | not_helping (133) | working (66) | not_helping (133) |
| 2008 | bollinger_mean_reversion | stop_width | stop5 | +3.7 | 15751 | not_helping (20) | not_helping (120) | not_helping (56) | not_helping (120) |
| 2008 | bollinger_mean_reversion | time_stop | hold30 | +0.2 | 15561 | not_helping (20) | not_helping (133) | inconclusive (110) | not_helping (133) |
| 2008 | bollinger_mean_reversion | vix_gate | below25 | -6.0 | 15688 | not_helping (20) | not_helping (133) | inconclusive (110) | not_helping (133) |
| 2008 | channel_breakout | breadth_gate | breadth50 | -0.1 | 28211 | not_helping (20) | not_helping (127) | inconclusive (110) | not_helping (127) |
| 2008 | channel_breakout | earnings_blackout | off | -1.4 | 29288 | not_helping (20) | not_helping (129) | inconclusive (110) | not_helping (129) |
| 2008 | channel_breakout | market_gate | off | -3.7 | 31888 | not_helping (20) | not_helping (127) | inconclusive (110) | not_helping (127) |
| 2008 | channel_breakout | stop_width | stop5 | +2.9 | 28617 | not_helping (20) | not_helping (125) | not_helping (98) | not_helping (125) |
| 2008 | channel_breakout | time_stop | hold30 | -1.3 | 28690 | not_helping (20) | not_helping (148) | inconclusive (110) | not_helping (148) |
| 2008 | ensemble | breadth_gate | breadth60 | +8.7 | 87707 | working (20) | not_helping (121) | working (30) | not_helping (121) |
| 2008 | ensemble | market_gate | sma50 | +0.7 | 88332 | working (20) | not_helping (121) | working (30) | not_helping (121) |
| 2008 | ensemble | stop_width | stop5 | +11.0 | 90833 | not_helping (20) | not_helping (124) | inconclusive (110) | not_helping (124) |
| 2008 | ensemble | time_stop | hold30 | -0.2 | 84397 | not_helping (20) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2008 | ensemble | vix_gate | below25 | +3.8 | 87570 | not_helping (20) | not_helping (126) | inconclusive (110) | not_helping (126) |
| 2008 | ibs_mean_reversion | earnings_blackout | off | +0.0 | 1439 | working (20) | not_helping (122) | inconclusive (110) | not_helping (122) |
| 2008 | ibs_mean_reversion | sector_gate | sector10 | +0.0 | 1439 | working (20) | not_helping (122) | inconclusive (110) | not_helping (122) |
| 2008 | ibs_mean_reversion | stop_width | stop15 | +1.1 | 1440 | working (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2008 | ibs_mean_reversion | time_stop | hold15 | +0.1 | 1439 | working (20) | not_helping (122) | inconclusive (110) | not_helping (122) |
| 2008 | macd_momentum | breadth_gate | breadth60 | +3.9 | 107077 | not_helping (20) | not_helping (171) | inconclusive (110) | not_helping (171) |
| 2008 | macd_momentum | stop_width | stop5 | +7.3 | 107010 | not_helping (20) | not_helping (171) | inconclusive (110) | not_helping (171) |
| 2008 | macd_momentum | time_stop | hold30 | -0.6 | 101768 | not_helping (20) | not_helping (134) | inconclusive (110) | not_helping (134) |
| 2008 | macd_momentum | vix_gate | below20 | +4.2 | 106093 | not_helping (20) | not_helping (171) | inconclusive (110) | not_helping (171) |
| 2008 | moving_average_crossover | breadth_gate | breadth60 | +9.2 | 168060 | working (26) | not_helping (140) | working (93) | not_helping (140) |
| 2008 | moving_average_crossover | earnings_blackout | off | -0.8 | 161398 | not_helping (20) | not_helping (167) | inconclusive (110) | not_helping (167) |
| 2008 | moving_average_crossover | market_gate | sma50 | +3.7 | 171448 | working (20) | not_helping (140) | working (93) | not_helping (140) |
| 2008 | moving_average_crossover | sector_gate | sector50 | +0.4 | 155671 | not_helping (20) | not_helping (140) | not_helping (93) | not_helping (140) |
| 2008 | moving_average_crossover | stop_width | stop5 | +9.5 | 179694 | not_helping (20) | not_helping (130) | inconclusive (110) | not_helping (130) |
| 2008 | moving_average_crossover | time_stop | hold30 | +0.5 | 164624 | not_helping (20) | not_helping (144) | inconclusive (110) | not_helping (144) |
| 2008 | moving_average_crossover | vix_gate | below25 | +6.2 | 165409 | not_helping (20) | not_helping (144) | inconclusive (110) | not_helping (144) |
| 2008 | rsi2_pullback | earnings_blackout | off | +2.4 | 3086 | not_helping (20) | not_helping (122) | inconclusive (110) | not_helping (122) |
| 2008 | rsi2_pullback | market_gate | off | +1.6 | 3977 | not_helping (20) | not_helping (16) | not_helping (68) | not_helping (4) |
| 2008 | rsi2_pullback | sector_gate | sector50 | +1.8 | 2708 | not_helping (20) | not_helping (123) | inconclusive (110) | not_helping (123) |
| 2008 | rsi2_pullback | time_stop | hold15 | +0.8 | 2694 | not_helping (20) | not_helping (123) | inconclusive (110) | not_helping (123) |
| 2008 | rsi_mean_reversion | breadth_gate | breadth40 | -1.8 | 19137 | not_helping (20) | not_helping (138) | working (85) | not_helping (138) |
| 2008 | rsi_mean_reversion | market_gate | sma50 | -6.4 | 19580 | not_helping (20) | not_helping (138) | working (85) | not_helping (138) |
| 2008 | rsi_mean_reversion | time_stop | hold30 | -0.0 | 19005 | not_helping (20) | not_helping (138) | inconclusive (110) | not_helping (138) |
| 2008 | rsi_mean_reversion | vix_gate | below20 | -9.1 | 19463 | working (20) | not_helping (138) | working (85) | not_helping (138) |
| 2008 | trend_pullback | earnings_blackout | off | -0.6 | 45722 | not_helping (20) | not_helping (132) | inconclusive (110) | not_helping (132) |
| 2008 | trend_pullback | market_gate | sma50 | -2.5 | 68163 | working (20) | not_helping (130) | inconclusive (110) | not_helping (130) |
| 2008 | trend_pullback | stop_width | stop5 | +4.0 | 45799 | not_helping (20) | not_helping (133) | inconclusive (110) | not_helping (133) |
| 2008 | trend_pullback | time_stop | hold30 | +1.3 | 45198 | not_helping (20) | not_helping (124) | not_helping (124) | not_helping (124) |
| 2010 | bollinger_mean_reversion | breadth_gate | breadth60 | -0.1 | 13962 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2010 | bollinger_mean_reversion | market_gate | sma50 | -12.0 | 13976 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2010 | bollinger_mean_reversion | time_stop | hold30 | -0.1 | 13857 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2010 | bollinger_mean_reversion | vix_gate | below20 | -13.6 | 14032 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2010 | channel_breakout | breadth_gate | breadth60 | +1.6 | 28261 | working (20) | not_helping (128) | inconclusive (110) | not_helping (128) |
| 2010 | channel_breakout | earnings_blackout | off | -1.4 | 28985 | working (20) | not_helping (130) | inconclusive (110) | not_helping (130) |
| 2010 | channel_breakout | stop_width | stop5 | +2.7 | 28319 | working (20) | not_helping (122) | not_helping (122) | not_helping (122) |
| 2010 | channel_breakout | time_stop | hold30 | -1.4 | 28395 | working (20) | not_helping (143) | inconclusive (110) | not_helping (143) |
| 2010 | ensemble | breadth_gate | breadth50 | +0.2 | 72626 | not_helping (20) | not_helping (128) | inconclusive (110) | not_helping (128) |
| 2010 | ensemble | market_gate | sma50 | -3.6 | 74514 | not_helping (20) | not_helping (128) | inconclusive (110) | not_helping (128) |
| 2010 | ensemble | stop_width | stop5 | +8.9 | 76747 | not_helping (20) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2010 | ensemble | time_stop | hold30 | -0.2 | 71130 | not_helping (20) | not_helping (142) | inconclusive (110) | not_helping (142) |
| 2010 | ensemble | vix_gate | below20 | -1.9 | 75554 | not_helping (20) | not_helping (159) | inconclusive (110) | not_helping (159) |
| 2010 | ibs_mean_reversion | earnings_blackout | off | +0.0 | 1256 | working (20) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2010 | ibs_mean_reversion | sector_gate | sector10 | +0.0 | 1256 | working (20) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2010 | ibs_mean_reversion | stop_width | stop15 | +1.5 | 1256 | working (20) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2010 | macd_momentum | breadth_gate | breadth60 | -4.0 | 95116 | working (20) | not_helping (130) | inconclusive (110) | not_helping (130) |
| 2010 | macd_momentum | sector_gate | sector50 | +0.2 | 89710 | working (20) | not_helping (130) | inconclusive (110) | not_helping (130) |
| 2010 | macd_momentum | stop_width | stop5 | +6.2 | 94727 | working (20) | not_helping (132) | inconclusive (110) | not_helping (132) |
| 2010 | macd_momentum | time_stop | hold30 | -0.6 | 89973 | working (20) | not_helping (135) | inconclusive (110) | not_helping (135) |
| 2010 | macd_momentum | vix_gate | below20 | -1.7 | 93835 | working (20) | not_helping (141) | inconclusive (110) | not_helping (141) |
| 2010 | moving_average_crossover | breadth_gate | breadth60 | +2.8 | 146076 | working (20) | not_helping (145) | inconclusive (110) | not_helping (145) |
| 2010 | moving_average_crossover | market_gate | sma50 | -0.2 | 148657 | working (20) | not_helping (151) | inconclusive (110) | not_helping (151) |
| 2010 | moving_average_crossover | sector_gate | sector50 | +0.4 | 134492 | working (20) | not_helping (147) | inconclusive (110) | not_helping (147) |
| 2010 | moving_average_crossover | stop_width | stop5 | +8.2 | 156322 | working (20) | not_helping (166) | inconclusive (110) | not_helping (166) |
| 2010 | moving_average_crossover | vix_gate | below20 | +1.7 | 148771 | working (20) | not_helping (156) | inconclusive (110) | not_helping (156) |
| 2010 | rsi2_pullback | earnings_blackout | off | +2.3 | 2918 | not_helping (20) | not_helping (120) | not_helping (67) | not_helping (120) |
| 2010 | rsi2_pullback | sector_gate | sector50 | +1.1 | 2557 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2010 | rsi2_pullback | time_stop | hold15 | +0.8 | 2543 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2010 | rsi2_pullback | vix_gate | below30 | -2.9 | 2547 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2010 | rsi_mean_reversion | breadth_gate | breadth40 | -12.5 | 17000 | not_helping (20) | not_helping (122) | inconclusive (110) | not_helping (122) |
| 2010 | rsi_mean_reversion | market_gate | sma50 | -17.5 | 17417 | not_helping (20) | not_helping (121) | working (70) | not_helping (121) |
| 2010 | rsi_mean_reversion | time_stop | hold30 | +0.1 | 16901 | not_helping (20) | not_helping (127) | inconclusive (110) | not_helping (127) |
| 2010 | rsi_mean_reversion | vix_gate | below20 | -19.4 | 17340 | not_helping (20) | not_helping (125) | working (92) | not_helping (125) |
| 2010 | trend_pullback | breadth_gate | breadth60 | -1.9 | 42995 | not_helping (20) | not_helping (130) | inconclusive (110) | not_helping (130) |
| 2010 | trend_pullback | earnings_blackout | off | -0.6 | 43053 | not_helping (20) | not_helping (124) | not_helping (124) | not_helping (124) |
| 2010 | trend_pullback | market_gate | sma50 | -4.7 | 63612 | not_helping (20) | not_helping (124) | inconclusive (110) | not_helping (124) |
| 2010 | trend_pullback | sector_gate | sector50 | +0.1 | 41674 | not_helping (20) | not_helping (130) | inconclusive (110) | not_helping (130) |
| 2010 | trend_pullback | stop_width | stop5 | +3.7 | 43166 | not_helping (20) | not_helping (147) | inconclusive (110) | not_helping (147) |
| 2010 | trend_pullback | time_stop | hold30 | +1.3 | 42594 | not_helping (20) | not_helping (130) | inconclusive (110) | not_helping (130) |
| 2010 | trend_pullback | vix_gate | below30 | +0.3 | 41727 | not_helping (20) | not_helping (130) | inconclusive (110) | not_helping (130) |
| 2012 | bollinger_mean_reversion | breadth_gate | breadth60 | +2.4 | 12722 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2012 | bollinger_mean_reversion | market_gate | sma50 | -10.7 | 12733 | not_helping (20) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2012 | bollinger_mean_reversion | time_stop | hold30 | +0.2 | 12616 | not_helping (20) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2012 | channel_breakout | breadth_gate | breadth60 | +1.7 | 25868 | working (20) | not_helping (132) | inconclusive (110) | not_helping (132) |
| 2012 | ensemble | breadth_gate | breadth60 | +0.7 | 67399 | not_helping (20) | not_helping (124) | inconclusive (110) | not_helping (124) |
| 2012 | ensemble | market_gate | sma50 | -3.4 | 67627 | working (20) | not_helping (134) | inconclusive (110) | not_helping (134) |
| 2012 | ensemble | sector_gate | sector50 | +0.3 | 64447 | working (20) | not_helping (134) | inconclusive (110) | not_helping (134) |
| 2012 | ensemble | stop_width | stop5 | +8.7 | 69624 | working (20) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2012 | ensemble | time_stop | hold30 | -0.2 | 64507 | working (20) | not_helping (129) | not_helping (92) | not_helping (129) |
| 2012 | ensemble | vix_gate | below20 | -4.1 | 68320 | not_helping (20) | not_helping (126) | inconclusive (110) | not_helping (126) |
| 2012 | ibs_mean_reversion | earnings_blackout | off | +0.0 | 1084 | not_helping (20) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2012 | ibs_mean_reversion | sector_gate | sector10 | +0.0 | 1084 | not_helping (20) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2012 | ibs_mean_reversion | stop_width | stop15 | -0.1 | 1084 | not_helping (20) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2012 | ibs_mean_reversion | time_stop | hold15 | +0.0 | 1084 | not_helping (20) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2012 | macd_momentum | breadth_gate | breadth60 | -3.8 | 85980 | not_helping (20) | not_helping (126) | not_helping (84) | not_helping (126) |
| 2012 | macd_momentum | stop_width | stop5 | +5.6 | 85808 | working (20) | not_helping (143) | working (95) | not_helping (143) |
| 2012 | macd_momentum | time_stop | hold30 | -0.9 | 81511 | not_helping (20) | not_helping (157) | inconclusive (110) | not_helping (157) |
| 2012 | macd_momentum | vix_gate | below25 | +1.9 | 82327 | not_helping (20) | not_helping (127) | not_helping (84) | not_helping (127) |
| 2012 | moving_average_crossover | breadth_gate | breadth60 | +2.4 | 131938 | not_helping (20) | not_helping (283) | not_helping (50) | not_helping (283) |
| 2012 | moving_average_crossover | market_gate | sma50 | -0.9 | 133515 | not_helping (20) | not_helping (153) | inconclusive (110) | not_helping (153) |
| 2012 | moving_average_crossover | sector_gate | sector50 | +0.3 | 120843 | not_helping (20) | not_helping (131) | inconclusive (110) | not_helping (131) |
| 2012 | moving_average_crossover | stop_width | stop5 | +8.0 | 140508 | working (20) | not_helping (127) | inconclusive (110) | not_helping (127) |
| 2012 | moving_average_crossover | time_stop | hold30 | +0.6 | 128712 | not_helping (20) | not_helping (139) | inconclusive (110) | not_helping (139) |
| 2012 | moving_average_crossover | vix_gate | below20 | -0.8 | 133137 | not_helping (20) | not_helping (131) | not_helping (50) | not_helping (131) |
| 2012 | rsi2_pullback | breadth_gate | breadth40 | -1.8 | 2349 | working (20) | not_helping (123) | inconclusive (110) | not_helping (123) |
| 2012 | rsi2_pullback | earnings_blackout | off | +2.1 | 2689 | working (20) | not_helping (125) | inconclusive (110) | not_helping (125) |
| 2012 | rsi2_pullback | sector_gate | sector20 | +5.9 | 2375 | working (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2012 | rsi2_pullback | stop_width | stop15 | +1.6 | 2349 | working (20) | not_helping (123) | inconclusive (110) | not_helping (123) |
| 2012 | rsi2_pullback | time_stop | hold15 | +0.7 | 2348 | working (20) | not_helping (123) | inconclusive (110) | not_helping (123) |
| 2012 | rsi2_pullback | vix_gate | below30 | -3.6 | 2352 | working (20) | not_helping (123) | inconclusive (110) | not_helping (123) |
| 2012 | rsi_mean_reversion | breadth_gate | breadth60 | -8.4 | 15658 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2012 | rsi_mean_reversion | market_gate | sma20 | -13.8 | 15985 | not_helping (20) | not_helping (122) | inconclusive (110) | not_helping (122) |
| 2012 | rsi_mean_reversion | time_stop | hold30 | +0.3 | 15382 | not_helping (20) | not_helping (122) | working (90) | not_helping (122) |
| 2012 | rsi_mean_reversion | vix_gate | below30 | -15.1 | 15473 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2012 | trend_pullback | breadth_gate | breadth60 | -1.9 | 39260 | not_helping (20) | not_helping (121) | not_helping (52) | not_helping (121) |
| 2012 | trend_pullback | market_gate | sma20 | -6.0 | 50133 | not_helping (20) | not_helping (125) | inconclusive (110) | not_helping (125) |
| 2012 | trend_pullback | sector_gate | sector20 | +0.4 | 38140 | not_helping (20) | not_helping (124) | inconclusive (110) | not_helping (124) |
| 2012 | trend_pullback | stop_width | stop5 | +3.6 | 39369 | not_helping (20) | not_helping (124) | inconclusive (110) | not_helping (124) |
| 2012 | trend_pullback | time_stop | hold30 | +1.5 | 38844 | not_helping (20) | not_helping (130) | inconclusive (110) | not_helping (130) |
| 2012 | trend_pullback | vix_gate | below25 | -2.7 | 38377 | not_helping (20) | not_helping (125) | inconclusive (110) | not_helping (125) |
| 2014 | bollinger_mean_reversion | stop_width | stop5 | +2.1 | 11649 | not_helping (20) | not_helping (128) | inconclusive (110) | not_helping (128) |
| 2014 | bollinger_mean_reversion | time_stop | hold30 | -0.1 | 11497 | not_helping (20) | not_helping (122) | working (68) | not_helping (122) |
| 2014 | channel_breakout | breadth_gate | breadth40 | -0.2 | 21250 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2014 | channel_breakout | stop_width | stop5 | +0.0 | 21646 | not_helping (20) | not_helping (124) | not_helping (46) | not_helping (124) |
| 2014 | channel_breakout | time_stop | hold30 | -1.8 | 21700 | not_helping (20) | not_helping (131) | not_helping (102) | not_helping (131) |
| 2014 | ensemble | breadth_gate | breadth60 | +1.5 | 61151 | not_helping (20) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2014 | ensemble | earnings_blackout | off | -0.5 | 60074 | not_helping (20) | not_helping (123) | inconclusive (110) | not_helping (123) |
| 2014 | ensemble | stop_width | stop5 | +8.4 | 63103 | not_helping (20) | not_helping (129) | inconclusive (110) | not_helping (129) |
| 2014 | ensemble | time_stop | hold30 | -0.6 | 58408 | not_helping (20) | not_helping (127) | inconclusive (110) | not_helping (127) |
| 2014 | ensemble | vix_gate | below20 | -3.9 | 61888 | not_helping (20) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2014 | ibs_mean_reversion | earnings_blackout | off | +0.0 | 951 | working (23) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2014 | ibs_mean_reversion | sector_gate | sector10 | +0.0 | 951 | working (23) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2014 | ibs_mean_reversion | stop_width | stop15 | -0.1 | 951 | working (23) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2014 | ibs_mean_reversion | time_stop | hold15 | -0.1 | 951 | working (22) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2014 | macd_momentum | breadth_gate | breadth60 | -3.8 | 78051 | not_helping (20) | not_helping (127) | inconclusive (110) | not_helping (127) |
| 2014 | macd_momentum | stop_width | stop5 | +5.2 | 77756 | not_helping (20) | not_helping (125) | inconclusive (110) | not_helping (125) |
| 2014 | macd_momentum | time_stop | hold30 | -0.9 | 73738 | not_helping (20) | not_helping (130) | not_helping (110) | not_helping (130) |
| 2014 | macd_momentum | vix_gate | below20 | -1.7 | 76937 | not_helping (20) | not_helping (127) | inconclusive (110) | not_helping (127) |
| 2014 | moving_average_crossover | breadth_gate | breadth60 | +3.2 | 118743 | not_helping (20) | not_helping (123) | inconclusive (110) | not_helping (123) |
| 2014 | moving_average_crossover | market_gate | sma50 | -0.1 | 120136 | not_helping (20) | not_helping (127) | inconclusive (110) | not_helping (127) |
| 2014 | moving_average_crossover | stop_width | stop5 | +7.8 | 126014 | not_helping (20) | not_helping (133) | inconclusive (110) | not_helping (133) |
| 2014 | moving_average_crossover | time_stop | hold30 | +0.4 | 115015 | not_helping (20) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2014 | moving_average_crossover | vix_gate | below20 | -0.2 | 119753 | not_helping (20) | not_helping (124) | inconclusive (110) | not_helping (124) |
| 2014 | rsi2_pullback | breadth_gate | breadth40 | -2.1 | 2012 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2014 | rsi2_pullback | earnings_blackout | off | +1.2 | 2291 | not_helping (22) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2014 | rsi2_pullback | stop_width | stop15 | +1.6 | 2012 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2014 | rsi2_pullback | time_stop | hold15 | +0.7 | 2011 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2014 | rsi2_pullback | vix_gate | below30 | -4.2 | 2015 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2014 | rsi_mean_reversion | earnings_blackout | off | -0.0 | 14341 | not_helping (20) | not_helping (123) | not_helping (90) | not_helping (123) |
| 2014 | rsi_mean_reversion | stop_width | stop5 | +1.4 | 14279 | not_helping (20) | not_helping (127) | not_helping (27) | not_helping (127) |
| 2014 | rsi_mean_reversion | time_stop | hold30 | +0.1 | 13957 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2014 | rsi_mean_reversion | vix_gate | below30 | -16.6 | 14051 | not_helping (20) | not_helping (136) | inconclusive (110) | not_helping (136) |
| 2014 | trend_pullback | market_gate | off | -1.5 | 60840 | not_helping (20) | not_helping (135) | working (55) | not_helping (135) |
| 2014 | trend_pullback | sector_gate | sector20 | +0.3 | 33659 | not_helping (20) | not_helping (124) | inconclusive (110) | not_helping (124) |
| 2014 | trend_pullback | stop_width | stop5 | +3.7 | 34764 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2014 | trend_pullback | time_stop | hold30 | +1.4 | 34287 | not_helping (20) | not_helping (126) | inconclusive (110) | not_helping (126) |
| 2014 | trend_pullback | vix_gate | below25 | -3.0 | 33933 | not_helping (20) | not_helping (124) | inconclusive (110) | not_helping (124) |
| 2016 | bollinger_mean_reversion | breadth_gate | breadth60 | +1.5 | 10111 | not_helping (20) | not_helping (158) | working (29) | not_helping (158) |
| 2016 | bollinger_mean_reversion | earnings_blackout | off | -0.9 | 10295 | not_helping (20) | not_helping (144) | not_helping (144) | not_helping (144) |
| 2016 | bollinger_mean_reversion | stop_width | stop5 | +4.0 | 10142 | not_helping (20) | not_helping (137) | inconclusive (110) | not_helping (137) |
| 2016 | bollinger_mean_reversion | time_stop | hold30 | -0.3 | 10003 | not_helping (20) | not_helping (182) | inconclusive (110) | not_helping (182) |
| 2016 | channel_breakout | breadth_gate | breadth60 | -0.8 | 18762 | not_helping (20) | not_helping (120) | not_helping (79) | not_helping (120) |
| 2016 | channel_breakout | stop_width | stop5 | +1.0 | 18615 | working (21) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2016 | channel_breakout | time_stop | hold30 | -2.6 | 18640 | not_helping (20) | not_helping (122) | inconclusive (110) | not_helping (122) |
| 2016 | ensemble | breadth_gate | breadth60 | -0.7 | 56092 | not_helping (20) | not_helping (158) | working (30) | inconclusive (158) |
| 2016 | ensemble | earnings_blackout | off | -0.7 | 54981 | not_helping (20) | not_helping (164) | inconclusive (110) | not_helping (164) |
| 2016 | ensemble | stop_width | stop5 | +8.6 | 57804 | not_helping (20) | not_helping (167) | inconclusive (110) | not_helping (167) |
| 2016 | ensemble | time_stop | hold30 | -0.7 | 53417 | not_helping (20) | not_helping (166) | inconclusive (110) | not_helping (166) |
| 2016 | ensemble | vix_gate | below25 | -3.0 | 55560 | not_helping (20) | not_helping (172) | inconclusive (110) | not_helping (172) |
| 2016 | ibs_mean_reversion | earnings_blackout | off | +0.0 | 803 | working (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2016 | ibs_mean_reversion | sector_gate | sector10 | +0.0 | 803 | working (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2016 | ibs_mean_reversion | stop_width | stop15 | +0.0 | 803 | working (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2016 | ibs_mean_reversion | time_stop | hold15 | -0.1 | 803 | working (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2016 | macd_momentum | breadth_gate | breadth60 | -5.7 | 70856 | not_helping (20) | not_helping (319) | inconclusive (110) | not_helping (319) |
| 2016 | macd_momentum | stop_width | stop5 | +4.9 | 70485 | working (20) | not_helping (319) | inconclusive (110) | not_helping (319) |
| 2016 | macd_momentum | time_stop | hold30 | -1.4 | 66739 | not_helping (20) | not_helping (340) | inconclusive (110) | not_helping (340) |
| 2016 | moving_average_crossover | breadth_gate | breadth60 | +2.1 | 108125 | not_helping (20) | not_helping (120) | working (64) | not_helping (120) |
| 2016 | moving_average_crossover | stop_width | stop5 | +8.0 | 113945 | not_helping (20) | not_helping (127) | inconclusive (110) | not_helping (127) |
| 2016 | moving_average_crossover | time_stop | hold30 | +0.1 | 103510 | not_helping (20) | not_helping (124) | inconclusive (110) | not_helping (124) |
| 2016 | moving_average_crossover | vix_gate | below25 | +1.3 | 104542 | not_helping (20) | not_helping (127) | inconclusive (110) | not_helping (127) |
| 2016 | rsi2_pullback | breadth_gate | breadth40 | -2.4 | 1739 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2016 | rsi2_pullback | earnings_blackout | off | +0.5 | 1982 | working (20) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2016 | rsi2_pullback | stop_width | stop15 | +1.6 | 1739 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2016 | rsi2_pullback | time_stop | hold15 | +0.6 | 1738 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2016 | rsi2_pullback | vix_gate | below30 | -4.8 | 1742 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2016 | rsi_mean_reversion | breadth_gate | breadth60 | -11.6 | 12671 | not_helping (20) | not_helping (127) | working (25) | not_helping (127) |
| 2016 | rsi_mean_reversion | earnings_blackout | off | -0.3 | 12781 | not_helping (20) | not_helping (123) | inconclusive (110) | not_helping (123) |
| 2016 | rsi_mean_reversion | stop_width | stop5 | +2.5 | 12737 | not_helping (20) | not_helping (130) | not_helping (116) | not_helping (130) |
| 2016 | rsi_mean_reversion | time_stop | hold30 | -0.3 | 12430 | not_helping (20) | not_helping (135) | inconclusive (110) | not_helping (135) |
| 2016 | trend_pullback | breadth_gate | breadth60 | -3.5 | 30262 | not_helping (20) | not_helping (122) | not_helping (115) | not_helping (122) |
| 2016 | trend_pullback | market_gate | off | -2.4 | 52283 | not_helping (20) | not_helping (150) | not_helping (24) | not_helping (150) |
| 2016 | trend_pullback | sector_gate | sector20 | +0.3 | 29344 | working (20) | not_helping (122) | inconclusive (110) | not_helping (122) |
| 2016 | trend_pullback | stop_width | stop5 | +4.5 | 30311 | working (20) | not_helping (123) | inconclusive (110) | not_helping (123) |
| 2016 | trend_pullback | time_stop | hold30 | +1.3 | 29837 | working (20) | not_helping (123) | inconclusive (110) | not_helping (123) |
| 2016 | trend_pullback | vix_gate | below30 | +0.5 | 29266 | working (20) | not_helping (122) | inconclusive (110) | not_helping (122) |
| 2018 | bollinger_mean_reversion | breadth_gate | breadth60 | +0.4 | 8662 | not_helping (20) | not_helping (151) | inconclusive (110) | not_helping (151) |
| 2018 | bollinger_mean_reversion | earnings_blackout | off | -0.9 | 8826 | not_helping (20) | not_helping (153) | inconclusive (110) | not_helping (153) |
| 2018 | bollinger_mean_reversion | stop_width | stop5 | +3.5 | 8701 | not_helping (20) | not_helping (145) | not_helping (145) | not_helping (145) |
| 2018 | bollinger_mean_reversion | time_stop | hold30 | +0.8 | 8575 | not_helping (20) | not_helping (174) | inconclusive (110) | not_helping (174) |
| 2018 | bollinger_mean_reversion | vix_gate | below30 | -17.4 | 8610 | not_helping (20) | not_helping (152) | not_helping (152) | not_helping (152) |
| 2018 | channel_breakout | breadth_gate | breadth60 | +0.1 | 14684 | not_helping (20) | not_helping (133) | inconclusive (110) | not_helping (133) |
| 2018 | channel_breakout | market_gate | off | -7.5 | 16431 | not_helping (20) | not_helping (133) | inconclusive (110) | not_helping (133) |
| 2018 | channel_breakout | stop_width | stop5 | +0.8 | 14490 | working (20) | not_helping (136) | not_helping (53) | not_helping (136) |
| 2018 | channel_breakout | time_stop | hold30 | -2.1 | 14463 | not_helping (20) | not_helping (137) | working (48) | not_helping (137) |
| 2018 | ensemble | breadth_gate | breadth60 | +0.5 | 49897 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2018 | ensemble | earnings_blackout | off | -0.7 | 48829 | not_helping (20) | not_helping (129) | not_helping (118) | not_helping (129) |
| 2018 | ensemble | stop_width | stop5 | +8.6 | 51414 | not_helping (20) | not_helping (140) | inconclusive (110) | not_helping (140) |
| 2018 | ensemble | time_stop | hold30 | -0.5 | 47375 | not_helping (20) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2018 | ensemble | vix_gate | below30 | -5.6 | 48660 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2018 | ibs_mean_reversion | earnings_blackout | off | +0.0 | 674 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2018 | ibs_mean_reversion | sector_gate | sector10 | +0.0 | 674 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2018 | ibs_mean_reversion | time_stop | hold15 | -0.1 | 674 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2018 | macd_momentum | stop_width | stop5 | +4.5 | 61838 | not_helping (20) | not_helping (141) | not_helping (141) | not_helping (141) |
| 2018 | macd_momentum | time_stop | hold30 | -1.2 | 58450 | not_helping (20) | not_helping (122) | inconclusive (110) | not_helping (122) |
| 2018 | macd_momentum | vix_gate | below20 | -2.1 | 61808 | not_helping (20) | not_helping (131) | inconclusive (110) | not_helping (131) |
| 2018 | moving_average_crossover | breadth_gate | breadth60 | +3.1 | 95559 | not_helping (20) | not_helping (127) | inconclusive (110) | not_helping (127) |
| 2018 | moving_average_crossover | stop_width | stop5 | +8.1 | 100331 | not_helping (20) | not_helping (158) | inconclusive (110) | not_helping (158) |
| 2018 | moving_average_crossover | time_stop | hold30 | -0.2 | 90266 | not_helping (20) | not_helping (133) | working (102) | not_helping (133) |
| 2018 | moving_average_crossover | vix_gate | below25 | +1.3 | 92702 | not_helping (20) | not_helping (127) | inconclusive (110) | not_helping (127) |
| 2018 | rsi2_pullback | breadth_gate | breadth40 | -3.0 | 1398 | not_helping (20) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2018 | rsi2_pullback | earnings_blackout | off | -1.7 | 1594 | not_helping (20) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2018 | rsi2_pullback | stop_width | stop15 | +2.0 | 1398 | not_helping (20) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2018 | rsi2_pullback | time_stop | hold15 | +0.8 | 1397 | not_helping (20) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2018 | rsi2_pullback | vix_gate | below30 | -6.0 | 1401 | not_helping (20) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2018 | rsi_mean_reversion | breadth_gate | breadth60 | -13.4 | 11025 | not_helping (20) | not_helping (124) | inconclusive (110) | not_helping (124) |
| 2018 | rsi_mean_reversion | earnings_blackout | off | +0.1 | 11111 | not_helping (20) | not_helping (127) | inconclusive (110) | not_helping (127) |
| 2018 | rsi_mean_reversion | stop_width | stop5 | +1.3 | 11082 | not_helping (20) | not_helping (124) | inconclusive (110) | not_helping (124) |
| 2018 | rsi_mean_reversion | time_stop | hold30 | +0.3 | 10799 | not_helping (20) | not_helping (139) | inconclusive (110) | not_helping (139) |
| 2018 | rsi_mean_reversion | vix_gate | below25 | -17.8 | 11014 | not_helping (20) | not_helping (127) | inconclusive (110) | not_helping (127) |
| 2018 | trend_pullback | breadth_gate | breadth60 | -3.1 | 25239 | working (20) | not_helping (125) | inconclusive (110) | not_helping (125) |
| 2018 | trend_pullback | market_gate | off | -6.2 | 44318 | working (20) | not_helping (126) | inconclusive (110) | not_helping (126) |
| 2018 | trend_pullback | stop_width | stop5 | +3.8 | 25059 | working (20) | not_helping (124) | not_helping (54) | not_helping (124) |
| 2018 | trend_pullback | time_stop | hold30 | +0.8 | 24615 | working (20) | not_helping (122) | inconclusive (110) | not_helping (122) |
| 2018 | trend_pullback | vix_gate | below30 | +0.6 | 24273 | working (20) | not_helping (125) | inconclusive (110) | not_helping (125) |
| 2020 | bollinger_mean_reversion | earnings_blackout | off | -0.6 | 7027 | not_helping (20) | not_helping (179) | not_helping (116) | not_helping (179) |
| 2020 | bollinger_mean_reversion | stop_width | stop5 | +2.5 | 6932 | not_helping (20) | not_helping (172) | inconclusive (110) | not_helping (172) |
| 2020 | bollinger_mean_reversion | time_stop | hold30 | -0.4 | 6832 | not_helping (20) | not_helping (172) | inconclusive (110) | not_helping (172) |
| 2020 | channel_breakout | breadth_gate | breadth60 | +0.2 | 10772 | not_helping (20) | not_helping (135) | inconclusive (110) | not_helping (135) |
| 2020 | channel_breakout | market_gate | off | -11.0 | 12266 | not_helping (20) | not_helping (135) | inconclusive (110) | not_helping (135) |
| 2020 | channel_breakout | stop_width | stop5 | +0.2 | 10662 | working (21) | not_helping (124) | not_helping (124) | not_helping (124) |
| 2020 | channel_breakout | time_stop | hold30 | -3.6 | 10584 | not_helping (20) | not_helping (129) | inconclusive (110) | not_helping (129) |
| 2020 | ensemble | earnings_blackout | off | -0.8 | 41954 | not_helping (20) | not_helping (132) | not_helping (92) | not_helping (132) |
| 2020 | ensemble | stop_width | stop5 | +9.3 | 44280 | not_helping (20) | not_helping (125) | working (39) | not_helping (125) |
| 2020 | ensemble | time_stop | hold30 | -1.0 | 40718 | not_helping (20) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2020 | ibs_mean_reversion | earnings_blackout | off | +0.0 | 541 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2020 | ibs_mean_reversion | sector_gate | sector10 | +0.0 | 541 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2020 | macd_momentum | stop_width | stop5 | +5.0 | 52577 | working (20) | not_helping (137) | inconclusive (110) | not_helping (137) |
| 2020 | macd_momentum | time_stop | hold30 | -0.9 | 49578 | working (20) | not_helping (125) | working (125) | not_helping (125) |
| 2020 | macd_momentum | vix_gate | below25 | +3.1 | 50789 | working (20) | not_helping (132) | inconclusive (110) | not_helping (132) |
| 2020 | moving_average_crossover | breadth_gate | breadth60 | +4.2 | 78809 | not_helping (20) | not_helping (129) | inconclusive (110) | not_helping (129) |
| 2020 | moving_average_crossover | stop_width | stop5 | +9.0 | 83977 | working (20) | not_helping (152) | inconclusive (110) | not_helping (152) |
| 2020 | moving_average_crossover | time_stop | hold30 | -0.3 | 75093 | not_helping (20) | not_helping (121) | working (88) | not_helping (121) |
| 2020 | moving_average_crossover | vix_gate | below25 | +1.3 | 78389 | not_helping (20) | not_helping (128) | inconclusive (110) | not_helping (128) |
| 2020 | rsi2_pullback | breadth_gate | breadth40 | -3.8 | 1124 | not_helping (20) | not_helping (120) | not_helping (67) | not_helping (120) |
| 2020 | rsi2_pullback | earnings_blackout | off | +0.3 | 1281 | not_helping (20) | not_helping (128) | inconclusive (110) | not_helping (128) |
| 2020 | rsi2_pullback | market_gate | off | -5.8 | 1570 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2020 | rsi2_pullback | sector_gate | sector20 | +7.3 | 1137 | not_helping (20) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2020 | rsi2_pullback | stop_width | stop15 | +0.5 | 1124 | not_helping (20) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2020 | rsi2_pullback | time_stop | hold15 | +1.0 | 1123 | not_helping (20) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2020 | rsi2_pullback | vix_gate | below30 | -7.5 | 1127 | not_helping (20) | not_helping (120) | not_helping (67) | not_helping (120) |
| 2020 | rsi_mean_reversion | stop_width | stop5 | +1.0 | 9173 | not_helping (20) | not_helping (134) | inconclusive (110) | not_helping (134) |
| 2020 | rsi_mean_reversion | time_stop | hold30 | -0.4 | 8940 | not_helping (20) | not_helping (134) | inconclusive (110) | not_helping (134) |
| 2020 | trend_pullback | earnings_blackout | off | -0.8 | 20373 | not_helping (20) | not_helping (123) | inconclusive (110) | not_helping (123) |
| 2020 | trend_pullback | market_gate | off | -5.1 | 35833 | not_helping (20) | not_helping (133) | working (33) | not_helping (133) |
| 2020 | trend_pullback | stop_width | stop5 | +3.9 | 20452 | working (20) | not_helping (124) | working (102) | not_helping (124) |
| 2020 | trend_pullback | time_stop | hold30 | -1.0 | 20036 | not_helping (20) | not_helping (133) | inconclusive (110) | not_helping (133) |
| 2022 | bollinger_mean_reversion | breadth_gate | breadth50 | +6.6 | 5088 | not_helping (20) | not_helping (145) | inconclusive (110) | not_helping (145) |
| 2022 | bollinger_mean_reversion | stop_width | stop5 | +0.3 | 5135 | not_helping (20) | not_helping (134) | inconclusive (110) | not_helping (134) |
| 2022 | bollinger_mean_reversion | time_stop | hold30 | -0.8 | 5059 | not_helping (20) | not_helping (142) | inconclusive (110) | not_helping (142) |
| 2022 | channel_breakout | market_gate | off | -12.3 | 10025 | working (20) | not_helping (123) | inconclusive (110) | not_helping (123) |
| 2022 | channel_breakout | stop_width | stop5 | +2.9 | 8507 | working (20) | not_helping (123) | inconclusive (110) | not_helping (123) |
| 2022 | channel_breakout | time_stop | hold30 | -3.9 | 8418 | working (20) | not_helping (123) | inconclusive (110) | not_helping (123) |
| 2022 | ensemble | earnings_blackout | off | -0.9 | 27415 | not_helping (20) | not_helping (142) | inconclusive (110) | not_helping (142) |
| 2022 | ensemble | stop_width | stop5 | +9.2 | 28861 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2022 | ensemble | time_stop | hold30 | -1.0 | 26545 | not_helping (20) | not_helping (140) | inconclusive (110) | not_helping (140) |
| 2022 | ibs_mean_reversion | earnings_blackout | off | +0.0 | 373 | not_helping (20) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2022 | ibs_mean_reversion | sector_gate | sector10 | +0.0 | 373 | not_helping (20) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2022 | ibs_mean_reversion | time_stop | hold15 | -0.1 | 373 | not_helping (20) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2022 | macd_momentum | breadth_gate | breadth40 | -4.0 | 33875 | not_helping (20) | not_helping (125) | inconclusive (110) | not_helping (125) |
| 2022 | macd_momentum | stop_width | stop5 | +4.2 | 35047 | not_helping (20) | not_helping (174) | inconclusive (110) | not_helping (174) |
| 2022 | macd_momentum | time_stop | hold30 | -1.5 | 33067 | not_helping (20) | not_helping (125) | inconclusive (110) | not_helping (125) |
| 2022 | macd_momentum | vix_gate | below30 | +0.0 | 32916 | not_helping (20) | not_helping (125) | inconclusive (110) | not_helping (125) |
| 2022 | moving_average_crossover | stop_width | stop5 | +8.0 | 55261 | not_helping (20) | not_helping (137) | inconclusive (110) | not_helping (137) |
| 2022 | moving_average_crossover | time_stop | hold30 | -0.8 | 49368 | not_helping (20) | not_helping (132) | inconclusive (110) | not_helping (132) |
| 2022 | rsi2_pullback | stop_width | stop15 | +1.3 | 722 | not_helping (20) | not_helping (122) | inconclusive (110) | not_helping (122) |
| 2022 | rsi2_pullback | time_stop | hold15 | +1.3 | 722 | not_helping (20) | not_helping (122) | inconclusive (110) | not_helping (122) |
| 2022 | rsi_mean_reversion | stop_width | stop5 | -1.8 | 6876 | not_helping (20) | not_helping (129) | inconclusive (110) | not_helping (129) |
| 2022 | rsi_mean_reversion | time_stop | hold30 | -1.1 | 6682 | not_helping (20) | not_helping (144) | inconclusive (110) | not_helping (144) |
| 2022 | trend_pullback | earnings_blackout | off | -0.9 | 13414 | not_helping (20) | not_helping (159) | not_helping (159) | not_helping (159) |
| 2022 | trend_pullback | market_gate | off | -12.8 | 23857 | not_helping (20) | not_helping (153) | not_helping (108) | not_helping (153) |
| 2022 | trend_pullback | stop_width | stop5 | -0.5 | 13451 | not_helping (20) | not_helping (131) | inconclusive (110) | not_helping (131) |
| 2022 | trend_pullback | time_stop | hold30 | -1.6 | 13171 | not_helping (20) | not_helping (134) | inconclusive (110) | not_helping (134) |
| 2022 | trend_pullback | vix_gate | below30 | +0.9 | 12998 | not_helping (20) | not_helping (150) | inconclusive (110) | not_helping (150) |
| 2024 | bollinger_mean_reversion | breadth_gate | breadth50 | -2.0 | 2884 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2024 | bollinger_mean_reversion | earnings_blackout | off | -3.4 | 2951 | not_helping (20) | not_helping (122) | not_helping (90) | not_helping (122) |
| 2024 | bollinger_mean_reversion | stop_width | stop5 | +7.2 | 2914 | working (20) | not_helping (123) | inconclusive (110) | not_helping (123) |
| 2024 | bollinger_mean_reversion | time_stop | hold30 | -2.4 | 2872 | not_helping (20) | not_helping (122) | inconclusive (110) | not_helping (122) |
| 2024 | channel_breakout | stop_width | stop5 | +4.2 | 6050 | not_helping (20) | not_helping (124) | not_helping (49) | not_helping (124) |
| 2024 | channel_breakout | time_stop | hold30 | -1.9 | 5981 | not_helping (20) | not_helping (122) | inconclusive (110) | not_helping (122) |
| 2024 | ensemble | stop_width | stop5 | +9.1 | 18313 | not_helping (20) | not_helping (174) | inconclusive (110) | not_helping (174) |
| 2024 | ensemble | time_stop | hold30 | -0.5 | 16901 | not_helping (20) | not_helping (163) | inconclusive (110) | not_helping (163) |
| 2024 | ibs_mean_reversion | earnings_blackout | off | +0.0 | 179 | working (24) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2024 | ibs_mean_reversion | sector_gate | sector10 | +0.0 | 179 | working (24) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2024 | ibs_mean_reversion | time_stop | hold15 | -0.4 | 179 | working (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2024 | macd_momentum | stop_width | stop5 | +2.8 | 22759 | not_helping (20) | not_helping (123) | not_helping (114) | not_helping (123) |
| 2024 | macd_momentum | vix_gate | below30 | +0.0 | 21437 | not_helping (20) | not_helping (136) | inconclusive (110) | not_helping (136) |
| 2024 | moving_average_crossover | breadth_gate | breadth50 | +0.6 | 31124 | not_helping (20) | not_helping (169) | inconclusive (110) | not_helping (169) |
| 2024 | moving_average_crossover | market_gate | sma10 | +1.8 | 38022 | not_helping (20) | not_helping (164) | not_helping (164) | not_helping (164) |
| 2024 | moving_average_crossover | stop_width | stop5 | +8.2 | 34974 | working (20) | not_helping (187) | inconclusive (110) | not_helping (187) |
| 2024 | moving_average_crossover | time_stop | hold30 | -0.6 | 31437 | not_helping (20) | not_helping (203) | inconclusive (110) | not_helping (203) |
| 2024 | rsi2_pullback | earnings_blackout | off | +0.7 | 601 | working (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2024 | rsi2_pullback | stop_width | stop15 | -0.4 | 541 | working (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2024 | rsi2_pullback | time_stop | hold15 | +1.6 | 541 | working (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2024 | rsi_mean_reversion | earnings_blackout | off | -1.1 | 3945 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |
| 2024 | rsi_mean_reversion | stop_width | stop5 | -1.1 | 3943 | not_helping (20) | not_helping (121) | inconclusive (110) | not_helping (121) |
| 2024 | rsi_mean_reversion | time_stop | hold30 | -1.0 | 3830 | not_helping (20) | not_helping (123) | inconclusive (110) | not_helping (123) |
| 2024 | trend_pullback | stop_width | stop5 | +0.6 | 9671 | not_helping (20) | not_helping (124) | not_helping (124) | not_helping (124) |
| 2024 | trend_pullback | time_stop | hold30 | -1.1 | 9464 | not_helping (20) | not_helping (156) | not_helping (56) | not_helping (156) |
| 2024 | trend_pullback | vix_gate | below30 | -0.7 | 9311 | not_helping (20) | not_helping (120) | inconclusive (110) | not_helping (120) |

</details>

## Experiment 3: deflating the adoptions

Every adoption the engine's rule makes on today's full-history point-in-time replay (gate and exit templates, live setups), with the multiplicity hurdle on the paired gain (candidate minus live per replayed decision, SPY-adjusted except market-timing gates) counting every candidate tried for the strategy across templates: entry-date clustered t >= 3, or deflated Sharpe > 0.95 of the per-decision difference with those trials. `Paired confirmed` = the full-history paired improvement e-process reaches 10 (alpha 0.10).

| Strategy | Template | Adopted | Baseline bps | Adopted bps | Min gain bps | Trades | Trials | Paired mean | Paired std | t | DSR | Hurdle | Paired confirmed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| rsi_mean_reversion | earnings_blackout | off | -16.7 | -15.6 | 0.3 | 24882 | 18 | -0.1 | 81 | -0.17 | 0.000 | **fail** | no |
| rsi_mean_reversion | stop_width | stop5 | -16.7 | -7.7 | 9.0 | 20740 | 18 | +2.0 | 311 | 0.86 | 0.006 | **fail** | no |
| rsi_mean_reversion | time_stop | hold30 | -16.7 | -13.7 | 1.6 | 24108 | 18 | +0.4 | 90 | 0.66 | 0.004 | **fail** | no |
| macd_momentum | vix_gate | below30 | -49.8 | -45.2 | 2.0 | 119421 | 18 | +4.3 | 143 | 2.08 | 0.000 | **fail** | yes |
| macd_momentum | stop_width | stop5 | -49.8 | -44.1 | 5.7 | 103322 | 18 | +9.3 | 339 | 7.19 | 0.000 | pass | yes |
| ensemble | earnings_blackout | off | -57.8 | -56.1 | 0.6 | 115182 | 18 | -0.4 | 83 | -1.48 | 0.000 | **fail** | no |
| bollinger_mean_reversion | breadth_gate | breadth60 | -18.0 | -2.6 | 2.1 | 9748 | 18 | +14.5 | 313 | 2.10 | 0.922 | **fail** | yes |
| bollinger_mean_reversion | stop_width | stop5 | -18.0 | -6.0 | 12.0 | 17222 | 18 | +5.4 | 296 | 2.08 | 0.006 | **fail** | yes |
| bollinger_mean_reversion | time_stop | hold30 | -18.0 | -15.2 | 1.9 | 19499 | 18 | +0.5 | 94 | 0.69 | 0.000 | **fail** | no |
| trend_pullback | vix_gate | below30 | -9.2 | -8.3 | 0.3 | 54089 | 18 | +0.9 | 82 | 0.96 | 0.199 | **fail** | yes |
| trend_pullback | stop_width | stop5 | -9.2 | +1.8 | 11.1 | 46117 | 18 | +4.8 | 299 | 3.16 | 0.642 | pass | yes |
| trend_pullback | time_stop | hold30 | -9.2 | -6.0 | 0.4 | 52993 | 18 | +1.4 | 150 | 1.85 | 0.113 | **fail** | no |
| ibs_mean_reversion | earnings_blackout | off | +13.0 | +13.0 | 0.0 | 2193 | 18 | +0.0 | 0 | n/a | n/a | **fail** | no |
| ibs_mean_reversion | sector_gate | sector10 | +13.0 | +13.0 | 0.0 | 2193 | 18 | +0.0 | 0 | n/a | n/a | **fail** | no |
| ibs_mean_reversion | time_stop | hold15 | +13.0 | +13.9 | 0.9 | 2176 | 18 | +0.0 | 10 | 0.11 | 0.013 | **fail** | no |

Blocked by the hurdle: 13 of 15; of those, confirmed improvements by the paired test: 4.

**Pre-registered default:** on unless more than half of the adoptions the hurdle blocks on today's replay are confirmed improvements by the full-history paired improvement e-process at alpha 0.10; none blocked = on. Result: `experiment_multiplicity_hurdle = True`.

## Readings

* technical: the 20-trade rule reverts 48.7% of null changes and 43.1% of good ones, and keeps 46.0% of bad ones; the e-process judge 100.0% / 100.0% / 0.0%; O'Brien-Fleming 16.2% / 1.5% / 39.6%.
* reversion: the 20-trade rule reverts 47.8% of null changes and 37.8% of good ones, and keeps 42.2% of bad ones; the e-process judge 99.8% / 83.4% / 0.0%; O'Brien-Fleming 10.2% / 0.6% / 33.2%.
* technical, one trade per date: the 20-trade rule reverts 50.1% of null changes and 40.0% of good ones, and keeps 42.9% of bad ones; the e-process judge 97.8% / 68.7% / 0.3%; O'Brien-Fleming 10.0% / 1.5% / 49.6%.
* reversion, one trade per date: the 20-trade rule reverts 48.1% of null changes and 33.6% of good ones, and keeps 39.5% of bad ones; the e-process judge 97.8% / 53.3% / 0.1%; O'Brien-Fleming 11.5% / 0.7% / 29.2%.

