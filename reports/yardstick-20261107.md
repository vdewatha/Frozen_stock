# Yardstick: 2026-11-07

> **Note added 2026-11-10.** The point-in-time S&P 500 replays in this report (the cost bracket, turnover and passive-score readings on the 707-symbol universe) included 18 past members whose tickers yfinance now serves under unrelated companies, ETFs or post-bankruptcy shells (BBT, CCU, EP, CPWR, GENZ, SII, PARA, CSR, KG, WEN, NC, FNMA, FMCC and others), so some of their trades are on another instrument's bars. `point_in_time_universe.coverage()` now rejects those histories before a replay; this report's numbers were not rerun. See `reports/point-in-time-coverage-20261110.md`, section 4.

Step 1 of `reports/Retail bot edge feasibility.md` ("Fix the yardstick", no new trials). Branch `feat/yardstick`.
Pure measurement: the experiment engine's `RULE`, the discovery gate and `fixed_20` keep acting on the cost they
always used; the bracket and the new scores are reported beside them. The only new decision rules are the
pre-registered short-hold veto (it can only block an engine adoption or a new discovery admission) and the
monthly-sleeve harm guardrail (it acts only on the target-weight book's sleeves from PR #45, and only once their
daily P&L rows exist). Paper only.

## What was built

| Spec item | Where | What it does |
|---|---|---|
| Cost bracket on every verdict | `yardstick.cost_bracket`, `bracket_decisions`; `experiment_engine.run_experiment` / `run_book_experiment` (`experiment["yardstick"]`), `forward_bracket` (every `change_verdict` and pending entry), `strategy_discovery.summarize_candidate` (`row["yardstick"]`) | Each replay re-priced at 5, 10, 25 bps a round trip and at the published model (recomputed per trade from the bars before entry and exit, whatever calibration the replay used); a `calibrated` column appears once a fitted cell has >= 20 fills; `acting` is the net the rule used. The engine also records which label `RULE` would adopt at each column. |
| Realised-cost table | `yardstick.realized_cost_table`, `record_yardstick` (Saturday 13:50, `yardstick` audit row), `GET /stock-paper/yardstick` | Realised per-side and round-trip cost by class and symbol straight from the paper fills (no shrinkage, no 0.5x floor), plus the probes' measured round trips. |
| Probe orders | `cost_probes.py`; `stock_paper_ledger.PROBE_SOURCE` / `PROBE_CLIENT_PREFIX`; job every 5 minutes in session (`learning_fast`) | One share of a rotation symbol (SPY, QQQ, IWM, DIA, XLK, XLF, AAPL, MSFT, AMZN, GOOGL), market order between 10:30 and 14:00 ET, sold 30 minutes later (or at the next session), default 3 a week (Mon, Tue, Thu). Source `cost_probe`, client ids `sp-probe-...`, scorecard label `probe`, so attribution, the loss diagnosis and the scoreboards never see them. The existing reservation, dispatch, idempotency keys, launch gates, kill switch and risk limits apply; a probe needs a flat symbol with no open order and two free position slots. |
| DIV check | `dividend_check.py`; weekly job (Saturday 13:40, `learning_fast`); `learning-scoreboard.yardstick.dividends`, `/stock-paper/yardstick` | Reads `GET /v2/account/activities?activity_types=DIV` on the paper account (400-day window) and the local DIV activity rows; reports `booked`, `none_found` or `unavailable` (no credentials or a broker error), never raises. |
| Turnover | `yardstick.turnover` | One-sided turnover per year = trades decided in the window x slot (`yardstick_slot_fraction`, 5%) / years, with the cost drag at each bracket. |
| Short-hold veto | `yardstick.short_hold_veto`; `experiment_engine.decide(..., vetoes=)` and the last sentence of `RULE`; `strategy_discovery.admit_survivors` | Median hold under 20 sessions => vetoed unless out-of-sample gross per trade >= max(30 bps, 3 x round-trip cost), at 10 bps until a measured round trip (>= 20 priced fills in the class) replaces it. Out of sample = the discovery holdout; for the engine the last two years of the replay (it has no pristine holdout; `RULE` says so). Recorded in `decision["short_hold_veto"]` and in the discovery run's `vetoed` list. |
| Excess and residual | `yardstick.passive_scores` | Daily equal-weight return minus a static SPY/T-bill mix whose SPY weight is the candidate's volatility over SPY's (capped at 1), month-clustered t; per-trade net minus beta x SPY over the same hold, entry-date clustered t (`verdict_stats.paired_stats`, CR0). T-bills from `^IRX` (weekly cache), else a 0% cash leg, flagged. |
| Trials per family | `yardstick.trial_families`; `discovery_trials` rows now keep `hash_families`; `gate(..., family_trials=)`; the engine hurdle's `family_trials` | Discovery hashes + engine candidates + `RESEARCH_TRIALS` (hand-run reports) per family. The discovery gate charges the library count plus the family's engine and research trials; the engine hurdle its own count plus the family's discovery and research trials (only ever stricter). Shown in `learning-experiments.yardstick` and `/stock-paper/yardstick`. |
| Monthly sleeves | `sleeve_guardrail.py`; `experiment_engine.is_monthly_sleeve`, `review_changes`; daily job at 17:15 (`learning_fast`) | Rows with `parameters["sleeve_cadence"] == "monthly"` skip `fixed_20`; the guardrail acts (rule below). The e-process is still computed and recorded in shadow; `fixed_20` still acts on every other row. |

