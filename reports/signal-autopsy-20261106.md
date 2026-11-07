# Signal autopsy, 2026-11-06

Branch `claude/eloquent-elgamal-74a0de`, a step of `self-evolution-roadmap-20261106.md`.
**Question:** `signal_wrong` (the stock fell on its own, beyond the market and its sector) is the largest loss
cause in the paper ledger (7 of the first 20 real losses) and the only repeating cause without an experiment
template. Its lesson text said "compare the entry features of these losses with this strategy's wins; narrow
its symbols or regimes". Can the bot do that comparison itself, turn what it finds into a pre-registered
experiment, and apply the result only through the engine's unchanged rule?

**Goal used:** total profit after costs, as everywhere in the engine. The autopsy supplies candidates, never
decisions; a scope reaches a strategy only when `experiment_engine.decide` passes it in every window.

## What was built

`app/services/signal_autopsy.py` and `app/services/strategies/scope.py`, paper only; nothing here places,
sizes or blocks an order directly.

1. **Features at the decision bar.** For every closed paper trade of a strategy with at least 10 closed
   trades (`trade_attribution.current_attributions`, scheduled and exploration round trips), the entry-time
   features are recomputed from the stored daily bars ending on the decision bar (the last completed session
   before the entry session), with the same `rules.feature` functions the discovered rules and the live scope
   use: `rsi_2`, `rsi_14`, `dist_sma_200`, `dist_sma_50`, `atr_pct_14`, `gap`, `vol_ratio_20_100`, `ibs`,
   `bb_pctb_20`, `down_streak`, `dow`. The bars are scaled by `adjusted_close / close` exactly as the paper
   frame is (`trusted_data.trusted_history`), so a feature here is the number the strategy saw. Entry VIX and
   breadth come from the attribution's diagnosis (`entry_vix`, `entry_breadth`).
2. **Contrast.** Per feature, losses against wins: difference in means, Cohen's d, and a two-sided permutation
   p-value on the difference in means (2,000 seeded label shuffles), corrected across the 13 features with
   Benjamini-Hochberg (`q_value`). A feature with fewer than two finite values on a side is reported, not
   tested, and left out of the family.
3. **Scope candidates.** A rule feature with q < 0.10 and at least 10 trades on each side becomes a candidate
   scope rule: a one-condition threshold at the median of the winners, in the winners' direction
   (`rsi_2 < 9.8` when winners had the lower RSI(2), `dist_sma_200 > 0.012` when they sat higher above the
   average), at most five per strategy, strongest effect first. VIX and breadth are contrasted and reported but
   never become a scope rule (the VIX gate template owns the VIX; a breadth gate is another session's
   template).
4. **Ledger.** One `signal_autopsy` audit row per strategy per run (event `stock_paper`, no migration): the
   table, the candidates, the trade records (symbol, entry session, decision bar, result, cause), the method.
   `GET /stock-paper/signal-autopsy` (viewer) shows the latest row per strategy and the scopes in force.
5. **The `scope` template** (`experiment_engine.TEMPLATES`, its own block at the end, kind `scope`,
   triggered by `signal_wrong`). `Template` gained `dynamic`: a callable that returns the candidates given the
   strategy row; the static templates are unchanged (`candidates_for(strategy_type)` still answers for them).
   The scope template's candidates are the latest autopsy row's rules for that strategy, scope off first, so a
   scope that costs profit is removed by the same rule that added it. A lesson whose only template has no
   candidates (no autopsy yet, or nothing separated) is reported under `untemplated` with that reason and
   listed in `skipped`; a lesson another template acted on is not. `RULE` and `decide` are untouched: a
   candidate that keeps fewer than 30 trades in any window fails the rule as before.
