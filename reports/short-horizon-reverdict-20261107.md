# Short-horizon sleeve re-verdict: 2026-11-07

Step 4 of the build plan in [Retail bot edge feasibility](Retail%20bot%20edge%20feasibility.md). Branch `feat/short-horizon-reverdict`. Paper only.

**Every number here is in-sample.** Every post-2015 replay of IBS and RSI(2) has been looked at before (the survivorship, sector-reversal and yardstick reports), and RSI(2)'s scope was chosen on the same windows. The pre-registered rule binds what happens next, not what is already known.

**Outcome: nothing passes at any cost.** IBS and RSI(2) on the four index ETFs fail the sleeve rule at 10 bps a round trip, at 25 bps and at the published model. A one-time seed step moves both rows to `shadow`, so they keep recording signals but place no orders. RSI(2)'s 29-stock leg, run once on the 707-name point-in-time universe, fails too, so it stays in shadow. The close-to-open variant fails every check and is dropped for good, with no strategy row. The bot's measured round-trip cost does not enter the verdict: no rule passed at 10 bps, which is the only branch that reads it.

The closest call is IBS at 10 bps. Its gross of +46.4 bps a trade clears the 30 bps floor, and a 10% slice raises the live arm's Sharpe in both windows with less than 2 extra points of drawdown. It fails on Sharpe alone: the sleeve earns a net Sharpe of 0.67 over T-bills, which is 0.33 after the 50% haircut, against a bar of 0.5. The report's trade-level reading, the date-clustered t divided by the square root of the years, gives 0.88, or 0.44 after the haircut. That fails as well, so the verdict does not depend on which reading is used.

## The verdict table

The window is 2015-01-01 to 2026-11-06. "Need" is max(30 bps, 3 × cost). Check (ii) is the sleeve's annualised net Sharpe over T-bills × 0.5, which must be at least 0.5; the t-implied Sharpe in brackets is shown for comparison and does not decide anything. Check (iii) is the step-3 marginal rule on the live arm (20% SPY / 80% T-bills, rebalanced monthly), with the slice taken from the T-bill leg at the allocation the sleeve would take: 10% at 10 bps, 20% at 25 bps and at the model. Each cell shows the arm's Sharpe and maximum drawdown, then the same with the slice. It passes only if Sharpe rises and drawdown grows by at most 2 points, in both windows.

| Rule | Cost | Cost bps | Gross bps (need) | (i) | Net bps [95% CI] | (ii) Sharpe × 0.5 (t-implied) | (ii) | (iii) 2008-07+ | (iii) 2015+ | (iii) | Passes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| IBS, 4 ETFs | 10 bps | 10.0 | +46.4 (30) | pass | +36.4 [+12, +58] | 0.33 (0.88) | fail | 0.600→0.625, 11.1%→12.9% | 0.683→0.726, 7.1%→8.8% | pass | no |
| IBS, 4 ETFs | 25 bps | 25.0 | +46.4 (75) | fail | +21.4 [−3, +43] | 0.19 (0.52) | fail | 0.600→0.524, 11.1%→15.1% | 0.683→0.626, 7.1%→10.6% | fail | no |
| IBS, 4 ETFs | model | 24.3 | +46.4 (73) | fail | +22.1 [−2, +44] | 0.19 (0.54) | fail | 0.600→0.521, 11.1%→15.7% | 0.683→0.632, 7.1%→10.7% | fail | no |
| RSI(2), 4 ETFs | 10 bps | 10.0 | +29.9 (30) | fail | +19.9 [−10, +49] | 0.12 (0.43) | fail | 0.600→0.629, 11.1%→11.1% | 0.683→0.682, 7.1%→7.4% | fail | no |
| RSI(2), 4 ETFs | 25 bps | 25.0 | +29.9 (75) | fail | +4.9 [−25, +34] | 0.02 (0.11) | fail | 0.600→0.612, 11.1%→11.1% | 0.683→0.631, 7.1%→7.6% | fail | no |
| RSI(2), 4 ETFs | model | 20.8 | +29.9 (62) | fail | +9.1 [−21, +38] | 0.04 (0.20) | fail | 0.600→0.624, 11.1%→11.1% | 0.683→0.643, 7.1%→7.6% | fail | no |
| RSI(2) stock leg, point in time | 10 bps | 10.0 | +26.5 (30) | fail | +7.0 [−10, +22] | 0.06 (0.28) | fail | 0.600→0.544, 11.1%→11.8% | 0.683→0.594, 7.1%→8.0% | fail | no |
| RSI(2) stock leg, point in time | 25 bps | 25.0 | +26.5 (75) | fail | −8.1 [−25, +7] | −0.18 (−0.32) | fail | 0.600→0.241, 11.1%→12.7% | 0.683→0.247, 7.1%→8.9% | fail | no |
| RSI(2) stock leg, point in time | model | 30.2 | +26.5 (89) | fail | −13.3 [−30, +2] | −0.26 (−0.53) | fail | 0.600→0.158, 11.1%→12.8% | 0.683→0.156, 7.1%→8.9% | fail | no |
| Close-to-open variant | 10 bps | 10.0 | +8.2 (30) | fail | −1.8 [−14, +10] | −0.05 (−0.07) | fail | 0.600→0.569, 11.1%→10.9% | 0.683→0.638, 7.1%→7.0% | fail | no; post-2021 gross +3.9 < 30 |
| Close-to-open variant | 25 bps | 25.0 | +8.2 (75) | fail | −16.8 [−29, −5] | −0.36 (−0.69) | fail | 0.600→0.391, 11.1%→11.2% | 0.683→0.440, 7.1%→7.2% | fail | no; post-2021 gross +3.9 < 75 |
| Close-to-open variant | model | 22.6 | +8.2 (68) | fail | −14.4 [−27, −4] | −0.31 (−0.60) | fail | 0.600→0.407, 11.1%→11.6% | 0.683→0.463, 7.1%→7.4% | fail | no; post-2021 gross +3.9 < 68 |

