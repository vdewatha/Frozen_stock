# Rule mutation: discovery evolves its own library (2026-11-06)

**Question.** Weekly strategy discovery tests a fixed library of rule hypotheses against a gate fixed in advance
and has never found a `proven` rule (last result: 0 proven, 2 explore). Can the bot grow the library itself,
without the search inflating its own evidence?

**Answer.** Yes, with the search charged honestly. `app/services/rule_mutation.py` adds five deterministic edit
operators on the rule language; `strategy_discovery.py` breeds up to 40 children of the near-misses per weekly
run, dedups them against every rule ever tested, evaluates them with the existing engine inside a wall-clock
budget, and charges every candidate's deflated Sharpe for the cumulative trial count (library plus every child
ever tried). One real run on the point-in-time universe (720 symbols, 2000-2026): 342 library candidates, 2
parents, 40 children, 8 children better than their parent on the discovery window, 3 children admitted as
`explore` paper candidates with their lineage. Nothing is `proven`: at 382 trials the deflated Sharpe of every
candidate is 0.0, as it was at 342. The children are forward tests, not claims of edge.

## 1. What was built

### Operators (`rule_mutation.py`)

Each operator is a pure function of a parent candidate and a seeded generator, so a (parent, operator, seed)
triple always gives the same child. A child keeps the parent's exit plan (stop, target, time stop) and family,
gets an ordinary `candidate_id`, and records `lineage = {parent_id, operator, seed, generation, root_id}`.

| Operator | Edit | Constraint |
|---|---|---|
| `shift_threshold` | one condition's threshold moves one step on its feature's grid (RSI 5, distance to SMA 1%, returns 1%, streaks 1, Bollinger %B 0.1, IBS 0.1, gap 0.25%, ATR% 0.5%, volatility ratio 0.1, calendar features 1, earnings surprise 5) | inside the grid's bounds; booleans (`at_low_N` / `at_high_N`) are never shifted |
| `add_condition` | one entry condition from a pool of 25 (one per feature and direction) | the feature is not already in the rule; `earn_surprise` only on single stocks |
| `drop_condition` | one condition removed | at least one entry condition stays; an open-timed rule keeps its gap condition |
| `swap_exit` | the indicator exit replaced by another of 8 library exits (first up close, above SMA5 / SMA10, %B mid, RSI(2) recovered, 10-day high, below SMA50, time stop only) | not for open-timed rules |
| `change_universe` | the same rule on another universe | an earnings rule stays on single stocks |

`validate_rule` runs on every child; an operator that cannot apply returns nothing and the loop tries another.
Every operator applies to more than 80% of the library and always yields a different hash
(`tests/test_rule_mutation.py`).

### Dedup and the trial count

The dedup key is `trial_hash`: the canonical rule (conditions sorted, so order is not a new trial), the exit
plan and the universe. A `discovery_trials` audit row per run lists every hash ever evaluated. The gate's
deflated Sharpe ratio is computed with `trials = |hashes ever|` for every candidate, library rows included,
because a bigger library is more trials whichever week the rows were added. A child whose hash is already in
the set is never generated again, across runs.

### The evolution step per run

1. **Parents**: the top 10 paper-executable candidates with at most one failed check, ranked by the lower bound
   of their net expectancy on the discovery window, plus every `explore` / `proven` survivor. Open-timed rules
   are not bred: their children could never be admitted either.
2. **Children**: up to 40, parents visited round-robin, operator drawn from the seeded generator; the run's seed
   is a hash of the data end date and the trial count, so a re-run of the same state reproduces the children.
3. **Evaluation** with `evaluate_candidate` (same costs, fills, universe mask), stopped at
   `strategy_discovery_mutation_budget_seconds` (default 300) with `budget_exhausted` recorded.
4. **Gate**: every candidate re-gated against the cumulative count; children that pass as `explore` or `proven`
   go through the unchanged `admit_survivors` path, with `lineage` in the admit audit row and in the strategy
   row's `parameters["discovery"]`.