**The guardrail rule (fixed in `sleeve_guardrail.RULE`).** On the daily active return (sleeve minus passive arm, one
observation per session), it is harm if, after at least 60 sessions, the date-clustered t of the mean daily active
return is at or below -2 (one-sided, about the 2.3% level), or if, at any time, the active wealth ratio
prod(1+sleeve)/prod(1+passive) falls more than 15% from its peak. Harm pauses the sleeve row
(`strategy_governance` / `sleeve_guardrail_pause`) and reverts an engine change on it. Daily P&L is read
defensively: the target-weight book's `target_weight_book` / `sleeve_daily_pnl` rows (each sleeve's `return`
against `passive_arm.return`; the arm is never judged against itself), registered readers,
`parameters["sleeve_daily_pnl"]`, or other sleeve audit rows as `{session, sleeve_return, passive_return}` or
`{session, active_return}`. Nothing readable means `no_data`, and nothing is done.

Settings (all in `app/core/config.py`): `yardstick_slot_fraction` 0.05, `yardstick_short_hold_veto` True,
`cost_probe_per_week` 3 (0 disables), `cost_probe_quantity` 1, `cost_probe_max_notional` 1000,
`cost_probe_hold_minutes` 30, `cost_probe_symbols` (above), `sleeve_guardrail_enabled` True,
`sleeve_guardrail_action_enabled` True. Endpoint: `GET /stock-paper/yardstick` (viewer). No migration: every new
record is an audit row or a JSON field.

## Which seven searches

Section 1 of the feasibility report cites eight sources, and its conclusion splits the seven failures into rule
searches ("whether a candidate beat zero or the stock's own drift") and book-level searches ("whether a change beat
the existing book"). The seven are **the discovery library** (rule mutation, 422 trials), **sector-relative
reversal**, **the liquid-universe check**, **news entries**, **regime allocation**, **position sizing** and **the
inverse-ETF hedge**. The survivorship report is a check that the others lean on, not a search, and gap fill /
earnings drift is not cited in section 1 (it is cited there only for the cost model).

## How a search is re-scored

`scripts/run_yardstick_check.py` uses $0 data: yfinance daily history cached under the scratch area (the 45 core
names, SH, VIX, ^IRX, and 704 past and present S&P 500 members from the fja05680 membership file; coverage since
2010 80.9%, as in the survivorship report), data through 2026-11-06, the published cost model (no paper
calibration is recorded locally). For each candidate:

* **Net check**: the month-clustered 95% lower bound of net expectancy per trade is above zero (the discovery gate's
  check), at the acting cost (here the published model) and, for the bracket, at 5, 10 and 25 bps.
* **Excess check**: excess over the volatility-matched SPY/T-bill arm with t >= 2 **and** SPY residual with t >= 2,
  both with a positive mean.
* A candidate **passes the new yardstick** only if it passes the net check at the acting cost, the excess check and
  the short-hold veto. The tables also show the optimistic reading at 5 bps.

Book-level searches are scored on the live book: today's 45 names, every replayable strategy under its live gates,
the live slots (`run_slot_allocation_backtest.allocate`, 5% of equity a slot). Each arm gets its weighted net per
trade and the book's daily return against the passive arm at the model and at each bracket (each trade's model
cost added back on its exit day, less the bracket cost).

## Result: all re-scored searches still fail; the gate did not get looser

