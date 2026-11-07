# Position Sizing: Fixed Fraction, Volatility Target and Quarter Kelly, 2026-11-06

**Question:** expectancy becomes profit through size, and every scheduled paper BUY is sized by one rule: the
strategy's risk fraction of equity (`max_risk_per_trade`), times its bounded allocation weight, times the opt-in
volatility / VIX-breadth scale. Would sizing by the loss at the stop (volatility target), or by quarter Kelly on
the meta-label's calibrated probability and the strategy's empirical payoff ratio, raise the book's profit
without a worse drawdown, strategy by strategy?

**Decision rule (fixed before the run, in `scripts/run_position_sizing_backtest.py`):** every mode is capped at
the fixed-fraction notional (the live rule stays the ceiling). A candidate is tested per strategy: that
strategy's trades take the candidate's size while every other strategy keeps the fixed fraction. It is eligible
for the strategy only if, in every window (all, 2015+, 2020+):
* the book's total profit is not lower than the baseline's;
* the book's max drawdown is not worse; and
* the candidate changed the size of at least one of the strategy's trades (an arm that never fires is no evidence).

Among eligible candidates the one with the largest minimum total-profit gain is adopted; none eligible = keep
`fixed_fraction`.

Candidates: `volatility_target` at 0.25% / 0.5% / 1% of equity at risk per trade; `fractional_kelly` with a
simulated calibrated probability at 0.55 / 0.575 / 0.60 (the ends and the middle of a 55-60% band), the payoff
ratio from the strategy's book trades closed strictly before the entry day (at least 30, else the 0.5% volatility
target), quarter Kelly.

## Answer: no mode is adopted; every strategy stays on the fixed fraction

No candidate was eligible for any of the 9 replayable strategies. **`SIZED_STRATEGIES` in `seed.py` ships
empty**, so the one-time seed step applies nothing; the modes, the per-strategy parameter and the backtest are in
place so the question can be re-asked.

Whole book, every strategy on the arm at once (information only; the rule is per strategy):

| Arm | Trades resized, all / 2015+ / 2020+ | Exp. per unit notional, bps, all / 2015+ / 2020+ | Total return, all / 2015+ / 2020+ | Max DD, all / 2015+ / 2020+ |
|---|---|---|---|---|
| **fixed fraction (live)** | 0 / 0 / 0 | +0.27 / +18.75 / +19.26 | +1.5% / +62.2% / +36.3% | 56.6% / 17.5% / 17.5% |
| volatility target 0.25% | 1,312 / 561 / 335 | −0.34 / +18.23 / +18.86 | −2.2% / +55.8% / +33.1% | 55.5% / 18.0% / 18.0% |
| volatility target 0.5% | 0 / 0 / 0 | = fixed | = fixed | = fixed |
| volatility target 1% | 0 / 0 / 0 | = fixed | = fixed | = fixed |
| quarter Kelly, p = 0.55 | 337 / 133 / 0 | +0.52 / +18.75 / +19.26 | +3.0% / +62.2% / +36.3% | 55.9% / 17.5% / 17.5% |
| quarter Kelly, p = 0.575 | 213 / 0 / 0 | +0.43 / +18.75 / +19.26 | +2.3% / +62.2% / +36.3% | 56.2% / 17.5% / 17.5% |
| quarter Kelly, p = 0.60 | 826 / 446 / 285 | −0.52 / +19.15 / +19.86 | −3.2% / +57.5% / +34.2% | 56.8% / 18.4% / 18.4% |

The trade set is identical in every arm by construction (11,547 book trades; 5,240 since 2015; 3,274 since
2020): sizing changes size, never which trades the book takes. Every expectancy confidence interval includes
zero (fixed fraction, 2015+: [−3.1, +42.4] bps), as in the earlier sizing reports.

Per strategy, every candidate that changed at least one size (the others are identical to the baseline and fail
the "must fire" check):

| Strategy | Candidate | Trades resized, all / 2015+ / 2020+ | Book profit vs baseline, all / 2015+ / 2020+ | Book max DD vs baseline, all / 2015+ / 2020+ | Fails |
|---|---|---|---|---|---|
| IBS | volatility target 0.25% | 682 / 241 / 144 | −1.96 / −2.91 / −1.39 pp | −0.43 / +0.32 / +0.32 pp | profit everywhere, DD since 2015 |
| IBS | quarter Kelly 0.55 | 65 / 0 / 0 | **+1.76** / 0 / 0 pp | **−0.75** / 0 / 0 pp | never fires since 2015 |
| IBS | quarter Kelly 0.575 | 210 / 0 / 0 | +0.86 / 0 / 0 pp | −0.36 / 0 / 0 pp | never fires since 2015 |
| IBS | quarter Kelly 0.60 | 577 / 197 / 129 | −2.88 / −1.77 / −0.64 pp | +0.23 / +0.54 / +0.54 pp | profit and DD everywhere |
| RSI(2) | volatility target 0.25% | 630 / 320 / 191 | −1.76 / −3.57 / −1.81 pp | −0.61 / +0.17 / +0.17 pp | profit everywhere, DD since 2015 |
| RSI(2) | quarter Kelly 0.60 | 249 / 249 / 156 | −1.84 / −2.94 / −1.53 pp | 0 / +0.30 / +0.30 pp | profit everywhere, DD since 2015 |
| Channel Breakout | quarter Kelly 0.55 | 256 / 133 / 0 | −0.22 / −0.02 / 0 pp | +0.04 / 0 / 0 pp | profit, DD; never fires since 2020 |
| Channel Breakout | quarter Kelly 0.575 | 3 / 0 / 0 | −0.01 / 0 / 0 pp | +0.01 / 0 / 0 pp | never fires since 2015 |
| Bollinger | quarter Kelly 0.55 | 16 / 0 / 0 | −0.04 / 0 / 0 pp | +0.02 / 0 / 0 pp | never fires since 2015 |

