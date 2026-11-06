# Exit autopsy: 2026-11-06

Roadmap item 2 of `self-evolution-roadmap-20261106.md`. **Question:** the bot explains how trades were
entered; can it also learn from how they were exited, and test a different stop or time stop itself?

Until now `trade_mistakes.premature_stop` could never fire: `post_exit_return` was declared but nothing
computed it (learning-loop review, 2026-10-27). The exit geometry was also fixed in the replay (`BRACKET`,
`REVERSION_BARRIER`), so the experiment engine could only turn entry gates.

## What was added

1. **Post-exit path on every closed trade** (`trade_attribution.py`, `trade_mistakes.py`). Each attribution
   record now carries `post_exit`: the return from the exit price to the planned horizon
   (`max_holding_sessions` sessions after the entry session; the row's parameter, else the strategy's exit
   policy), the best and worst excursion over that span (`mfe`, `mae`, from the exit price), the number of
   daily bars seen (`bars`) and whether the horizon bar was among them (`horizon_reached`). A second key,
   `excursion`, holds the hold's own best and worst from the entry price. Both come from stored daily bars
   (`market_prices`, raw high/low/close). A trade whose horizon session is not over records `None` and is filled
   by a later revision once the bars exist, through the existing append-only revision mechanism (same
   `source_key`, new `revision`, only when the content changed). Trial lots get the same path through
   `trial_lot_trades`, and the classifier's `premature_stop` mistake now receives a real `post_exit_return`
   (round trips too; the old one-day lookahead remains only as a fallback when the path is not available).
2. **Three exit flags** (`loss_diagnosis.py`, `loss-diagnosis-v2`). Contributing flags next to the primary
   cause, which is unchanged:
   * `premature_stop`: a stop exit, and the post-exit high got back to the entry price before the horizon.
   * `gave_back_gains`: the hold's best gain was at least 1% and the trade closed below half of it.
   * `held_too_long`: a time-stop exit whose best gain had reached the target (the policy's take-profit, or 3%
     for RSI(2) / IBS, which have none) and that gain was never taken.
   Each has a proposal in `CONTRIBUTING_PROPOSALS`, so `repeating_failures` carries them into lessons.
3. **Exit templates in the experiment engine** (`experiment_engine.py`, `gate_replay.py`).
   * `stop_width` (trigger `premature_stop`): 2% / 3% / 5% for the technical strategies, 7% / 10% / 15% for
     RSI(2) and IBS.
   * `time_stop` (triggers `held_too_long`, `gave_back_gains`): 10 / 20 / 30 sessions for the technical
     strategies, 5 / 10 / 15 for the reversion pair.
   The replay builds its `BarrierConfig` from the setup (`barrier_for`): the strategy's fixed barrier,
   overridden key by key by `stop_loss_pct` / `take_profit_pct` / `max_holding_sessions`. The baseline is the
   strategy's live exit policy (`current_setup(parameters, strategy_type)`), so a setup equal to the current
   barrier replays identically to before (parity-tested). The decision rule (`RULE` / `decide`) is untouched:
   every window, at least 30 trades, expectancy and total profit not lower, drawdown at most 1.10x.
4. **Governed adoption through the exit policy.** An adopted stop or time stop is written to the strategy row
   as `stop_loss_pct` / `max_holding_sessions` with the `strategy_governance` action `exit_policy_update`;
   `BaseStrategy.exit_policy()` reads those overrides (the existing mechanism; `BACKTESTED_BRACKET_EXIT_POLICY`
   and `SHORT_TERM_REVERSION_EXIT_POLICY` stay the class defaults). The forward verdict and automatic revert
   work as for the gates: `not_helping` removes the override and the class default returns.

Tests: `tests/test_exit_autopsy.py` (15): the path on synthetic bars (horizon reached, not reached, at the
horizon, pending), the three flags, an attribution that is filled on a later revision, a trial lot, the
templates in `plan_experiments`, replay parity, adoption writing the exit policy and the revert, and the
reversion pair's own candidates. `tests/test_experiment_engine.py` updated for the new setup keys.

## What is still manual, and what to watch

* **No gate-cost-style lesson for exits yet.** An exit flag reaches the engine only as a contributing flag of
  a repeating primary cause (3+ losses, 40% of losses in the window), the same way `high_vix_entry` and
  `market_down` do. A strategy whose stops are premature but whose losses have no single repeating cause is
  reported, not experimented on. A standalone "exit cost" audit (like `gate_cost.py`) would turn the flags
  themselves into lessons.
* **Open lots and the time stop.** Broker legs of open lots are left as they were opened; a new stop applies to
  entries after the change. The time stop, however, is evaluated live from the row's policy
  (`evaluate_policy_exit`), so a shorter time stop closes already-open lots held past the new limit at their
  next evaluation. The verdict counts only trades opened after the change, so this does not bias it. The exit
  re-arm (`stock_exit_rearm.py`) also uses the current policy when it has to replace missing legs.
* **Daily bars only.** `post_exit` and `excursion` use daily highs and lows, so an entry-day high before the fill
  counts toward the hold's MFE, and an intraday recovery that reversed by the close still counts as a recovery.
  Trial lots keep their 1-minute excursions when those bars exist.
* **The target is only replayed, not tested.** `take_profit_pct` is part of the setup and the replay honours it,
  but no template varies it; add one when `held_too_long` repeats on a strategy with a target.
* **The 3% stand-in target** for strategies without a take-profit is a fixed constant (`HELD_TOO_LONG_MFE`),
  not derived from their volatility.
* **First run after deploy** re-records every attribution in the 120-day lookback as a new revision (the record
  gained keys); that is the append-only contract working as designed, not a change of evidence.