6. **Replay.** `gate_replay.simulate` reads the setup's `scope` key (validated by `strategies/scope
   .scope_condition`) and ANDs the condition, computed with `rules.feature` on the same bars, into the entry
   mask. `setup_from_parameters` and the gate/exit keys are unchanged; `experiment_engine.current_setup` adds
   `scope` only when the row has one, so a live scope is the baseline it is judged against.
7. **Adoption and the live path.** An adopted candidate is written as `parameters["scope"]`
   (`{"f": "rsi_2", "op": "<", "v": 9.8}`, the `rules.py` condition format) through the governance action
   `scope_update`, source `experiment_engine`, with the same forward verdict and revert as every other change.
   `strategies/registry.get_strategy` wraps every strategy (registry slugs and discovered rules) with
   `scope.apply_scope`, the way `vix_gate.apply_vix_gate` does: a BUY whose decision bar fails the condition
   becomes a HOLD (`gated_action: "BUY"`, the value and the condition in `features["scope"]`); SELLs, HOLDs
   and exits are untouched. A value the frame cannot supply fails the condition, as a rule condition does,
   flagged `scope_unavailable`. An invalid scope parameter is ignored and logged, never applied half-way.
   `signal_autopsy.scope_decided_by_engine(db, strategy_id)` is the seed-style override check: a one-time seed
   step that wants to set a scope must find the engine's `scope_update` row and leave it.
8. **Schedule.** `signal_autopsy_job`, Sunday 11:30 New York on the `learning` queue, before the experiment
   engine's noon run, so the Sunday experiments read that morning's tables.

## Pre-registered rule

Unchanged (`experiment_engine.RULE`): adopt a candidate only if, in every window (all, 2015+, 2020+), it keeps
at least 30 trades, net expectancy per trade is not lower, total net profit is not lower and portfolio max
drawdown is at most 1.10x the current setup's; among eligible candidates take the largest minimum expectancy
gain; none eligible means no change. The autopsy's own thresholds (q < 0.10, 10 per side, winners' median)
only decide what gets replayed, never what gets adopted.

## First autopsy tables

None yet. No local copy of the database (`artifacts/api-server/backend/trading_app.db`, `backend/trading_app.db`,
`trading_app.db`) holds a `stock_trade_attributions` table or the daily bars the features need; the attributed
trades live in the production Postgres. The first tables will be the `signal_autopsy` audit rows written by the
first Sunday run after deploy (or `signal_autopsy.record_autopsies(db)` by hand), readable at
`GET /stock-paper/signal-autopsy`. Each row carries, per feature: `n_losses`, `n_wins`, `mean_losses`,
`mean_wins`, `median_wins`, `diff`, `effect_size`, `p_value`, `q_value`, and the candidate list with its
`why` line. The synthetic end-to-end tests exercise the whole path (bars in the ledger, twelve attributed
trades, one audit row, the view, the engine planning and adopting a candidate, the live wrapper holding a BUY).

## Tests

`tests/test_signal_autopsy.py` (17): features at the decision bar including the adjustment factor and missing
history, the permutation test and the correction on synthetic data, candidate derivation and direction, the
autopsy payload, the ledger end to end, the dynamic candidates (off first, live scope excluded, test doubles),
the template shape, planning with and without candidates, the replay mask (and an invalid scope ignored), the
live wrapper (BUY to HOLD, SELL untouched, fail closed when unavailable, invalid scopes ignored, applied through
the registry to every strategy), adoption through governance with the override check, the live path, revert,
keeping a live scope as the baseline, and the job, route, queue and beat wiring. One assertion in
`tests/test_experiment_engine.py` changed: `signal_wrong` now maps to the `scope` template. Full backend suite
run before the PR (see the PR body for the count).

## Not done, and what to watch

* The autopsy contrasts all losses against all wins, not only `signal_wrong` losses: the scope narrows the
  entries, whatever the loss cause, and the per-cause split is in the trade records for a later cut.
* A scope with two conditions, or a threshold searched rather than taken at the winners' median, is deliberately
  out: one condition at a median is the smallest hypothesis the data can suggest, and the rule decides.
* The scope is not shadowed by `setup_shadow` (it shadows the gate templates only); an adopted scope earns its
  verdict from the live trades after the change, as the exit templates do.
* `decision_bar` is the last stored bar before the entry session. A trade entered from a decision made on a
  stale frame (a missed import) will have its features computed on a later bar than the one the strategy used;
  the record carries the bar so such cases can be seen.