Net CIs are month-clustered bootstraps. The stock leg's gross and its required gross read every executable trade of the rule (24,023 since 2015, model cost 29.8 bps). Its net, Sharpe and marginal columns read the five-slot sleeve's 2,725 trades, whose gross is +16.9 bps and whose model cost is 30.2 bps.

| Rule | Passes at 10 bps / 25 bps / model | Outcome |
|---|---|---|
| IBS, 4 ETFs | fail / fail / fail | **shadow** (one-time seed step) |
| RSI(2), 4 ETFs | fail / fail / fail | **shadow** (one-time seed step; the stock leg was already `shadow_symbols`) |
| RSI(2) stock leg, point in time | fail / fail / fail | **stays in shadow** |
| Close-to-open variant | fail / fail / fail | **dropped for good**, no strategy row |

## Per-trade detail and the 2021 split

| Rule | Trades 2015+ | Per year | Hold (sessions) | Win (gross) | Gross 2015+ [CI] | Gross 2015-20 (trades) | Gross 2021+ (trades) | Model cost | Net at model 2015-20 / 2021+ |
|---|---|---|---|---|---|---|---|---|---|
| IBS, 4 ETFs | 883 | 74.6 | 3.1 | 69.2% | +46.4 [+22, +68] | +47.0 (433) | +45.8 (450) | 24.3 | +22.2 / +22.0 |
| RSI(2), 4 ETFs | 252 | 21.4 | 3.5 | 70.2% | +29.9 [+0, +59] | −9.0 (107) | +58.7 (145) | 20.8 | −28.9 / +37.2 |
| RSI(2) stock leg (5-slot sleeve) | 2,725 | 230.4 | 3.9 | 62.8% | +16.9 [−0, +32] | +26.8 (1,354) | +7.2 (1,371) | 30.2 | −1.9 / −24.5 |
| Close-to-open variant | 881 | 74.4 | 1.0 | 60.2% | +8.2 [−4, +20] | +12.7 (431) | +3.9 (450) | 22.6 | −9.8 / −18.9 |

IBS's per-trade figures match the yardstick report (883 trades, +46.4 gross, +22.1 net at the model). RSI(2) on the ETFs alone earned −9 bps gross a trade over 2015-2020 and +59 bps since 2021, so its record rests on the last five years. The variant's gross fell from +12.7 bps before 2021 to +3.9 bps after, the same decay the NY Fed reports for the overnight drift.

