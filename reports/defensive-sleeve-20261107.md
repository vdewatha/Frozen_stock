# Defensive sleeve: 2026-11-07

**What this is:** step 3 of the build plan in [Retail bot edge feasibility](Retail%20bot%20edge%20feasibility.md) ("Settle the defensive sleeve"). I ran the pre-registered test as written. **Both designs fail the rule, so no sleeve is funded.** The static passive arm keeps all of the book's capital. Design A (TSMOM-IV) now runs beside it as a zero-capital shadow. The shadow places no orders, and the rule will be re-decided once, at 36 months or after the next SPY drawdown deeper than 20%, whichever comes first. Paper only.

Code: `app/services/defensive_sleeve_rule.py` holds the frozen designs, the backtest, `RULE` and `DECISION`. `app/services/defensive_sleeve.py` holds the shadow and the re-decision. `scripts/run_defensive_sleeve_check.py` runs the test. The other changes are small: a seed step, one job, one beat entry, one setting, and a field on `GET /stock-paper/target-weight-book`. No database migration: all state lives in audit rows and strategy-row parameters.

## The test, as pre-registered

* **Designs.** I froze Design A (TSMOM-IV) and Design B (GTAA5-ENS) exactly as the [ETF note](../research_notes/Retail%20bot%20edge%20feasibility/etf_rotation.md) (Q7) defines them, and coded them to match `etf_rotation_backtest.py`, `verify_etf.py` and `blend_check.py`.
  * Each signal is taken at one close and traded at the next close.
  * Weights drift between rebalances.
  * Cost is charged per unit of one-way turnover, at 11 and 25 bps a side.
  * Each design is tranched: four 25% sleeves, rebalancing monthly at 0, 5, 10 and 15 sessions before month-end.
* **The arm.** The live passive arm holds 20% SPY and 80% T-bills (BIL), rebalanced monthly. A slice of x takes x of equity from the T-bill leg, giving 20% SPY, x design and (80% − x) BIL. I report slices of 10%, 20% and 30%.
* **Rule** (`defensive_sleeve_rule.RULE`). A design takes 20% of equity from the T-bill leg only if the 20% slice raises the arm's Sharpe with no more than 2 extra points of maximum drawdown. That must hold at both costs and in both windows (2008-07-01 to latest, and 2015-01-01 to latest).
* **Sharpe** is the daily excess over BIL, annualized.
* **Data.** The price cache is the yfinance file the feasibility report used: `px.csv`, md5 `8042865bca1be9a6ebb96e1ce1862747`, adjusted closes through **2026-11-06**, which is the latest session.
* **Reproduction.** On data to 2026-10-31, the code reproduces the report's blend check exactly. Under the 50/50 arm, Design A moved Sharpe from 0.611 to 0.636 at 11 bps and to 0.624 at 25 bps over 2008-2026. Since 2015 it moved Sharpe from 0.699 to 0.695 at 11 bps and to 0.681 at 25 bps. It also reproduces the standalone figures (4.04%, Sharpe 0.53, −10.6%).

## Result: both designs fail; every 2015+ cell lowers the arm's Sharpe

**Primary: the live 20/80 arm.** Each pair is the base arm against the arm with a 20% slice. Data runs through 2026-11-06.

| Design | Cost | Window | Arm Sharpe | + 20% slice | Arm max DD | + 20% slice | Extra DD | Cell |
|---|---|---|---|---|---|---|---|---|
| A, TSMOM-IV | 11 bps | 2008-07+ | 0.600 | **0.648** | −11.1% | −10.7% | −0.4 pts | pass |
| A, TSMOM-IV | 11 bps | 2015+ | 0.683 | **0.665** | −7.1% | −8.7% | +1.6 pts | **fail** |
| A, TSMOM-IV | 25 bps | 2008-07+ | 0.600 | 0.620 | −11.1% | −10.8% | −0.3 pts | pass |
| A, TSMOM-IV | 25 bps | 2015+ | 0.683 | 0.634 | −7.1% | −8.7% | +1.6 pts | **fail** |
| B, GTAA5-ENS | 11 bps | 2008-07+ | 0.600 | 0.600 (+0.0001) | −11.1% | −11.6% | +0.5 pts | pass (barely) |
| B, GTAA5-ENS | 11 bps | 2015+ | 0.683 | 0.630 | −7.1% | −9.8% | +2.7 pts | **fail** |
| B, GTAA5-ENS | 25 bps | 2008-07+ | 0.600 | 0.580 | −11.1% | −11.6% | +0.6 pts | **fail** |
| B, GTAA5-ENS | 25 bps | 2015+ | 0.683 | 0.607 | −7.1% | −9.8% | +2.7 pts | **fail** |

