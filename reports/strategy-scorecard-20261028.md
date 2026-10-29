# Strategy Scorecard: Win Rate vs Expectancy, 2026-10-28

**Question:** does any strategy we have, or either of the two published high-hit-rate candidates, reach the
owner's goal of **winning 8 trades out of 10 while still making money after costs**?

**Short answer: no.** Across 28 variants, 45 symbols and three windows, **no pooled strategy reaches an 80% net
win rate.** The best pooled win rate in any window is 64% (RSI(2) pullback). The only per-symbol cells that pass
the gate are 2 of about 3,000 (GOOGL, post-2020, RSI(2) < 5, 27–28 trades). Their deflated Sharpe is 0.08–0.10
after 1,215+ trials, which is what selection noise looks like. RSI(2) pullback is the only family whose expectancy
CI is above zero on the full history, and that edge weakens after 2015.

Method summary: daily yfinance OHLCV (split/dividend-adjusted, `auto_adjust=True`), from each symbol's first bar
(1993 for SPY and most megacaps; XLRE 2015, XLC 2018) to 2026-10-27. Every run uses the shared backtester
(`app/services/backtester.py`): decisions on completed bars, **next-open fills**, intrabar stop-first exits and
volatility-scaled costs, with fees of 1 bp plus a 5 bp slippage floor plus a volatility and impact component on
each side. A trade is a win only if its **net** return is > 0. The CLI writes the full output (CSV, JSON and
Markdown) to `output/strategy_scorecard/` (git-ignored).

Reproduce:
```
PYTHONPATH=artifacts/api-server/backend .venv/bin/python artifacts/api-server/backend/scripts/run_strategy_scorecard.py \
  --data-dir output/scorecard_data --output-dir output/strategy_scorecard --end 2026-10-28 \
  --include-model-predictive --workers 9      # add --download on first run; ~18 min on 10 cores
```

## 1. Gate and statistics

* **Goal gate (`meets_goal`):** net win rate ≥ 80% AND expectancy > 0 AND the lower bound of the 95% bootstrap CI
  on expectancy > 0 AND ≥ 100 pooled trades (≥ 20 for a single symbol).
  **Robust** additionally requires the win-rate CI lower bound ≥ 80% and profit factor ≥ 1.3.
* **CIs:** cluster bootstrap with whole calendar months resampled (2,000 draws, seed 7). Trades on 40 symbols
  during the same sell-off are one piece of evidence, not 40. A Wilson interval is also stored for the win rate.
* **Deflated Sharpe (DSR):** computed on the equal-weight daily portfolio return of each variant. Every
  variant counts as a trial: 27 trials in `all`/`post2015` and 28 in `post2020`. For per-symbol cells it is
  variants × symbols (≈1,200 trials), because picking "the symbols where it works" is also a selection.
* **Baselines:** buy-and-hold of the same symbol from the first bar of the window, using the same cost model,
  and an **exposure-matched** buy-and-hold (time-in-market × B&H return). Mean B&H in the `all` window is dominated
  by NVDA, AMD and similar names (+27,000% average), so read the "share of symbols beating exposure-matched B&H" column
  rather than the mean excess.

## 2. Ranked verdict (pooled over 45 symbols; top rows by goal, then expectancy CI lower bound)