5. **Retained children**: children with at most one failed check (at most 60) are stored in the trials row
   and re-evaluated on the next run as part of the library (not re-charged, as re-running a rule on more data
   is not a new trial), so they can be parents in turn. That is how a second generation arises.
6. **Stopping**: a child "improves on" its parent when it reaches a higher tier, or the same tier with a higher
   net-expectancy lower bound. `mutation.stale_runs` on the run row counts consecutive runs with no improving
   child; at 3 the next run skips mutation and resets the count, so a dead search costs one run in four.
   `strategy_discovery_mutation_enabled` (default on) turns the step off.

## 2. One real run (2026-11-06, point-in-time universe, history 2000-01-01 to 2026-11-05)

Run with the production settings against the cached history (720 symbols: today's 45 plus every past S&P 500
member with history; 13 past members have no usable file). SQLite in a scratchpad; nothing touched the paper
account.

| | |
|---|---|
| Library candidates | 342 (114 hypotheses x 3 universes, earnings family on single stocks only) |
| Trials charged to every deflated Sharpe | 382 (342 + 40 children) |
| Parents | 2 (the two `explore` survivors; no other candidate fails only one check) |
| Children generated / evaluated | 40 / 40, in 3.6 s of the 300 s budget |
| Operators used | shift_threshold 12, add_condition 17, drop_condition 4, swap_exit 7, change_universe 0 (every universe swap reproduced a library row and was deduped) |
| Children better than their parent | 8 |
| Children retained for the next run | 9 (every child that fails only the deflated Sharpe) |
| Tiers after the run | 0 proven, 11 explore (2 library, 9 children), 371 rejected |
| Admitted | 3 children (`strategy_discovery_max_new`), all `explore`, lineage recorded |
| Whole run | 1,224 s, of which 3.6 s mutation; the rest is the library over 720 frames and the earnings cache fill |

### Parents

| Id | Family / universe | Rule | Trades | Net bps (CI) | Excess CI | Holdout |
|---|---|---|---|---|---|---|
| `131a87e5c1` | calm_market_dip / index ETFs | dist_sma_200 > 0 and vol_ratio_20_100 < 1 and rsi_2 < 5; exit dist_sma_5 > 0 | 230 | 44.8 (19.7, 69.5) | (5.6, 56.6) | 36 trades, +34.9 bps |
| `c8e129d06c` | weekly_reversal / sector ETFs | dist_sma_200 > 0 and ret_5 < -0.05; exit dist_sma_5 > 0 | 313 | 64.7 (18.8, 107.1) | (2.5, 91.9) | 41 trades, +79.0 bps |

### Best child vs its parent

| | Parent `131a87e5c1` | Child `873f09a0fc` (`add_condition`: ibs < 0.2) |
|---|---|---|
| Trades (discovery window) | 230 | 160 |
| Net expectancy bps (CI) | 44.8 (19.7, 69.5) | 69.1 (42.9, 96.1) |
| Excess over drift CI | (5.6, 56.6) | (28.8, 83.2) |
| Post-2015 | positive | positive |
| Holdout (2024-11 to 2026-11) | 36 trades, +34.9 bps | 24 trades, +10.2 bps |
| Deflated Sharpe at 382 trials | 0.0 | 0.0 |
| Tier | explore | explore |

The child's lower bound is 23 bps higher on the discovery window, which is what the improvement rule measures,
and that is also where it was selected, so the number is in-sample. Its holdout is weaker than the parent's
(fewer trades, a quarter of the expectancy). The admission stays `explore`: the forward paper trades decide.

### The three admitted children

| Slug | Parent | Operator | Rule | Trades | Net bps (CI) | Holdout |
|---|---|---|---|---|---|---|
| `rule_873f09a0fc` | `131a87e5c1` | add_condition | calm dip + ibs < 0.2 | 160 | 69.1 (42.9, 96.1) | 24, +10.2 |
| `rule_9807535423` | `131a87e5c1` | shift_threshold | vol_ratio_20_100 < 0.9 | 175 | 57.2 (30.6, 82.4) | 23, +5.3 |
| `rule_e18e83c361` | `c8e129d06c` | shift_threshold | dist_sma_200 > 0.01 | 282 | 74.3 (27.9, 118.2) | 38, +102.0 |