## The sleeves themselves, and the 50/50 arm (shown, not decisive)

| Rule | Cost | Sleeve CAGR / Sharpe / max DD, 2015+ | 50/50 arm + slice 2008-07+ | 50/50 arm + slice 2015+ |
|---|---|---|---|---|
| IBS | 10 bps | 8.16% / 0.667 / 16.9% | 0.603→0.616, 26.4%→27.9% (passes) | 0.684→0.706, 17.6%→19.2% (passes) |
| IBS | 25 bps | 5.17% / 0.373 / 17.4% | 0.603→0.573, 26.4%→29.7% | 0.684→0.664, 17.6%→20.9% |
| IBS | model | 5.31% / 0.387 / 18.0% | 0.603→0.571, 26.4%→30.2% | 0.684→0.667, 17.6%→21.0% |
| RSI(2) ETFs | 10 bps | 2.82% / 0.236 / 9.2% | 0.603→0.616, 26.4%→26.4% (passes) | 0.684→0.685, 17.6%→17.9% (passes) |
| RSI(2) ETFs | 25 bps | 1.99% / 0.032 / 10.1% | 0.603→0.611, 26.4%→26.4% | 0.684→0.666, 17.6%→18.1% |
| RSI(2) ETFs | model | 2.22% / 0.089 / 9.8% | 0.603→0.616, 26.4%→26.4% | 0.684→0.671, 17.6%→18.1% |
| RSI(2) stock leg | 10 bps | 2.67% / 0.122 / 37.8% | 0.603→0.584, 26.4%→27.1% | 0.684→0.652, 17.6%→18.3% |
| RSI(2) stock leg | 25 bps | −4.20% / −0.352 / 53.2% | 0.603→0.441, 26.4%→27.8% | 0.684→0.480, 17.6%→19.1% |
| RSI(2) stock leg | model | −6.49% / −0.517 / 59.7% | 0.603→0.399, 26.4%→27.9% | 0.684→0.432, 17.6%→19.1% |
| Variant | 10 bps | 1.35% / −0.104 / 9.6% | 0.603→0.591, 26.4%→26.3% | 0.684→0.667, 17.6%→17.5% |
| Variant | 25 bps | −1.44% / −0.709 / 21.9% | 0.603→0.519, 26.4%→26.5% | 0.684→0.585, 17.6%→17.7% |
| Variant | model | −1.00% / −0.614 / 18.9% | 0.603→0.525, 26.4%→26.9% | 0.684→0.595, 17.6%→17.9% |

The passive arms reproduce the feasibility report's static blends. Since 2015 the 20/80 arm returned 4.30% a year at a 7.1% maximum drawdown, against the report's 4.4% and 7.1%. The 50/50 arm returned 7.74% at 17.6%, against 7.9% and 17.6%.

## What was run

`artifacts/api-server/backend/scripts/run_short_horizon_reverdict.py`. Its docstring holds the pre-registered rule; the decision functions are pure and tested (`tests/test_short_horizon_reverdict.py`).

* **Data ($0).** yfinance daily bars (split- and dividend-adjusted) from the step 1 cache, from 2000-01-01 to 2026-11-06. The membership file is fja05680's (coverage since 2010 80.9%). T-bills are BIL's total return, the T-bill leg the step-3 checks use. Earnings blackouts come from the earnings cache, which covers today's names only (the survivorship report's convention). No paper cost calibration is recorded locally, so the published model priced the model column.
* **IBS and RSI(2) on the ETFs.** These are the live replicas (`gate_replay.simulate`, `calibration=None`). IBS enters below 0.1 with no trend filter and exits above 0.8. RSI(2) enters below 10 with close above SMA200 and the live 50-day SPY gate, and exits with close above SMA5. Both use a 10-session time stop and a 10% stop, enter at the next open and exit at the open.
* **RSI(2)'s stock leg, once.** It uses the same rule and setup on every point-in-time S&P 500 single stock with history (691 loaded, 610 with at least one trade); a bar is enterable only while the stock was a member. `gate_replay.simulate` would cut the scope to the 29 live names, so the script calls the same replica functions without the scope. The sleeve has five slots of a fifth each (the live per-strategy cap), filled first-come in entry-date order.
* **The variant.** On each of the four ETFs: buy the close of a session whose close-to-close return is at or below −1.5 times the standard deviation of the previous 20 daily returns, and sell at the next official open. The −1.5 threshold was fixed before the run and not tuned; it is the only free parameter.
* **Sleeve series.** One slot per ETF (a quarter of the sleeve each, one position per symbol), or five shared slots for the stock leg. Idle days earn T-bills. Each round trip's cost is charged half on its entry day and half on its exit day. The model cost is each trade's own two sides (`yardstick.trade_costs`; for the variant, one side at the close and one at the next open).
* **Marginal rule.** SPY, the sleeve and BIL are rebalanced to target weights on each month's last session. Sharpe is over BIL. Windows run from 2008-07-01 and from 2015-01-01 to the latest bar.