| Search | Re-run here? | Candidate(s) | Net check (acting cost / 5 bps) | Excess check | Short-hold veto | Verdict under the new yardstick |
|---|---|---|---|---|---|---|
| Discovery library (rule mutation) | yes, the 362 library candidates on the point-in-time S&P 500 (no mutation children: they live in the host's trial ledger) | 362 | 16 / 158 pass | 0 pass (best residual t +1.92) | 177 vetoed | **fails**: 0 of 362 pass both checks, at the model or at 5 bps |
| Sector-relative reversal | yes, the default rule inside that run | default rule, 16,597 trades | fails (-13.0 bps [-24.5, -1.9]) / passes (+14.9) | fails (residual -20.5 bps, t -8.3) | vetoed (3-session hold, OOS gross +28.0 < 30) | **fails** |
| Liquid universe (core list) | yes, the core list's pooled long-only signals since 2015; the terciles were not re-run (below) | pooled, 30,876 trades | fails (+17.0 [-1.9, +37.6]) / passes (+42.9) | fails (-3.3%/y, t -0.8; residual -18.0 bps, t -6.0) | vetoed (4-session hold, OOS gross +22.5) | **fails** |
| News entries | **no** (below) | - | - | - | - | not re-scored; the cited excess-over-drift interval [-90, +93] bps already fails |
| Regime allocation | yes, all three arms on the live book | regime arm | fails (+16.3 [-5.4, +40.1]) / passes (+39.8) | fails (book 3.7%/y vs 8.6%/y passive, t -2.6; +0.7%/y at 5 bps, t +0.4; residual t -3.5) | - | **fails** |
| Position sizing | yes, all seven arms on the live book | every arm | fails at the model in every arm / passes at 5 bps | fails in every arm (best: +1.5%/y at 5 bps, t +0.9) | - | **fails** |
| Inverse-ETF hedge | yes, the SH leg and the book with it at 2%, 5% and 10% | SH leg; books | leg fails at every cost (-151.5 bps at the model since 2015, -131.5 at 5 bps); books fail at the model | fails (leg residual -68.6 bps, t -5.3; books t -2.6 to -2.7) | leg vetoed | **fails** |

**Pass-rule verdict: met, as far as the data allowed.** Every search that could be re-run fails its net or its
excess-return check under the new yardstick, at the acting cost and at 5 bps a round trip. The cheaper brackets turn
many net checks positive (158 discovery candidates, the whole live book and every sizing and allocation arm pass the
net check at 5 bps), so the net check alone would have got looser; the excess check is what holds them out, and it
fails everywhere. No candidate passes the excess check, so there is nothing to fix before step 2 on this evidence.

What the numbers say, in short:

* **The live book trails the passive arm at the published model and roughly ties it at 5 bps.** Since 2015 the book
  (5,484 trades, one-sided turnover **23.2x equity a year**) made 4.35% a year at a 17.1% maximum drawdown, against
  8.70% for a SPY/T-bill mix at the book's own volatility (56% SPY). At 5 bps a round trip the book would have made
  10.3% a year, +1.5 points over the arm with t +0.9; at 10 bps +0.3 points (t +0.2); at 25 bps -3.2 points. This is
  the feasibility report's arithmetic, now measured: at planning costs the book roughly ties the passive arm and the
  timing adds nothing. The trades' SPY residual is -16.5 bps a trade (t -3.5) since 2015, so before costs the
  signals bought market drift, not timing.
* **No arm of any book-level search changes that.** Regime allocation, the six sizing modes and the three hedge sizes
  move the book's return by tenths of a point and leave every excess t below +1 at 5 bps.
* **The two `explore` survivors fail the new excess check.** Calm-market dip on the index ETFs (131a87e5c1, +44.9 bps
  net [+19.8, +69.7]) and the sector-ETF weekly reversal (c8e129d06c, +65.2 [+19.0, +107.6]) both pass the net check
  and the old excess-over-drift check, but against SPY their residuals are -13.2 bps (t -2.7) and -7.6 bps (t -0.6),
  and their books trail the risk-matched arm by 1.5-1.8 points a year. Their deflated Sharpe is 0.0 at 422 trials.
  They are already admitted, so the veto does not retire them (they are not vetoed anyway: OOS gross +56 and +113 bps).
* **Sector reversal is vetoed and fails both checks.** Gross +19.9 bps a trade over 3-session holds on the
  point-in-time single stocks (discovery window; +19.7 from 2015 to the holdout, against the report's +21.3 since
  2015 including the holdout). Net turns positive only at 5-10 bps, and the SPY residual is -20.5 bps (t -8.3).
* **The short-hold veto bites hard.** 177 of 362 library candidates are vetoed (all 54 opening-gap fills, all 8 sector
  reversals, 28 of 36 turn-of-month rules), and so are 6 of the 9 live strategies' signal sets on the core list. The
  live strategies are not touched by it: the veto acts only on engine adoptions and new discovery admissions.

## What was not re-run, and why

* **News entries.** The news-tone history is an Alpaca news backfill cached on the host (`news_history`), and
  re-downloading it needs the account's API keys; the 12 news candidates in the library therefore fire no trade in
  this run (rows `positive_news_trend`, `negative_news_dip`, `post_upgrade_drift` above, zero trades). The report's
  own figures (+169 to +186 bps a trade, excess over drift [-90, +93] bps) already fail the excess check, and the
  yardstick will score them on the host's next discovery run.
* **Liquidity terciles.** The liquid-universe check's tercile replay runs nine strategies over 700 names with the
  Corwin-Schultz spread panel; it was not re-run in this session. The headline core-list reading (above) fails the
  excess check, and each tercile traded below the core list per trade before any extra spread.
* **Earnings drift.** Earnings surprises were not re-downloaded for 700 names, so the 24 earnings-drift candidates
  fire no trade here (as on a host without the cache).
* **Mutation children.** They are stored in the host's `discovery_trials` ledger, not in the repository.

## Caveats

* **The passive arm treats idle capital as a cost.** A candidate's daily book is the equal-weight return across the
  symbols it traded, flat days included, and the arm is matched to that book's volatility. A rule that is rarely in
  the market therefore faces an arm that is mostly T-bills and loses to it on its flat days; that is the feasibility
  report's point (unassigned capital should sit in the passive arm), but it makes the excess-vs-passive reading harsh
  for sparse rules (several 11-47 trade ETF rules show t below -7). The per-trade SPY residual is scale-free, and it
  fails for every candidate on its own (best t +1.92), so the verdict does not rest on the daily comparison.
* All windows overlap, today's 45 names flatter the book-level searches (survivorship report), every post-2015 number
  has been seen before, and T-bills come from the 13-week yield, not a traded fund.
* The engine's out-of-sample window for the veto is its last two years, which its rule's windows also contain.
* Runtime: on a laptop the yardstick adds about 44% to a discovery run (80 candidates on the 45 names: 16.3 s without
  it, 23.4 s with it); the point-in-time library run here took 37 minutes with it.

## Reproduce

From `artifacts/api-server/backend`, with a cache filled by `strategy_scorecard.load_daily_history`, the membership
file and coverage under `<dir>/universe`, and `date,close` CSVs of `^VIX` and `^IRX`:

    PYTHONPATH=. python scripts/run_yardstick_check.py --stage book --data-dir <dir> --vix-csv <vix.csv> \
        --irx-csv <irx.csv> --output book.json
    PYTHONPATH=. python scripts/run_yardstick_check.py --stage pit --data-dir <dir> --irx-csv <irx.csv> \
        --output pit.json

## Tables

All figures are bps a trade unless marked; "net check" is the month-clustered 95% lower bound above zero, "excess
check" is excess over the volatility-matched SPY/T-bill arm (month-clustered t) and the SPY residual (entry-date
clustered t), both t >= 2 with a positive mean. OOS gross is the gross per trade over the last two years (book stage)
or the discovery holdout (point-in-time stage), the veto's window. The acting cost is the published model in every
table here.