| Window | # | Variant | Trades | Win % [95% CI] | Avg win / loss % | Payoff | Break-even win % | Exp. bps [95% CI] | PF | DSR | Goal |
|---|---|---|---|---|---|---|---|---|---|---|---|
| all | 1 | rsi2_pullback rsi<5, exit close>SMA5, t=10 | 4,754 | 64.3 [62.0, 66.6] | 2.03 / 2.73 | 0.74 | 57.4 | **+32.8 [13.3, 51.6]** | 1.34 | 0.48 | no |
| all | 2 | rsi2_pullback rsi<10, SMA5, t=10 | 9,343 | 63.6 [61.8, 65.4] | 1.95 / 2.75 | 0.71 | 58.5 | +23.9 [9.8, 38.1] | 1.24 | 0.46 | no |
| all | 3 | rsi2_pullback rsi<5, SMA5, t=5 | 4,911 | 62.5 [60.0, 64.9] | 2.04 / 2.68 | 0.76 | 56.8 | +26.5 [8.1, 45.1] | 1.26 | 0.33 | no |
| all | 6 | ibs_mean_reversion ibs<0.1, exit IBS>0.8, t=10 | 15,766 | 60.0 [58.6, 61.6] | 1.91 / 2.54 | 0.75 | 57.0 | +13.2 [3.5, 23.8] | 1.13 | 0.21 | no |
| post2015 | 1 | bollinger_mean_reversion (3% stop / 6% target) | 2,857 | 42.8 [38.5, 47.6] | 5.21 / 3.21 | 1.62 | 38.2 | +39.4 [0.6, 81.5] | 1.21 | 0.47 | no |
| post2015 | 2 | ibs_mean_reversion ibs<0.1, IBS exit, t=10 | 6,998 | 59.8 [57.4, 62.1] | 1.81 / 2.38 | 0.76 | 56.7 | +12.9 [−3.2, 28.1] | 1.13 | 0.33 | no |
| post2015 | 7 | rsi2_pullback rsi<15, SMA5, t=10 | 5,854 | 62.0 [59.4, 64.6] | 1.75 / 2.55 | 0.68 | 59.4 | +11.5 [−8.5, 31.3] | 1.12 | 0.21 | no |
| post2020 | 1 | ibs_mean_reversion ibs<0.1, IBS exit, t=5 | 4,391 | 59.6 [56.2, 62.8] | 1.88 / 2.32 | 0.81 | 55.2 | +18.6 [−3.1, 36.8] | 1.20 | 0.64 | no |
| post2020 | 12 | rsi2_pullback rsi<15, SMA5, t=10 | 3,428 | 61.4 [58.0, 64.8] | 1.89 / 2.74 | 0.69 | 59.2 | +9.9 [−20.4, 38.6] | 1.09 | 0.23 | no |
| post2020 | — | model_predictive_long (SPY/QQQ/AAPL/MSFT only) | 656 | 42.1 [36.6, 47.9] | 5.05 / 3.21 | 1.58 | 38.8 | +26.8 [−22.1, 76.1] | 1.14 | 0.39 | no |

Best variant per existing strategy family (pooled, expectancy bps [CI], win %):

| Family | all | post2015 | post2020 |
|---|---|---|---|
| moving_average_crossover | −10.6 [−22.3, 1.2], 37% | +4.8 [−15.8, 26.2], 40% | −2.6 [−31.8, 24.1], 38% |
| macd_momentum | **−15.6 [−30.0, −1.0]**, 37% | +14.3 [−10.8, 40.6], 40% | +12.6 [−20.2, 42.4], 39% |
| rsi_mean_reversion | +4.1 [−14.7, 24.2], 39% | +31.2 [−6.4, 69.1], 42% | +25.4 [−22.3, 76.9], 41% |
| bollinger_mean_reversion | +9.3 [−12.1, 31.9], 39% | +39.4 [0.6, 81.5], 43% | +31.8 [−17.4, 85.0], 41% |
| channel_breakout | −3.8 [−18.6, 12.3], 40% | +16.1 [−8.7, 40.3], 42% | +18.7 [−14.1, 48.4], 41% |
| trend_pullback | +3.7 [−12.6, 20.8], 40% | +9.3 [−17.0, 35.9], 41% | −0.5 [−34.7, 33.0], 39% |
| ensemble | −6.2 [−19.8, 7.4], 37% | +12.6 [−12.7, 39.1], 40% | +6.7 [−27.1, 37.7], 38% |

The existing strategies all use the default 3% stop / 6% target / 20-day bracket. That geometry has a ~38%
break-even win rate, so a 37–43% win rate is what it produces. **Structurally, they cannot reach 80%.**

## 3. Verdict by window

* **All history (1993–2026):** none passes. RSI(2) pullback is the only family with a clearly positive expectancy:
  five of its variants have a CI lower bound > 0, best +33 bps/trade. IBS (ibs<0.1) is also marginally positive.
  Win rates are 60–64%, not 80%. The average loss (2.5–2.7%) is **larger** than the average win (1.9–2.0%), which is
  the classic shape of a high-hit-rate reversion strategy.
* **Post-2015:** none passes. No RSI(2)/IBS variant keeps a CI lower bound > 0, which fits the documented decay
  after the 2008–09 publications. Only bollinger_mean_reversion squeaks above zero (CI low +0.6 bps, DSR 0.47), at
  a 43% win rate.
* **Post-2020:** none passes. IBS ibs<0.1 has the best point estimate (+18.6 bps, DSR 0.64), but its CI includes 0.
  model_predictive_long has a 42% win rate and a CI that includes 0. It also loses to exposure-matched buy-and-hold on
  all 4 symbols.