MA Crossover, MACD, Ensemble, RSI(14) and Trend Pullback: no candidate ever changed a size.

## Why the modes do not help under the cap

**The cap turns every mode into a brake.** Capped at the fixed-fraction notional, a mode can only shrink an
entry, so it helps only when the trades it shrinks are the losing ones. The baseline here is the slot fraction
(5% of equity per position, as in `slot-allocation-20261106.md` and `regime-allocation-20261106.md`).

* **A volatility target binds only when `risk / stop` is below the slot.** The technical strategies replay with
  the 3% stop they send to the broker: 0.25% / 3% = 8.3% of equity, over the 5% slot, so the cap wins and nothing
  changes; 0.5% and 1% are further above it. Only RSI(2) and IBS, with their 10% catastrophe stop, bind at 0.25%
  (2.5% of equity, half size), and halving them lowers the book's profit in every window because those two
  strategies are net profitable (IBS +48 bps per trade in its best cell, RSI(2) +1 to +44). The drawdown falls a
  little over the whole history and rises a little since 2015 (the slot they give up was carrying the book's
  recovery), so the rule rejects it twice.
* **Quarter Kelly binds only when the payoff ratio is low.** With the strategy's empirical payoff ratio *b*,
  quarter Kelly is under the 5% slot when the full Kelly `p − (1 − p) / b` is under 20%: *b* below 1.29 at
  p = 0.55, below 1.0 at p = 0.60. The technical strategies carry a payoff ratio of 1.38-1.60 (3% stop, 6%
  target), so Kelly at the band wants 5.6%-8.8% of equity and is capped to the slot. The mean-reversion pair
  carry 0.71-0.72 with a 61% win rate: at p = 0.55 or 0.575 Kelly says "no edge" (the breakeven probability at
  b = 0.71 is 58.5%) and the mode falls back to the 0.5% volatility target, which equals the slot; at p = 0.60 it
  sizes them at 0.9%-1.2% of equity, a fifth of the slot, and the book loses the same way as under the 0.25%
  target. Channel Breakout and Bollinger shrink only in the stretches where their causal payoff ratio dipped under
  1.29, before 2015, and lose a little there.
* **The one gain is out of window.** IBS at p = 0.55 sized smaller in 65 pre-2015 trades (the early years when
  its causal payoff ratio was below 0.82, so the "no edge" fallback did not trigger) and that raised the book's
  all-history profit by 1.76 points with a lower drawdown. It never fired since 2015, so by the pre-registered
  rule it is no evidence for forward use.

Payoff ratios over the whole history (descriptive, not the causal input) and what quarter Kelly would size:

| Strategy | Book trades | Win rate | Payoff ratio | Quarter Kelly at p = 0.55 / 0.575 / 0.60 |
|---|---:|---:|---:|---|
| moving_average_crossover | 3,464 | 37.0% | 1.50 | 6.3% / 7.3% / 8.3% |
| macd_momentum | 2,250 | 40.4% | 1.50 | 6.3% / 7.3% / 8.3% |
| ensemble | 1,504 | 38.4% | 1.60 | 6.7% / 7.8% / 8.8% |
| trend_pullback | 1,273 | 43.8% | 1.39 | 5.6% / 6.7% / 7.8% |
| channel_breakout | 917 | 48.4% | 1.38 | 5.6% / 6.7% / 7.8% |
| ibs_mean_reversion | 682 | 61.0% | 0.71 | 0 / 0 / 0.9% |
| rsi2_pullback | 630 | 61.0% | 0.72 | 0 / 0 / 1.2% |
| rsi_mean_reversion | 537 | 41.2% | 1.51 | 6.3% / 7.4% / 8.4% |
| bollinger_mean_reversion | 290 | 39.3% | 1.46 | 6.1% / 7.1% / 8.2% |

**The live cap is tighter than the simulated one.** The paper risk rule sizes a scheduled BUY at
`max_risk_per_trade` = 1% of equity (the ledger rejects more), not the 5% slot of this replay. At 1% notional
the loss at a 3% stop is 0.03% of equity, so on the live book a volatility target would change a technical
strategy's size only below 0.03% risk per trade, and a 10% stop strategy's only below 0.1%. The modes as
specified are therefore a way to size *down* inside the fixed fraction, never up; letting them size up means
raising the fixed fraction (the risk rule) and letting the mode fill the room, which is a different experiment
with the ledger's exposure checks in play. This report does not recommend it; it records the constraint.