**Liquid universe: the core list's long-only signals, pooled, 2015+ (and per strategy)**

| Candidate | Trades | Gross | Net at 5 / 10 / 25 bps | Net at model (95% CI) | Median hold | OOS gross | Veto | Excess vs passive (t) | SPY residual (t) | Net check | Excess check | Passes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| all strategies pooled | 30876 | +47.9 | +42.9 / +37.9 / +22.9 | +17.0 [-1.9, +37.6] | 4.0 | +22.5 | yes | -3.27%/y (-0.83) | -18.0 (-5.96) | model: no, 5 bps: yes | no | no |
| bollinger_mean_reversion | 1048 | +70.4 | +65.4 / +60.4 / +45.4 | +42.6 [-3.7, +93.3] | 5.0 | +11.4 | yes | -2.37%/y (-4.93) | -11.1 (-0.88) | model: no, 5 bps: yes | no | no |
| channel_breakout | 2332 | +58.9 | +53.9 / +48.9 / +33.9 | +31.6 [+1.6, +61.9] | 9.0 | +64.7 | no | -2.46%/y (-3.28) | +4.1 (+0.48) | model: yes, 5 bps: yes | no | no |
| ensemble | 5088 | +45.4 | +40.4 / +35.4 / +20.4 | +11.6 [-15.7, +39.2] | 3.0 | +11.6 | yes | -4.24%/y (-3.92) | -28.5 (-4.89) | model: no, 5 bps: yes | no | no |
| ibs_mean_reversion | 883 | +46.4 | +41.4 / +36.4 / +21.4 | +22.1 [-2.1, +44.1] | 2.0 | +51.6 | no | -3.99%/y (-1.90) | -22.1 (-7.66) | model: no, 5 bps: yes | no | no |
| macd_momentum | 5686 | +52.0 | +47.0 / +42.0 / +27.0 | +18.4 [-8.2, +44.4] | 4.0 | +29.1 | yes | -3.58%/y (-2.75) | -15.6 (-2.82) | model: no, 5 bps: yes | no | no |
| moving_average_crossover | 9460 | +38.5 | +33.5 / +28.5 / +13.5 | +6.3 [-16.3, +30.8] | 4.0 | +15.2 | yes | -6.20%/y (-4.40) | -22.6 (-5.26) | model: no, 5 bps: yes | no | no |
| rsi2_pullback | 2072 | +46.2 | +41.2 / +36.2 / +21.2 | +18.9 [-3.6, +41.3] | 3.0 | +43.3 | no | -2.73%/y (-3.89) | -5.1 (-0.80) | model: no, 5 bps: yes | no | no |
| rsi_mean_reversion | 1146 | +66.7 | +61.7 / +56.7 / +41.7 | +38.4 [-4.4, +82.1] | 5.0 | +0.7 | yes | -2.41%/y (-4.75) | -19.3 (-1.67) | model: no, 5 bps: yes | no | no |
| trend_pullback | 3161 | +52.0 | +47.0 / +42.0 / +27.1 | +25.0 [-0.1, +50.8] | 7.0 | +12.6 | yes | -2.63%/y (-3.23) | -17.5 (-2.39) | model: no, 5 bps: yes | no | no |