* **Against passive baselines:** no variant beats exposure-matched buy-and-hold on a majority of symbols in any
  window. The best is IBS post-2020 at 40% of symbols. Every pooled portfolio return is far below the
  buy-and-hold return of the same names.

### Where it "works" (per symbol, ≥ 20 trades)

* Cells meeting the gate: **GOOGL post-2020, rsi2 rsi<5 SMA-exit** (t=5: 28 trades, 85.7% win, +132 bps
  [67, 198]; t=10: 27 trades, 85.2%, +129 bps). DSR is 0.08–0.10 over ~1,200 trials. **Do not trade this.** It is the
  best of 1,200 lottery tickets on 28 trades, and the same variant gives 77–78% on GOOGL post-2015.
* Near misses (75–80% win): NVDA, XLY, SMH and XLC post-2020, V on full history (76.6%, +88 bps [45, 131], 145 trades).
  The candidates for the 80% goal sit in individual high-momentum megacaps over short windows, which is where survivorship
  bias in today's universe is strongest.
* Index ETFs (SPY/QQQ/IWM/DIA pooled, RSI(2) rsi<10 SMA-exit t=10): 67% win, +23 bps on 906 trades (all),
  66% / +14 bps (post-2015), 68% / +23 bps (post-2020). SPY alone: 71% win, +36 bps [14, 57] on 267 trades over
  the full history, but the CI includes 0 after 2015. Sector ETFs are weaker (62–65% win, 1–11 bps).

## 4. What did not get scored, and caveats

* **model_predictive_long:** it is causal. At every bar it refits on the completed-bar prefix, and labels that
  need future closes are NaN and dropped. It costs ~0.5 s per bar, though, so the full universe and history would
  take about 30 CPU-hours. It was scored on SPY, QQQ, AAPL and MSFT, with data from 2018 and trades from 2020 on,
  and counted as trial #28 in post-2020.
* **Execution vs the books:** Connors and Alvarez enter and exit at the signal-day close. We decide on the close
  and fill at the next open, which is how this bot trades. Part of the published edge lives in that close-to-open
  gap, so these numbers are lower than the books by design.
* **Survivorship bias:** the universe is today's 45 megacaps and ETFs. The full-history numbers for names such as
  NVDA, AMD and TSLA are flattered.
* **Sizing:** the backtester sizes from `risk_per_trade / stop_loss_pct`. For RSI(2)/IBS that means a 10%
  catastrophe stop, i.e. about 10% notional per trade, so their total returns are small. Win rate and expectancy are
  per-trade and do not depend on size.
* **Trade overlap:** pooled trades overlap in time, which is why the CIs are month-clustered. An iid trade
  bootstrap would give narrower, over-optimistic intervals.

## 5. Recommendations

1. **Do not promote any strategy on the 80% claim.** Nothing reaches it honestly. Re-state the operating goal as
   "positive net expectancy with CI lower bound > 0 and an 80% win rate *if* filtering achieves it." The win rate
   is an output to monitor. It is not a target to tune exits toward.
2. **Exploration (paper, tagged, no promotion credit): `rsi2_pullback` rsi<10, close>SMA5 exit, 10-day time stop,
   10% catastrophe stop, on SPY/QQQ/IWM/DIA.** It is the only family whose edge survives honest costs and month
   clustering on 30 years, and it is the natural **primary model for the meta-labeling selector** another agent is
   building. Meta-labeling is the legitimate way to move 64–70% toward 80%, by not taking the worst setups. Before
   any paper order, wire `BaseStrategy.exit_signal` into paper execution, because today only the backtester
   honours indicator exits. Both new slugs are in `RESEARCH_ONLY_STRATEGY_SLUGS` and are not seeded.
3. **Second candidate: `ibs_mean_reversion` ibs<0.1, IBS>0.8 exit**, as an additional primary signal and a
   meta-model feature (it had the best post-2020 point estimate and DSR). It is cost-sensitive (1–3 day holds), so
   re-check it against the observed paper fill costs.
4. **Keep the existing seven where they are.** macd_momentum is significantly negative over the full history
   (CI entirely < 0), and moving_average_crossover, the one `paper_trading_active` strategy, is negative or flat in
   every window. Consider demoting the crossover from active to candidate.
5. **Use per-symbol "it works on GOOGL" results only as meta-model features, never as symbol whitelists.** Their
   deflated Sharpe is ≈ 0.1.
6. Re-run this scorecard (same CLI, same trial accounting) after the meta-label selector lands, scoring
   "RSI(2) + meta-filter" walk-forward. That is the first configuration with a plausible path to the 80% target.