## What was built (nothing switched on)

* `app/services/position_sizing.py`: pure functions. `fixed_fraction` (the live rule), `volatility_target`
  (equity × risk ÷ stop distance, capped; no stop leg = fixed fraction), `fractional_kelly` (quarter Kelly from
  the calibrated probability and the payoff ratio, only when the strategy is in meta-label enforce mode, the
  signal carries a probability and at least 30 closed paper trades with both wins and losses exist; a
  non-positive Kelly and every missing input fall back to the volatility target; capped), `size_notional`
  (dispatch), `sizing_parameters` (validation of the row parameters), `payoff_ratio` / `strategy_payoff` (mean
  win ÷ mean loss of the strategy's closed scheduled round trips with known costs).
* **Strategy row parameters** `sizing_mode` (`fixed_fraction` / `volatility_target` / `fractional_kelly`) and
  `risk_per_trade` (0 < x ≤ 5%, default 0.5%), read by the autotrader's sizing step through the same governed
  overlay as every other parameter. Learning-sized exploration and news probes keep the exploration fraction;
  the whole-share rounding, the one-share floor and the ledger's exposure gate run after the mode, unchanged.
  The stop distance is the one the broker stop leg uses (the strategy's exit policy, else the risk-rule stop).
* **Every scheduled BUY records `features.sizing`** on its signal: requested and effective mode, status
  (`sized` / `capped` / `fallback_…`) with the reason, the stop distance, risk budget, probability, payoff ratio
  and closed-trade count, the fixed-fraction cap, the resulting notional and quantity, the allocation weight and
  scale, so attribution and the experiment engine can see how the entry was sized.
* **Seed step** `apply_sizing_modes` (`_enable_gate_once` pattern, governance action `sizing_mode_update`,
  source this report): applies `SIZED_STRATEGIES` once per row, never over a row that already has a
  `sizing_mode` or a previous update event, so an operator's later choice stands. It ships empty.
* `scripts/run_position_sizing_backtest.py`: the rule above over the live book, with a precomputed daily
  mark-to-market of every accepted trade so that 63 book arms price in seconds.
* `tests/test_position_sizing.py` (16): each mode's arithmetic and caps, the fallbacks, the parameter validation,
  the payoff lookup, the script's rule on synthetic rows and its causal sizing, the book pricing, three cycle
  tests with a fake ledger (fixed fraction, volatility target, Kelly in and out of enforce mode, exploration
  probes untouched) and the seed contract.

## Data and method

* **Data:** the 2026-11-06 cache of `market-gate-20261106.md` (45 paper symbols, 2000-01-01 → 2026-11-05), the
  VIX close history of `vix-gate-20261106.md`, the earnings cache of `gate-cost-20261106.md`.
* **Candidates:** every executable trade of the 9 replayable live strategies from `gate_replay.simulate` under the
  gates live today (SPY market gates, Channel Breakout's VIX < 20 gate, the 10-session earnings blackout):
  60,151 (`run_regime_allocation_backtest.candidates`, read-only).
* **Book:** the live caps (1 per symbol, 5 per strategy, 3 per sector, 20 total), slots in the live cycle order,
  the 10-session stop-out cooldown adopted in `slot-allocation-20261106.md` (`run_slot_allocation_backtest.allocate`,
  read-only): 11,547 trades.
* **Sizes:** fraction of equity per trade; the fixed fraction is the 5% slot. Stop distance from
  `gate_replay.barrier_for` (3% technical, 10% RSI(2) / IBS), the same geometry the replay exits on. The payoff
  ratio is cumulative over the strategy's book trades closed strictly before the entry day (causal); Kelly needs
  30 of them. Positions are marked to market daily; total return and max drawdown are of the summed weighted
  daily returns (`Book.curve`).
* **Expectancy per unit of notional** is Σ(weight × net return) / Σ weight with a month-clustered bootstrap CI,
  as in `regime-allocation-20261106.md`.
* Runtime about a minute (`scripts/run_position_sizing_backtest.py --data-dir <dir> --vix-csv <csv> --output <json>`).
* **Caveats:** the simulated probability is a constant, not a per-trade calibrated score, so the Kelly arms test
  the payoff side of Kelly only; a real calibrated probability that separates winners from losers is exactly
  what the mode is for and is not available in replay. Survivorship in the megacap list flatters every arm
  about equally. Live stops adopted later by the exit autopsy (`exit_policy_update`) are not in the replay
  barrier.

## Re-asking the question

Two triggers, both forward:

* A strategy reaches meta-label **enforce** mode with 30 or more closed scheduled round trips. Its live Kelly
  path then has a real calibrated probability per signal; set `sizing_mode: fractional_kelly` on the row as a
  pre-registered experiment and judge it on expectancy, drawdown and trade count together, the way the
  experiment engine judges gates. The `features.sizing` record on every BUY is the evidence.
* The operator decides to raise the fixed fraction. Re-run this script with the slot set to the new fraction
  (`SLOT`) before changing the risk rule; under a looser cap the volatility target and Kelly can size up and the
  comparison is a different one.