**Beside it: the report's 50/50 arm**, same construction, data through 2026-11-06.

| Design | Cost | Window | Arm Sharpe | + 20% slice | Arm max DD | + 20% slice | Extra DD | Cell |
|---|---|---|---|---|---|---|---|---|
| A | 11 bps | 2008-07+ | 0.603 | 0.627 | −26.4% | −26.1% | −0.4 pts | pass |
| A | 11 bps | 2015+ | 0.684 | 0.679 | −17.6% | −19.1% | +1.5 pts | **fail** |
| A | 25 bps | 2008-07+ | 0.603 | 0.615 | −26.4% | −26.1% | −0.3 pts | pass |
| A | 25 bps | 2015+ | 0.684 | 0.666 | −17.6% | −19.1% | +1.5 pts | **fail** |
| B | 11 bps | 2008-07+ | 0.603 | 0.608 | −26.4% | −26.7% | +0.3 pts | pass |
| B | 11 bps | 2015+ | 0.684 | 0.663 | −17.6% | −20.1% | +2.5 pts | **fail** |
| B | 25 bps | 2008-07+ | 0.603 | 0.599 | −26.4% | −26.7% | +0.3 pts | **fail** |
| B | 25 bps | 2015+ | 0.684 | 0.653 | −17.6% | −20.2% | +2.5 pts | **fail** |

**What the 20/80 arm changes.** The verdict is the same under both arms: both designs fail on the 2015+ window. The 20/80 arm makes the result sharper in both directions:

* **2008-07+:** A's Sharpe gain is about twice as large (+0.048 against +0.024 at 11 bps).
* **2015+:** its loss is about four times as large (−0.018 against −0.005 at 11 bps; −0.049 against −0.019 at 25 bps).
* **Why:** the arm holds less equity, so the slice is a larger part of the arm's risk, and its post-2015 weakness weighs more.

The report's own "already known" figures for A came from the 50/50 arm (2015+: 0.695 and 0.681 against 0.699). On the live arm and the latest data, the 2015+ cells are 0.665 and 0.634 against 0.683. Design B also fails 2008-07+ at 25 bps under both arms, and its 2015+ slice adds more than 2 points of drawdown.

The 10% and 30% slices tell the same story. Every 2015+ slice of either design lowers the Sharpe at both costs under both arms. The full grid is printed by the script.

**Standalone** (tranched, 20/80 not involved):

| Design | Cost | CAGR 2008-07+ | Sharpe | Max DD | CAGR 2015+ | Sharpe | Max DD |
|---|---|---|---|---|---|---|---|
| A, TSMOM-IV | 11 bps | 3.99% | 0.53 | −10.6% | 3.69% | 0.37 | −10.6% |
| A, TSMOM-IV | 25 bps | 3.34% | 0.41 | −10.7% | 3.03% | 0.24 | −10.7% |
| B, GTAA5-ENS | 11 bps | 4.47% | 0.44 | −13.8% | 4.33% | 0.37 | −13.7% |
| B, GTAA5-ENS | 25 bps | 3.94% | 0.38 | −14.0% | 3.79% | 0.29 | −13.9% |

### Volatility-matched static arms (monthly, 11 bps a side)

I matched static SPY/BIL arms to each design alone, as the report did, and to each 20%-slice arm. The second match asks whether a slightly larger SPY weight would do the slice's job.