**Regime allocation: the live book under each arm, 2015+**

| Arm | Weighted net at 5 / 10 / 25 bps | At model (95% CI) | Book %/y at model / 5 bps | Max DD at model | Passive arm %/y (SPY weight) | Active %/y at model (t) / at 5 bps (t) | SPY residual bps (t) | Net check (model / 5 bps) | Excess check (model / 5 bps) | Passes |
|---|---|---|---|---|---|---|---|---|---|---|
| equal | +42.4 / +37.4 / +22.4 | +18.6 [-2.9, +42.6] | +4.35 / +10.27 | 17.1% | +8.70 (0.56) | -4.08 (-2.37) / +1.50 (+0.88) | -16.5 (-3.50) | no / yes | no / no | no |
| performance | +40.6 / +35.6 / +20.6 | +17.0 [-4.8, +40.7] | +3.90 / +9.64 | 17.3% | +8.65 (0.55) | -4.47 (-2.54) / +0.98 (+0.56) | -16.5 (-3.50) | no / yes | no / no | no |
| regime | +39.8 / +34.8 / +19.8 | +16.3 [-5.4, +40.1] | +3.70 / +9.36 | 17.7% | +8.63 (0.55) | -4.64 (-2.61) / +0.74 (+0.42) | -16.5 (-3.50) | no / yes | no / no | no |

**Position sizing: the live book under each arm, 2015+**

| Arm | Weighted net at 5 / 10 / 25 bps | At model (95% CI) | Book %/y at model / 5 bps | Max DD at model | Passive arm %/y (SPY weight) | Active %/y at model (t) / at 5 bps (t) | SPY residual bps (t) | Net check (model / 5 bps) | Excess check (model / 5 bps) | Passes |
|---|---|---|---|---|---|---|---|---|---|---|
| fixed_fraction | +42.4 / +37.4 / +22.4 | +18.6 [-2.9, +42.6] | +4.35 / +10.27 | 17.1% | +8.70 (0.56) | -4.08 (-2.37) / +1.50 (+0.88) | -16.5 (-3.50) | no / yes | no / no | no |
| volatility_target_0.25% | +42.1 / +37.1 / +22.1 | +18.2 [-3.8, +42.7] | +4.01 / +9.62 | 16.2% | +8.50 (0.54) | -4.23 (-2.48) / +1.09 (+0.65) | -16.5 (-3.50) | no / yes | no / no | no |
| volatility_target_0.50% | +42.4 / +37.4 / +22.4 | +18.6 [-2.9, +42.6] | +4.35 / +10.27 | 17.1% | +8.70 (0.56) | -4.08 (-2.37) / +1.50 (+0.88) | -16.5 (-3.50) | no / yes | no / no | no |
| volatility_target_1.00% | +42.4 / +37.4 / +22.4 | +18.6 [-2.9, +42.6] | +4.35 / +10.27 | 17.1% | +8.70 (0.56) | -4.08 (-2.37) / +1.50 (+0.88) | -16.5 (-3.50) | no / yes | no / no | no |
| fractional_kelly_p0.550 | +42.4 / +37.4 / +22.4 | +18.6 [-2.9, +42.6] | +4.35 / +10.27 | 17.1% | +8.70 (0.56) | -4.08 (-2.37) / +1.50 (+0.88) | -16.5 (-3.50) | no / yes | no / no | no |
| fractional_kelly_p0.575 | +42.5 / +37.5 / +22.5 | +18.7 [-3.1, +42.9] | +4.25 / +10.03 | 15.7% | +8.61 (0.55) | -4.10 (-2.40) / +1.36 (+0.81) | -16.5 (-3.50) | no / yes | no / no | no |
| fractional_kelly_p0.600 | +42.1 / +37.1 / +22.1 | +18.2 [-3.8, +43.2] | +3.97 / +9.53 | 16.2% | +8.48 (0.54) | -4.25 (-2.51) / +1.03 (+0.62) | -16.5 (-3.50) | no / yes | no / no | no |

