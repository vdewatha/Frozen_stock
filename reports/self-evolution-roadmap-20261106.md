# Self-evolution roadmap: 2026-11-06

Branch `feat/experiment-engine`. **Question:** what does the bot need in order to be self-sufficient,
self-evolving and self-analysing: to improve how it trades on its own, to explain every failed trade, to decide
what to do differently, and to make that change itself?

## 1. What the bot can already do

| Ability | Where | State |
|---|---|---|
| Explain every closed paper trade (cause, contributing flags) | `loss_diagnosis.py`, `trade_attribution.py` | live, runs every 30 min in session |
| Spot a failure that keeps repeating, with a lesson and a proposal | `loss_diagnosis.repeating_failures` | live, proposals only (`auto_apply: False`) |
| Judge whether the bot is improving (health, progress, changes) | `learning_scoreboard.py`, `weekly_report.py` | live |
| Pause a strategy whose edge decays; re-admit only through a fresh governed cycle | `strategy_edge_decay.py`, `strategy_readmission.py` | live |
| Retrain the BUY filter on its own trades | `meta_label_learning.py` | live, nightly; unproven out of sample |
| Search for new rule strategies against a fixed gate | `strategy_discovery.py` | live, weekly; 0 proven, 2 explore |
| Tune strategy parameters from the loss agenda through a governed cycle | `parameter_application.py`, `experiments.py` | live, nightly |
| Entry protections: market gate, VIX gate, ADX gate, stop cooldown, earnings and announcement blackouts, one position per symbol | `entry_protections.py`, `strategies/*_gate.py` | live |

## 2. The gap

Every behaviour change of the last two weeks followed the same path: the loss diagnosis named a repeating cause
(`missed_market_move` 14 of 26 losses; `clustered_stops` 23 of 34 trades), a person wrote a backtest with a
decision rule fixed before the run (`market-gate-20261106.md`, `vix-gate-20261106.md`,
`slot-allocation-20261106.md`), and a one-time seed step applied the result. The bot produced the lesson and
honoured the change, but **the step in between was manual**. That step is the self-evolving part.

A second gap: the bot shipped changes but did not keep its own record of what it changed, what it expected,
and whether the change worked. The scoreboard's change list was a constant in code.

## 3. What others do (checked 2026-11-06)

