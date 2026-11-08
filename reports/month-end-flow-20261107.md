# Month-end rebalancing flow: 2026-11-07

**What this is:** step 7 of the build plan in [Retail bot edge feasibility](Retail%20bot%20edge%20feasibility.md) ("replicate month-end flow"), run exactly as pre-registered. **Result: it fails, so it is dropped.** No calendar variant beyond the two below was tested, and nothing in the slow book changes.

Script: `artifacts/api-server/backend/scripts/run_month_end_flow.py`. Its docstring holds the pre-registration, written before the first run.

## The test as registered

* **Data:** yfinance adjusted daily closes for SPY and IEF. IEF's first bar is 2002-07-30, so the joint sample runs from 2002-07-30 to 2026-11-06. The current month (November 2026) is incomplete and excluded.
* **Signal:** on each of the last three sessions of a month, the month-to-date SPY return minus the month-to-date IEF return, both measured from the prior month's last close. It is divided by its trailing standard deviation, `s × √n`. Here `s` is the standard deviation of the daily SPY-minus-IEF return over the **63 sessions ending that day**, and `n` is the number of sessions in the month so far. The 63-session window was chosen before the run. It is one quarter, it uses only closes known that day, and it is short enough that the 2002 months enter the sample (first signal: 2002-10-29).
* **Outcome:** the next session's SPY-minus-IEF return, in bps. For the month's last session, that is the first session of the new month.
* **Model:** pooled OLS of the outcome on the signal. **Errors: Newey-West HAC with 2 lags**, on the stacked signal days in date order, so a month's three overlapping signals can share errors. Date-clustered errors would equal plain robust errors here, because every observation falls on a different date.
* **Variants (two only):** all month-ends, and quarter-ends (March, June, September, December), with the same month-to-date signal in both.
* **Pass rule (per variant):** the slope is negative with t ≤ −2 over the full sample, and the 2015-2026 slope is negative at no less than half the full-sample size.

## Results

Slope is in bps of next-day SPY-minus-IEF return per one standard deviation of the signal.

| Variant | Window | Signal days (months) | Slope (bps/SD) | HAC SE | t (HAC) | Pass checks |
|---|---|---|---|---|---|---|
| All month-ends | 2002-2026 | 867 (289) | −3.68 | 5.46 | **−0.67** | negative yes; t ≤ −2 **no** |
| All month-ends | 2015-2026 | 426 (142) | −4.65 | 6.64 | −0.70 | negative yes; ≥ half yes |
| Quarter-ends | 2002-2026 | 288 (96) | −9.90 | 9.87 | **−1.00** | negative yes; t ≤ −2 **no** |
| Quarter-ends | 2015-2026 | 141 (47) | −2.25 | 11.71 | −0.19 | negative yes; ≥ half **no** (23%) |

**Both variants fail.** The sign matches the published effect, but the size does not. The paper's one-SD effect is about −19 bps of stocks minus bonds. Here the full-sample slope is −3.7 bps for all month-ends and −9.9 bps for quarter-ends, and neither t-statistic comes near −2. Plain OLS t-statistics are almost the same (−0.70 and −0.95), so the result does not depend on the error model.

One diagnostic is not part of the rule but matters for the planned overlay. On the signal days where the signal was above +1 SD, which are the days the overlay would have delayed VOO buys, the next-day spread **averaged +6.3 bps** (166 days) over the full sample and +13.7 bps (91 days) since 2015. Delaying equity buys on those days would, on average, have bought later at a higher relative price.

## Decision

* **Drop the month-end overlay.** The slow book's VOO tranche timing is unchanged, and `target_weight_book.py` is not touched.
* **Test no further calendar variants**, as the plan requires. That rules out quarter-to-date signals, other windows, VIX-conditioned versions and other lookbacks. They were not run and will not be.
* Possible reasons, untested: decay after publication (the paper came out in 2025-26, but the effect is weak even in 2002-2014), the gap between the paper's simulated 60/40 futures weights and a spot ETF spread, and our scaling by trailing volatility, which down-weights crisis months where the paper's gains concentrate. None of these is grounds for a re-run under the plan's rules.

## Reproduce

```
cd artifacts/api-server/backend
PYTHONPATH=. python scripts/run_month_end_flow.py --data-dir <cache dir> --download --output <json>
```

The cache (SPY.csv, IEF.csv) and the JSON output live in an uncommitted scratch directory. The data cost is $0.