**Inverse-ETF hedge: the SH leg's trades**

| Candidate | Trades | Gross | Net at 5 / 10 / 25 bps | Net at model (95% CI) | Median hold | OOS gross | Veto | Excess vs passive (t) | SPY residual (t) | Net check | Excess check | Passes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| all | 134 | -87.9 | -92.9 / -97.9 / -112.9 | -114.1 [-166.8, -53.5] | 6.0 | -85.7 | yes | -12.64%/y (-3.34) | -61.9 (-8.06) | model: no, 5 bps: no | no | no |
| 2015+ | 59 | -126.5 | -131.5 / -136.5 / -151.5 | -151.5 [-198.1, -103.5] | 6.0 | -85.7 | yes | -17.77%/y (-3.36) | -68.6 (-5.32) | model: no, 5 bps: no | no | no |

**Inverse-ETF hedge: the live book with the SH leg at each size, 2015+**

| Arm | Weighted net at 5 / 10 / 25 bps | At model (95% CI) | Book %/y at model / 5 bps | Max DD at model | Passive arm %/y (SPY weight) | Active %/y at model (t) / at 5 bps (t) | SPY residual bps (t) | Net check (model / 5 bps) | Excess check (model / 5 bps) | Passes |
|---|---|---|---|---|---|---|---|---|---|---|
| hedge_0.02 | +39.6 / +34.6 / +19.6 | +15.9 [-5.6, +40.2] | +3.74 / +9.67 | 18.9% | +8.64 (0.55) | -4.61 (-2.63) / +1.01 (+0.58) | -19.6 (-4.12) | no / yes | no / no | no |
| hedge_0.05 | +38.5 / +33.5 / +18.6 | +14.8 [-6.5, +38.9] | +3.54 / +9.49 | 18.8% | +8.58 (0.55) | -4.75 (-2.67) / +0.90 (+0.51) | -19.6 (-4.12) | no / yes | no / no | no |
| hedge_0.1 | +36.8 / +31.8 / +16.8 | +13.1 [-8.1, +36.8] | +3.21 / +9.19 | 18.8% | +8.51 (0.54) | -5.01 (-2.73) / +0.69 (+0.38) | -19.6 (-4.12) | no / yes | no / no | no |
| baseline | +42.4 / +37.4 / +22.4 | +18.6 [-2.9, +42.6] | +4.35 / +10.27 | 17.1% | +8.70 (0.56) | -4.08 (-2.37) / +1.50 (+0.88) | -16.5 (-3.50) | no / yes | no / no | no |

**Discovery library on the point-in-time S&P 500, by family (discovery window)**

| Family | Candidates | Net check at model | Net check at 5 bps | Excess check | Short-hold vetoed | Passes (model) | Passes (5 bps) | Explore/proven tier today |
|---|---|---|---|---|---|---|---|---|
| bollinger_low | 24 | 2 | 10 | 0 | 6 | 0 | 0 | 0 |
| breakout_momentum | 36 | 0 | 20 | 0 | 0 | 0 | 0 | 0 |
| calm_market_dip | 12 | 4 | 10 | 0 | 6 | 0 | 0 | 1 |
| down_streak | 36 | 5 | 28 | 0 | 20 | 0 | 0 | 0 |
| earnings_drift | 24 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| gap_down_recovery | 24 | 0 | 1 | 0 | 15 | 0 | 0 | 0 |
| n_day_low | 36 | 0 | 21 | 0 | 19 | 0 | 0 | 0 |
| negative_news_dip | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| opening_gap_fill | 54 | 0 | 0 | 0 | 54 | 0 | 0 | 0 |
| positive_news_trend | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| post_upgrade_drift | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| pullback_in_uptrend | 24 | 0 | 12 | 0 | 14 | 0 | 0 | 0 |
| sector_reversal | 8 | 0 | 8 | 0 | 8 | 0 | 0 | 0 |
| turn_of_month | 36 | 0 | 18 | 0 | 28 | 0 | 0 | 0 |
| weekly_reversal | 36 | 5 | 30 | 0 | 7 | 0 | 0 | 1 |
| **all** | **362** | **16** | **158** | **0** | **177** | **0** | **0** | **2** |