* **Microsoft RD-Agent(Q)** (NeurIPS 2025, with Qlib): a research stage forms a hypothesis, a development
  stage implements it, a backtest scores it, and the result feeds the next hypothesis. Its value is the
  closed loop with a record of what was tried, not any one factor.
  [arXiv 2505.15155](https://arxiv.org/pdf/2505.15155v2), [microsoft/RD-Agent](https://github.com/microsoft/RD-Agent).
* **EvolveTrade** ("experience-driven policy refinement for self-evolving LLM trading agents") and
  **FinMem** (layered memory with reflection): both keep a memory of past trades and refine the policy from
  reflections on them. [EvolveTrade](https://arxiv.org/pdf/2609.17632),
  [FinMem code](https://github.com/pipiku915/finmem-llm-stocktrading).
* **AlgoEvolve / QuantaAlpha / Chain-of-Alpha**: an LLM mutates strategy programs or alpha expressions and an
  evolutionary loop keeps what backtests best. Reported Sharpe ratios are not independently verified; the
  search itself inflates them. [AlgoEvolve](https://arxiv.org/pdf/2606.26173),
  [QuantaAlpha](https://arxiv.org/html/2602.07085v1).
* **"What survives honest evaluation?"** (2026 preprint): a single pre-registered hypothesis needs little
  statistical deflation; a search over many variants must pay for the search.
  [arXiv 2608.27734](https://arxiv.org/pdf/2608.27734). Our discovery gate's deflated Sharpe already does this.
* **freqtrade FreqAI** retrains on a schedule and gates predictions on out-of-distribution data; its
  protections are already re-implemented here.

The common shape is: **hypothesis from evidence, experiment against a fixed bar, keep a record, feed the
record back.** None of these systems verifies a change on forward trades and reverts it; that is the piece we
add because paper trading gives us real forward evidence.

## 4. What shipped here: the experiment engine

`app/services/experiment_engine.py` closes the loop for the lessons the bot can act on.

1. **Lesson -> experiment.** Each repeating cause or contributing flag maps to a template with a knob and the
   values to try. `missed_market_move` / `market_down` test the SPY-trend gate (off, 10, 20, 50);
   `high_vix_entry` tests the VIX gate (off, below 20 / 25 / 30); `missed_earnings` tests the earnings blackout
   (off, 10, 20 sessions). The account-wide lesson (`all`) expands to every active replayable strategy. A lesson
   with no template (`signal_wrong`, `news_wrong`, `clustered_stops`, `missed_sector_move`, `weak_breadth_entry`)
   is reported with its proposal, not acted on.
2. **Replay** (`app/services/gate_replay.py`, the engine moved out of `scripts/run_market_gate_backtest.py`):
   each strategy as it trades on paper, under its *current* setup (its live gates) and under each candidate.
   New in the replay: the VIX gate and the earnings blackout, both decided on the close and filled at the next
   open like the live rules. The script still works and uses the service.
3. **Pre-registered decision rule**, fixed in the module before any run and recorded in every ledger row:
   adopt a candidate only if, in every window (all history, 2015+, 2020+), it keeps at least 30 trades, its net
   expectancy per trade is not lower, its total net profit is not lower, and its portfolio max drawdown is at most
   1.10x the current setup's. Among eligible candidates take the largest minimum expectancy gain. Turning a
   gate off is always a candidate, so a gate that costs profit can be removed by the same rule that added it.
4. **Governed adoption.** The winning value is written to the strategy row's parameters with the same
   `strategy_governance` audit action the one-time seed steps check (`market_gate_update`, `vix_gate_update`,
   `earnings_blackout_update`), so a seed pass never overrides the bot's decision. The earnings blackout is now
   per strategy (`earnings_blackout_sessions` on the row, account-wide setting as the default).
5. **Ledger with no migration.** Every run, experiment, adoption and verdict is an `experiment_engine` audit row
   with the full before/after tables. `GET /stock-paper/learning-experiments` (viewer) shows the rule, the
   templates, every change with its state, and the latest run. The learning scoreboard's `changes` now includes
   the bot's own changes, each judged on its strategy's trades before and after.
6. **Forward verdict with automatic revert.** After 20 closed paper trades opened after the change
   (`experiment_verdict_min_trades`), the realised expectancy and the share of losses with the triggering cause
   are compared with the 20 trades before. `working` keeps it; `not_helping` (expectancy at least 10 bps worse
   and the cause no rarer) reverts the parameters with an audit trail; a mixed result keeps watching up to 60
   trades, then closes as `inconclusive`.
7. **Pace.** A (strategy, template) pair is not re-run within 28 days unless its lesson grew by 3 losses; at
   most 4 experiments per run. The engine runs Sundays at 12:00 New York time on the learning queue, after
   discovery; verdicts run weekdays at 17:10. Both are paper only and never touch orders or exits.

Settings: `experiment_engine_enabled`, `experiment_engine_adoption_enabled`, `experiment_engine_max_experiments`,
`experiment_engine_cooldown_days`, `experiment_engine_history_start`, `experiment_verdict_min_trades`,
`experiment_verdict_max_trades`. Tests: `tests/test_experiment_engine.py` (19).

### What this does not do

* It cannot test lessons without a template. The biggest one today is `signal_wrong` (7 of the first 20
  losses); the nightly parameter research covers it, and a signal autopsy is next (below).
* The replay universe is today's symbol list (survivorship), the three windows overlap, and every gated CI
  overlaps its ungated CI, as in the hand-run reports. The rule is "no evidence against, and the mechanism
  makes sense", and the forward verdict is what decides.
* The weekly run re-downloads the 45 symbols' history (the discovery job does too). On the 2 vCPU host expect
  a few minutes per run.

## 5. Roadmap: what to add next, in order

1. **Gate cost audit.** Every blocked BUY (`buy_signal_outcomes`) is followed up with the return it would have
   had, so a gate that blocks winners gets a measured cost. Gates then earn a verdict on what they stopped, not
   only on what they let through. Builds on `meta_label_shadow` and the blocked-reason counts in the weekly
   report.
2. **Exit autopsy.** Compute `post_exit_return` for every closed trade (price path to the planned horizon after
   the exit), which `trade_mistakes.premature_stop` already needs. New templates: stop width and time stop per
   strategy, replayed through `triple_barrier` with the same rule.
3. **Signal autopsy for `signal_wrong`.** Contrast the entry-time features of a strategy's losses with its
   wins (RSI, distance to the 200-day average, ATR%, gap, VIX, breadth, time of day; all in `strategies/rules.py`)
   with a permutation test. A feature that separates them becomes a scope-narrowing candidate for the engine
   (for example "RSI(2) only when ATR% < 3"), tested under the same rule.
4. **Sector and breadth gates** as live protections, so `missed_sector_move` and `weak_breadth_entry` get
   templates. The market gate generalises to a sector ETF's average; the breadth gauge already exists.
5. **Evolutionary mutation in strategy discovery.** Mutate the near-passing candidates (shift a threshold,
   add or drop one condition) and re-test, with the trial count feeding the deflated Sharpe so the search pays
   for itself. This is the AlgoEvolve idea without an LLM.
6. **Drift-triggered retraining.** A `model.*` monitoring breach enqueues a challenger cycle instead of waiting
   for the daily schedule (`stock_monitoring` -> `create_learning_cycle`).
7. **LLM hypothesis writer (optional, costs API calls).** Give a model the weekly report, the repeating
   failures and the rule language, and have it propose new candidate rules into the discovery library. The
   fixed gate and the forward trial keep it honest. Only worth it once items 1 to 5 produce fewer ideas than the
   bot can test.

Each of these ends in a ledger row and a verdict through the same engine, so the bot's record of what it tried
grows with it.