| Matched to | Window | Target CAGR / Sharpe / DD | Static SPY weight | Static CAGR / Sharpe / DD |
|---|---|---|---|---|
| A alone | 2008-07+ | 3.99% / 0.527 / −10.6% | 27% | 4.31% / 0.601 / −14.8% |
| A alone | 2015+ | 3.69% / 0.372 / −10.6% | 28% | 5.22% / 0.683 / −10.0% |
| B alone | 2008-07+ | 4.47% / 0.440 / −13.8% | 39% | 5.67% / 0.602 / −21.2% |
| B alone | 2015+ | 4.33% / 0.367 / −13.7% | 40% | 6.57% / 0.684 / −14.1% |
| 20/80 + 20% A | 2008-07+ | 4.10% / 0.648 / −10.7% | 22% | 3.79% / 0.600 / −12.3% |
| 20/80 + 20% A | 2015+ | 4.65% / 0.665 / −8.7% | 23% | 4.67% / 0.683 / −8.3% |
| 20/80 + 20% B | 2008-07+ | 4.20% / 0.600 / −11.6% | 25% | 4.10% / 0.601 / −13.8% |
| 20/80 + 20% B | 2015+ | 4.79% / 0.630 / −9.8% | 26% | 4.98% / 0.683 / −9.2% |

These confirm the report. A design's whole advantage over a static arm at the same risk comes from 2008-09: over 2008-07+ the A slice beats a 22% SPY arm by 0.048 Sharpe and 1.6 points of drawdown. Since 2015, a static arm with 3 more points of SPY matches the A slice's return with a higher Sharpe and a shallower drawdown.

### Criterion 3 (reported, not decisive)

The ETF note's untested criterion 3 asks whether a 50/50 blend with the bot's actual equity book shows a lower maximum drawdown and a Sharpe no lower than the book alone. I tested it on the replayed book's daily P&L:

* **Book replay:** today's 45 names, every replayable strategy under its live gates, the live slots at 5% of equity, and the published cost model. This is the yardstick's book replay (`run_yardstick_check.book_daily`) on its yfinance cache.
* **Blend:** daily rebalanced, as the criterion states.
* **Idle cash:** the replay credits idle cash with nothing. I also show a version where the uninvested share earns T-bills.

| Window | Book (idle cash 0) CAGR / Sharpe / DD | + 50% A @11 | + 50% A @25 | + 50% B @11 | + 50% B @25 |
|---|---|---|---|---|---|
| 2008-07+ | 3.59% / 0.276 / −20.9% | 3.89% / 0.409 / −11.3% | 3.56% / 0.363 / −11.6% | 4.11% / 0.381 / −14.5% | 3.85% / 0.350 / −14.6% |
| 2015+ | 4.35% / 0.287 / −17.1% | 4.11% / 0.353 / −11.3% | 3.78% / 0.305 / −11.6% | 4.41% / 0.349 / −13.6% | 4.14% / 0.316 / −13.7% |

With idle cash at T-bills, the book makes 3.83% / 0.298 / −20.5% over 2008-07+ and 4.66% / 0.317 / −16.8% since 2015. Every blend still meets the criterion.

**Every blend meets criterion 3 in both windows, at both costs, under either cash treatment.** This is not evidence for trend. The book's own Sharpe is about 0.3, and the yardstick already showed that the passive arm beats it. Almost any low-volatility, positive-carry stream blended 50/50 would pass this test. The same blend with the passive arm, which is the marginal rule the plan judges every sleeve by, fails. That is why criterion 3 is reported and not decisive.

## Decision

**No design takes capital.** Design A fails in the 2015+ window at both costs. Design B fails there too, and also fails 2008-07+ at 25 bps. The static arm keeps the T-bill leg. As the plan says, Design A runs as a zero-capital shadow and is re-decided once under the same rule. Design B is dropped: the plan names A as the shadow, and B fails in more cells.

The decision is recorded in `defensive_sleeve_rule.DECISION` (per design, cost and window: the arm's and the slice's Sharpe and drawdown under both arms, and pass or fail). The live code reads it from there.

## What was built

* **Zero-capital shadow sleeve.** The seed creates a `Strategy` row once: "Defensive Sleeve A (TSMOM-IV, shadow)", type `defensive_tsmom_iv`, status `target_weight_shadow`, with a governance row `defensive_shadow_admission`. Its parameters carry:
  * `sleeve_cadence: "monthly"`, so `fixed_20` never judges it and the step 1 harm guardrail reads it;
  * `capital_fraction: 0`;
  * **no** `target_weights`.

  As a result, the slow book lists it as a zero-capital sleeve, and `book_symbols` never removes its ETFs from the slot cycle. The slot cycle skips any monthly-sleeve row, so the shadow cannot trade even if a resume-all moves its status. If that happens, the daily job restores the status.