**Discovery: every candidate whose net check passes at the model, or that is explore-tier today**

| Candidate | Trades | Gross | Net at 5 / 10 / 25 bps | Net at model (95% CI) | Median hold | OOS gross | Veto | Excess vs passive (t) | SPY residual (t) | Net check | Excess check | Passes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 9de5e4d602 weekly_reversal (index_etfs): BUY when dist_sma_200 > 0 and ret_5 < -0.08; exit when ret_1 | 14 | +195.2 | +190.2 / +185.2 / +170.2 | +163.6 [+117.8, +234.4] | 1.0 | n/a | yes | -1.75%/y (-10.54) | +12.5 (+0.50) | model: yes, 5 bps: yes | no | no |
| 9ca928f13d weekly_reversal (index_etfs): BUY when dist_sma_200 > 0 and ret_5 < -0.08; exit when dist_ | 11 | +207.8 | +202.8 / +197.8 / +182.8 | +176.1 [+81.8, +288.3] | 2.0 | n/a | yes | -1.85%/y (-9.06) | +30.0 (+1.15) | model: yes, 5 bps: yes | no | no |
| ed31def01d weekly_reversal (sector_etfs): BUY when dist_sma_200 > 0 and ret_5 < -0.08; exit when dist_ | 47 | +185.4 | +180.4 / +175.4 / +160.4 | +149.8 [+65.3, +237.8] | 3.0 | +159.0 | no | -1.84%/y (-7.83) | +68.1 (+1.92) | model: yes, 5 bps: yes | no | no |
| 131a87e5c1 calm_market_dip (index_etfs): BUY when dist_sma_200 > 0 and vol_ratio_20_100 < 1 and rsi_2 | 230 | +65.3 | +60.3 / +55.3 / +40.3 | +44.9 [+19.8, +69.7] | 3.0 | +56.1 | no | -1.53%/y (-3.54) | -13.2 (-2.68) | model: yes, 5 bps: yes | no | no |
| c8e129d06c weekly_reversal (sector_etfs): BUY when dist_sma_200 > 0 and ret_5 < -0.05; exit when dist_ | 313 | +97.0 | +92.0 / +87.0 / +72.0 | +65.2 [+19.0, +107.6] | 3.0 | +113.2 | no | -1.75%/y (-4.19) | -7.6 (-0.62) | model: yes, 5 bps: yes | no | no |
| fd878d9286 weekly_reversal (index_etfs): BUY when ret_5 < -0.08; exit when ret_1 > 0 | 172 | +126.9 | +121.9 / +116.9 / +101.9 | +82.3 [+12.6, +162.8] | 1.0 | +651.1 | no | -2.70%/y (-2.27) | -0.1 (-0.01) | model: yes, 5 bps: yes | no | no |
| be21113675 calm_market_dip (sector_etfs): BUY when dist_sma_200 > 0 and vol_ratio_20_100 < 1 and rsi_2 | 598 | +55.0 | +50.0 / +45.0 / +29.9 | +31.4 [+10.4, +51.7] | 3.0 | +31.7 | no | -1.66%/y (-4.92) | -13.2 (-2.23) | model: yes, 5 bps: yes | no | no |
| 4ec6c68526 calm_market_dip (index_etfs): BUY when dist_sma_200 > 0 and vol_ratio_20_100 < 1 and rsi_2 | 480 | +50.2 | +45.2 / +40.2 / +25.2 | +29.7 [+10.1, +48.2] | 3.0 | +50.6 | no | -1.53%/y (-2.42) | -16.5 (-4.95) | model: yes, 5 bps: yes | no | no |
| 8fa56147ea down_streak (index_etfs): BUY when down_streak >= 4; exit when dist_sma_5 > 0 | 545 | +61.4 | +56.4 / +51.4 / +36.4 | +35.7 [+9.1, +61.2] | 3.0 | +68.8 | no | -2.22%/y (-1.83) | -25.2 (-4.58) | model: yes, 5 bps: yes | no | no |
| 64efbb8fe3 bollinger_low (sector_etfs): BUY when dist_sma_200 > 0 and bb_pctb_20 < 0; exit when dist | 746 | +60.0 | +55.0 / +50.0 / +35.0 | +35.5 [+9.1, +60.8] | 3.0 | +40.1 | no | -1.72%/y (-3.31) | -18.4 (-3.12) | model: yes, 5 bps: yes | no | no |
| 03507822a9 bollinger_low (sector_etfs): BUY when dist_sma_200 > 0 and bb_pctb_20 < 0.05; exit when d | 960 | +54.6 | +49.6 / +44.6 / +29.6 | +29.7 [+5.1, +53.0] | 3.0 | +49.5 | no | -1.75%/y (-3.04) | -20.8 (-3.89) | model: yes, 5 bps: yes | no | no |
| f37929753b calm_market_dip (sector_etfs): BUY when dist_sma_200 > 0 and vol_ratio_20_100 < 1 and rsi_2 | 1264 | +41.7 | +36.7 / +31.7 / +16.7 | +18.1 [+3.7, +31.9] | 3.0 | +47.9 | no | -1.76%/y (-3.78) | -16.6 (-4.12) | model: yes, 5 bps: yes | no | no |
| 3e27562b27 down_streak (index_etfs): BUY when down_streak >= 5; exit when ret_1 > 0 | 246 | +64.4 | +59.4 / +54.4 / +39.4 | +37.9 [+2.9, +72.1] | 1.0 | +31.5 | no | -2.16%/y (-2.83) | -14.1 (-1.94) | model: yes, 5 bps: yes | no | no |
| 3c16007aaa down_streak (index_etfs): BUY when down_streak >= 5; exit when dist_sma_5 > 0 | 244 | +83.6 | +78.6 / +73.6 / +58.6 | +56.9 [+2.0, +103.6] | 2.0 | +8.2 | yes | -2.04%/y (-2.12) | -11.6 (-1.38) | model: yes, 5 bps: yes | no | no |
| d7eb0c4283 down_streak (index_etfs): BUY when down_streak >= 3; exit when dist_sma_5 > 0 | 1224 | +49.4 | +44.4 / +39.4 / +24.4 | +23.9 [+0.3, +45.2] | 3.0 | +33.0 | no | -2.21%/y (-1.38) | -19.8 (-5.55) | model: yes, 5 bps: yes | no | no |
| 14b00b363f down_streak (sector_etfs): BUY when down_streak >= 4; exit when dist_sma_5 > 0 | 1547 | +54.4 | +49.4 / +44.4 / +29.4 | +23.1 [+0.4, +45.8] | 3.0 | +57.2 | no | -2.30%/y (-2.52) | -16.2 (-2.85) | model: yes, 5 bps: yes | no | no |