## Changes from the spec, and approximations

* **15:45 decision prices.** Daily bars have no 15:45 price, so the variant decides on the official close and also fills there. That is 15 minutes of look-ahead, and it assumes a closing-auction fill that Alpaca's 15:50 cutoff would allow. The approximation most likely flatters the variant, and the variant still fails at every cost.
* **"Up to 20% / 10% of risk"** is read as 20% or 10% of equity for the slice in check (iii), the step-3 convention. At the live arm's 20% equity weight the sleeve's own volatility is the only risk in the slice.
* **Check (ii) has two readings.** The decisive one is the sleeve's daily net Sharpe over T-bills. The report's shorthand, the date-clustered t divided by the square root of the years, is shown beside it. They differ: IBS is 0.67 daily against 0.88 trade-level, because the four ETFs' positions overlap in the same sell-offs. Both fail for every rule at every cost.
* **Data hygiene for past members.** yfinance serves some delisted tickers' later bars as flat, zero-volume prints, for example CPWR in 2009-2010 and EP in 2003-2009. A stop replay turned these into fake +35% trades. The script therefore needs a traded decision bar, a traded entry bar and a 20-session median dollar volume of at least $1 million. The filter was added after the first run showed the glitch trades in the 2008 window, and before any decision was read. It removes 346 of 43,097 stock-leg trades (19 since 2015) and five names entirely (CCU, CPWR, EP, GENZ, PARA). Gross since 2015 is +26.5 bps with or without it. The ETF candidates are unaffected.
* **Measured cost.** The step 1 cost table is empty locally (no paper fills in this ledger). The script takes `--measured-cost-bps`, but no rule reached the 10 bps branch, so the value cannot change the outcome.

## What changes live after a publish

* The seed step `seed.apply_short_horizon_reverdict` (audit action `short_horizon_reverdict`, event `strategy_governance`) runs once per row on the next seed pass. IBS Mean Reversion and RSI(2) Pullback move from their current trading or exploring status to `shadow`. They keep generating and recording signals, including RSI(2)'s stock signals, but place no entries, scheduled or exploration. Open lots exit by their normal rules (broker legs, time stops, protective stop) and are not force-sold.
* A row that is already paused, retired or in shadow is left alone, and the skip is recorded. Once the audit row exists, a later operator or engine decision on that row is never overridden. To bring a rule back, set its status. Re-admission should come from a new forward test, not from this in-sample record.
* No other short-horizon strategy holds capital after this, so the bot's capital is the passive arm (VOO/SGOV) plus the cost probes. The slow book's `target_weight_slot_reserve` (10% of equity, kept for IBS and RSI(2) entries) stays in place and now sits in cash. Lowering it is a separate owner decision and is not changed here.
* No strategy row is created for the variant. No migration.

## Reproduce

From `artifacts/api-server/backend`, with the step 1 cache (yfinance bars for the four ETFs, BIL, SPY and every past member, plus `universe/` membership):

    PYTHONPATH=. python scripts/run_short_horizon_reverdict.py --data-dir <cache> --earnings-dir <dir with earnings/> \
        --end 2026-11-07 --output out.json --tables tables.md

The run takes about 2.5 minutes on a laptop (20 s loading, 2 s for the ETF rules, about 2 minutes for the stock leg, which replays twice to count the hygiene filter).
