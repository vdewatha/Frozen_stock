# Meta-label self-learning: 2026-11-02

Branch `feat/meta-label-learning`. Turns the meta-label BUY filter into a component that keeps learning from the
bot's own paper trades. Before this change it could never take a trade and was never retrained.

## What was wrong

1. **The threshold was always null.** The rule asked for an 80% validation win rate. No year reached it out of
   sample (`meta-label-20261028.md`), so every shadow decision was `skip`. The per-strategy shadow → enforce gate
   could not build evidence, and the filter never affected a trade.
2. **It could not score the strategies that trade.** `rsi2_pullback` and `ibs_mean_reversion` were not
   replayable. Their BUYs came back as `unsupported_strategy`, yet they are the only families with positive
   expectancy in the scorecard.
3. **It never learned.** It was trained once, offline, on simulated labels. No job retrained it, and paper outcomes
   were not used.
4. **Production probably had no model.** The artifact is git-ignored. Without it, shadow mode switches itself off
   (fail open).

## What changed

* **RSI(2) and IBS are replayed** at their seeded defaults (`meta_label_primary.py`). Their labels follow the paper
  exit policy: 10% stop, no target, 10 sessions, and the indicator exit filled at the next open, in the same order
  as the backtester. The parity test now covers both, and a new test checks the exits against
  `exit_signal`. `buy_consensus` still counts the original seven strategies, so the meaning of that feature does
  not change.
* **The threshold maximises validation expectancy subject to a 60% win-rate floor** (`meta_label_min_win_rate`),
  at least 30 trades and expectancy > 0. Win rate is reported next to expectancy everywhere it was reported before.
* **The bot's own trades are used for training** (`merge_paper_outcomes`). Each closed scheduled or exploration
  round trip is mapped to its decision bar, which is the last completed session before entry. It replaces the
  simulated label of that decision with the realised net result. It carries 3x weight
  (`meta_label_paper_outcome_weight`) and uses the signal's own confidence and probability.
* **It retrains itself** (`meta_label_learning.py` and `meta_label_retrain_job`, daily at 20:15 New York time, on the
  learning queue, and never during the session). A retrain is due when the artifact is missing, older than 7 days,
  or ≥ 10 paper trades have closed since the last training. If a candidate has no feasible threshold while the
  incumbent has one, the incumbent stays. The previous artifact is kept as `.prev`. Every run writes an audit log
  entry.
* **Unchanged:** enforcement is still decided per strategy by `meta_label_modes` from live shadow evidence:
  ≥ 30 trades, precision ≥ 0.55, recall ≥ 0.5 and lift ≥ 0.05 to enforce, and demotion on precision < 0.5. Nothing
  here places, blocks or sizes an order.

## Out-of-sample results (real data, 45 symbols, 2009–2026, yearly purged walk-forward)

Executable (non-overlapping) trades, net of costs, with month-cluster bootstrap 95% CIs on expectancy.

| Win-rate floor | Window | Trades | Win % | Exp. bps [95% CI] | PF |
|---|---|---:|---:|---|---:|
| take every BUY | 2014+ | 83,722 | 53.7 | −3.3 [−14.5, 7.7] | 0.98 |
| 0.55 | 2014+ | 25,849 | 53.7 | −5.4 [−24.3, 13.6] | 0.96 |
| **0.60 (new default)** | 2014+ | 9,322 | 55.4 | **+2.2** [−25.4, 33.4] | 1.02 |
| 0.65 | 2014+ | 2,397 | 57.4 | +26.0 [−46.5, 104.7] | 1.18 |
| 0.70–0.80 | 2014+ | 135 | 75.6 | +145.6 (1 fold only) | 3.35 |
| take every BUY | 2020+ | 48,678 | 52.8 | −6.7 [−21.7, 7.4] | 0.95 |
| **0.60** | 2020+ | 5,238 | 58.1 | **+24.4** [−18.5, 67.6] | 1.20 |

I tried 55% first, as originally proposed. It did not beat taking every signal, so the default became 60%. **That
choice was read off these test years.** Treat the 60% row as optimistic. The 0.70+ rows come from a single fold
(2026) with 135 trades. That is not evidence.

By strategy, at 0.60:

| Strategy | Window | Take all: trades / win % / exp. bps | Filter: trades / win % / exp. bps |
|---|---|---|---|
| RSI(2) | 2014+ | 4,516 / 63.3 / +12.4 | 820 / 61.5 / **−19.3** |
| IBS | 2014+ | 16,782 / 59.9 / +12.1 | 3,145 / 60.7 / +23.3 |
| RSI(2)+IBS on index ETFs (paper scope) | 2020+ | 1,086 / 62.6 / +17.6 | 234 / 68.8 / +43.8 |
| IBS | 2020+ | 9,314 / 59.4 / +13.4 | 1,903 / 62.0 / +47.5 [−0.1, 97.6] |

**Verdict:** the filter is not proven. It helps IBS and, since 2020, the index-ETF reversion book. It hurts RSI(2)
over the full window. Every CI includes zero. This is why enforcement stays per strategy and depends on live shadow
evidence: the paper trades will decide, strategy by strategy, whether the filter is allowed to veto. Nothing
reaches 80% wins with positive expectancy on a meaningful sample.

The final model trained on all data through 2026-10-30 picked a threshold of 0.81. On its validation year it took
315 trades at 71.1% wins and +128 bps per trade. Those numbers are in-sample for the threshold choice, not a
forecast.

## Operations

* The first evening after deploy, the job finds no artifact and trains one. It downloads about 45 symbols from
  yfinance into `stock_training_artifacts/meta_label/history`. Locally, building the events and fitting took under a
  minute on 10 cores. Expect a few minutes on the 2 vCPU host.
* If the host's filesystem does not survive a publish, the artifact is rebuilt that evening. Shadow mode fails open
  until then.
* To retrain by hand: `PYTHONPATH=. python scripts/train_meta_label.py --data-dir <dir> --download`. The default
  floor is now 0.60.