Deflated Sharpe of every candidate above: 9de5e4d602 0.0 (422 trials), 9ca928f13d 0.0 (422 trials), ed31def01d 0.0 (422 trials), 131a87e5c1 0.0 (422 trials), c8e129d06c 0.0 (422 trials), fd878d9286 0.0 (422 trials), be21113675 0.0 (422 trials), 4ec6c68526 0.0 (422 trials), 8fa56147ea 0.0 (422 trials), 64efbb8fe3 0.0 (422 trials), 03507822a9 0.0 (422 trials), f37929753b 0.0 (422 trials), 3e27562b27 0.0 (422 trials), 3c16007aaa 0.0 (422 trials), d7eb0c4283 0.0 (422 trials), 14b00b363f 0.0 (422 trials)

**Sector-relative reversal, default rule (point-in-time single stocks)**

| Candidate | Trades | Gross | Net at 5 / 10 / 25 bps | Net at model (95% CI) | Median hold | OOS gross | Veto | Excess vs passive (t) | SPY residual (t) | Net check | Excess check | Passes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| discovery window | 16597 | +19.9 | +14.9 / +9.9 / -5.1 | -13.0 [-24.5, -1.9] | 3.0 | +28.0 | yes | -2.05%/y (-16.58) | -20.5 (-8.32) | model: no, 5 bps: yes | no | no |
| 2015 to holdout (residual and excess since 2015) |  | +19.7 | +14.7 / +9.7 / -5.3 | -11.3 n/a | 3.0 | +28.0 | yes | -2.50%/y (-13.40) | -22.3 (-6.23) | model: no, 5 bps: no | no | no |