Each strategy row carries `parameters["discovery"]["lineage"]` and the admit audit row carries the same record.

## 3. Second run on the same ledger (same data, as next week would see it)

A second run against the same SQLite ledger, to show the accounting across runs on real data.

| | Run 1 | Run 2 |
|---|---|---|
| Library evaluated | 342 | 351 (342 + the 9 retained children, re-evaluated, not re-charged) |
| Trials charged | 382 | 422 (every one of the 40 new children was a new hash; run 1's set is a subset) |
| Parents | 2 | 11 (the 2 library survivors + the 9 retained children) |
| Children evaluated | 40 in 3.6 s | 40 in 29.7 s (universe swaps now 11, since a child's rule on another universe is new) |
| Children better than their parent | 8 | 5 |
| Explore / proven | 11 / 0 | 21 / 0 (2 library rows, 19 children) |
| Retained for the next run | 9 (generation 1) | 19 (9 of generation 1, 10 of generation 2) |
| Admitted / refreshed / live | 3 / 0 / 3 | 3 (generation 2) / 3 / 6 |
| Deflated Sharpe, every candidate | 0.0 | 0.0 |
| `stale_runs` | 0 | 0 |

Best child of run 2: `61ce81d445`, `swap_exit` (time stop only) of run 1's best child `873f09a0fc`: 155 trades,
net 74.6 to 216.7 bps (CI) against the parent's 42.9 to 96.1, holdout 24 trades at +30.3 bps against +10.2.
Generation 2 of the calm-market dip: `dist_sma_200 > 0 and vol_ratio_20_100 < 1 and rsi_2 < 5 and ibs < 0.2`,
held to the 10% stop or the 10-session time stop. Its upper bound says how wide 155 trades leave the interval.

## 4. What this does and does not claim

* **No edge is claimed.** Every candidate, parent or child, fails the deflated Sharpe at 382 trials, as every
  library row did at 342. Charging the cumulative count means the bar only rises as the search continues; a
  child can reach `proven` only by a Sharpe high enough to beat the expected maximum of 382 (and next week
  422) zero-skill trials.
* **"Better than its parent" is an in-sample comparison** on the discovery window, used only to decide whether
  the search is still finding anything (the stale-run pause) and to rank. The holdout is read every run, so it
  is a sanity check, not a pristine test set, as before.
* **Children are variants of two parents.** With the deflated Sharpe failing everywhere, "at most one failed
  check" is the same set as `explore`, so the parent pool is small and the children are close relatives. The
  paper slots (`strategy_discovery_max_live`, 10) and `max_new` (3 per run) bound how many of them trade.
* **Not done:** a per-family cap on admitted children (three siblings of one parent can occupy three slots);
  crossover between parents; mutating the exit plan (stop, target, time stop) itself; a template for the
  experiment engine to test a child against its parent on forward paper trades rather than on the backtest.

## 5. Settings, rows, tests

Settings: `strategy_discovery_mutation_enabled` (True), `strategy_discovery_mutation_budget_seconds` (300).
Constants in `strategy_discovery.py`: `MUTATION_PARENTS` 10, `MUTATION_CHILDREN` 40, `MUTATION_STALE_RUNS` 3,
`RETAINED_MAX` 60.

Audit rows (no migration): `discovery_trials` / `run` (hashes ever, count, retained children, run number) once
per run; the `strategy_discovery` / `run` payload gains `library`, `mutation` (parents, children generated /
evaluated / improved, operators, best child vs parent, budget, elapsed, `stale_runs`, `trials_cumulative`) and
`lineage` on the top rows; `strategy_discovery` / `admit` gains `lineage`.

Tests: `tests/test_rule_mutation.py` (13): every operator yields a valid rule with a new hash; determinism,
grids, family and open-timed constraints; dedup against seen hashes and within a batch; parent selection;
budget cut-off with a fake clock; the cumulative count reaching the deflated Sharpe; two scheduled runs
accumulating trials with no child retried; the stale-run pause and the setting; lineage in the admission row.
Full backend suite: 2746 passed, 33 skipped.