* **Monthly target weights and hypothetical tranched fills.** There are four tranches on the book's own schedule: tranche *k* on the *k*-th Tuesday, or the next session that week.
  * On a tranche's session, the target weights are Design A's signal at the previous close.
  * The hypothetical fill is at that session's close, at 11 bps a side of one-way turnover.
  * Each tranche starts as a quarter of the notional in BIL and drifts between its rebalances, as in the backtest. The notional is 20% of account equity when the shadow starts.
  * A `defensive_sleeve` / `shadow_tranche` row records each rebalance: signal session, target and previous weights, the fills per symbol (side, notional, price), turnover and cost.
* **Daily shadow P&L against the passive arm.** One row per session, `sleeve_pnl` / `sleeve_daily_pnl`, with `entity_id` set to the shadow row. Each row holds `sleeve_return`, `passive_return`, `active_return`, P&L, value and the tranche states.
  * The passive return is the book's own passive-arm return for that session, from its `sleeve_daily_pnl` row. Until the book has one, the arm's weights are applied to VOO and SGOV closes, and the row records which source it used.
  * These rows have the shape `sleeve_guardrail` already reads, so the guardrail judges the shadow's forward record with no change to its code.
* **Data.** Prices are yfinance adjusted closes ($0). The state is carried as values and weights rather than shares, so later dividend adjustments to the price history do not move it.
* **Re-decision trigger.** At admission, a `redecision_scheduled` row is written with a due date of 2029-11-07 (36 months after the decision) and the SPY trigger. SPY's drawdown is measured from its running peak since the decision's data end, 2026-11-06.
  * The job fires the trigger once, on the first of: the due date, or a SPY close more than 20% below that peak.
  * When it fires, it writes `redecision_fired` and a warning notification. It then re-runs `defensive_sleeve_rule.evaluate` on fresh yfinance history from 2006 and records `redecision_verdict` with an info notification.
  * If the history cannot be fetched, the verdict is retried on later runs. Once a verdict exists, the trigger never fires again.
  * The state (`scheduled`, `fired` or `decided`, the due date, the trigger and the verdict) is the `defensive_sleeve.redecision` field on `GET /stock-paper/target-weight-book`. That field also shows the decision and the shadow's record: sessions, value, cumulative return against the arm, and the latest tranches.
* **Job.** `defensive_sleeve_daily_job` runs on the `learning_fast` queue at 09:05 ET on weekdays, after the book's 08:45 run, so that session's passive-arm row exists. It backfills up to 30 sessions and is idempotent. Setting: `DEFENSIVE_SHADOW_ENABLED` (default `true`).
* **Tests.** `tests/test_defensive_sleeve.py` covers:
  * the frozen signals and the rule;
  * the recorded decision;
  * the trigger dates and drawdown;
  * that admission is zero-capital and fences nothing;
  * hypothetical tranche fills and daily P&L with no orders, readable by the guardrail;
  * the passive-return source;
  * one firing and verdict;
  * the endpoint field;
  * the disabled path and the resume-all case.

## Interpretations and limits

* **The SPY trigger fires on the crossing.** "After the next SPY drawdown deeper than 20%" is read as the first close more than 20% below the post-decision peak. Waiting for the drawdown to end could take longer than the 36-month date and make that trigger moot.
* **A passing re-decision does not fund itself.** It records that the design qualifies, and the notification asks the owner to fund it at 20% of equity from the T-bill leg. A funded version would also need VOO in place of SPY (the book fences SPY from its sleeves) and SGOV in place of BIL. The engine today takes a sleeve's capital pro rata from the arm, not from its T-bill leg. I left both out because nothing passes today.
* **The shadow trades on the book's Tuesdays, not on the backtest's month-end offsets.** This is how a funded sleeve would actually trade. Both schedules use four weekly tranches.
* **The SPY in the arm stands in for VOO in the backtest.** VOO starts in 2010, and the two track within a few basis points.
* **The test's monthly rebalance of the mix is not charged.** This follows the report's blend check. The mix's turnover is about 0.2 times a year, so its cost is negligible at either level.

## Reproduce

```
PYTHONPATH=. python scripts/run_defensive_sleeve_check.py --px <dir>/px.csv \
    --book-data-dir <yardstick yfinance cache> --output <dir>/check.json
```

`px.csv` is what `research_notes/Retail bot edge feasibility/etf_rotation_backtest.py` writes. The run takes about 90 seconds, including the book replay, and writes `check.json` plus a `check.txt` table.
