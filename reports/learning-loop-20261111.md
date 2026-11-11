# Learning loop: 2026-11-11

**Question.** The bot diagnoses every closed trade. Has a diagnosis ever become a tested change?

**Answer: no, and this change makes it possible.** The experiment engine's ledger was empty: no experiment and no adopted change, ever. It had run three times, on 2026-11-10 and 11-11. Each run found 3 lessons and 15 candidate experiments, skipped 14 of them and failed on the last. The cause was structural: the engine tested only actively trading strategies, and since the slow book there are none it may change. It now tests the shadow strategies too. A local run on real history completed four experiments and adopted none of them. Paper only.

Code: `app/services/experiment_engine.py` (module docstring, point 7), `app/services/strategy_scorecard.py`, `app/services/point_in_time_universe.py`, `app/tasks/jobs.py`, `app/tasks/catchup.py`. The end-to-end script and its output are in [research_notes/Learning loop 20261111](../research_notes/Learning%20loop%2020261111/).

## What was wrong

The latest production run (2026-11-10 21:16 ET) is typical of all three.

| Candidates | Outcome | Reason the engine gave | What it really was |
|---|---|---|---|
| 10 | skipped | "strategy cannot be replayed over daily history" | The five rule rows are the frozen forward experiments 1-5. They must never be changed, so the skip was right for the wrong reason. |
| 4 | skipped | "not an active strategy" | Channel Breakout is in shadow and Model Predictive Long is paused. These are the strategies whose losses produced the lessons. |
| 1 | failed | "no price history to replay" | The book-level entry cap, drawn from 14 clustered stop-outs. Nothing about the history load was recorded. |

* **Nothing was left to learn on.** [The slow book](slow-book-20261107.md) moved every hand-written strategy to `shadow`. The only rows still in a trading status are the five frozen rule rows. The engine read "active strategies" as its whole world, so that world held nothing it could act on.
* **The book experiment would have been empty even without the failure.** It replays "every active replayable strategy", and there are none.
* **Each failed run counted as the weekly run.** The job returned `complete`, so the catch-up never retried it.
* **Why the history load failed is still unknown.** The same code loads 628 of 630 symbols locally in 20 seconds from a warm cache, with SPY among them. In production all three runs lost SPY or every symbol, two of them within seconds, and no warning was logged. A cached file with no bars is the one path that drops a symbol silently, and it fits. I could not confirm it from outside the deployment.

## What changed

* **The engine tests shadow strategies.** They place no orders, so an adopted change only alters the signals they record. Two limits apply:
  * **Entry gates only.** A gate change has forward evidence without trades: the paired setup-shadow decisions (`setup_shadow.py`). An exit or scope change on a shadow strategy could never be judged, so it is skipped and the reason says so.
  * **Frozen forward experiments are never touched.** A row bound to one (`forward_experiments.is_registered_row`) is left out of the learning set and skipped by name. That covers the five rule rows and the IBS tag row.
* **A shadow strategy's change gets a real verdict.** The fixed 20-trade rule would wait forever, because a shadow strategy closes no trades. For those changes the always-valid e-process judge acts on the paired shadow decisions. It confirms, reverts, or closes at its 120-trade cap. Changes on trading strategies keep the judge that `experiment_verdict_method` names.
* **Book templates replay the same learning set**, which is the strategies whose trades taught the lesson.
* **The history load explains itself.**
  * The run row carries `history`: symbols requested and loaded, whether SPY loaded, and why each dropped symbol was dropped. The error message repeats the summary.
  * A lost benchmark is retried once with a fresh download.
* **The shared daily-bar cache is safer.**
  * Writes replace the file in one step. On 2026-11-10 six histories were read mid-write and failed to parse.
  * A refresh that fails keeps the bars already cached.
  * An empty download is a clear error, and a cached file with no bars is fetched again instead of trusted for a week.
* **A run where nothing could run is not a run.** The job reports `deferred`, so the weekday after-close catch-up tries once more.
* **One run this week.** A one-off catch-up runs the engine on the widened set after a close from 2026-11-12, instead of waiting for Sunday 11-15.
* **The run row lists its learning set**: active, shadow and frozen strategy types.

Moving a shadow strategy back to trading stays a governed decision outside the engine.

## The first real run (local, real history)

The script builds a throwaway database with six shadow strategies, one frozen row and 14 clustered market-down losses, then runs the engine unfaked.

* **Universe:** point in time, 628 symbols since 2000. The two dropped symbols have about 110 bars each.
* **Cost:** 228 seconds on a warm cache, with a peak of 1.6 GB of memory. A cold cache adds about 7 minutes of downloads.
* **Result:** four experiments ran, three more were deferred to the next run by the budget of four, and nothing was adopted.

| Experiment | Current setup, net per trade | Best candidate | Why it was not adopted |
|---|---|---|---|
| Book entry cap | −26.6 bps, drawdown 94% | cap of 2 new entries a session: −8.1 bps, drawdown 60% | Short-hold veto: +14.7 bps gross out of sample, 30 required |
| Channel Breakout market gate | −20.7 bps | SPY above its 20-day mean: −16.0 bps | Short-hold veto: +1.6 bps gross, 30 required |
| Bollinger market gate | −11.6 bps | none | Every gate lowers expectancy in 2015+ |
| Ensemble market gate | −22.1 bps | none | Every gate lowers expectancy |

Two things follow.

* **The loop closes.** A lesson became a recorded, pre-registered test with a decision. These are the engine's first four experiments.
* **The answer matches the earlier research.** Gates and caps make these strategies lose less. They do not make them earn. The entry cap cuts the replayed loss per trade by about two thirds and the rule still rejects it, because the short-hold veto asks for a real gross edge and there is none. Whether to set that cap by hand as a loss limit on exploration is a risk decision for the owner.

## How winning trades feed learning

* **Lessons come from losses only.** `loss_diagnosis.repeating_failures` counts repeating causes among losing trades. Wins are diagnosed (`edge`, `market_tailwind`) but never raise a lesson of their own.
* **Wins are used in three indirect ways:**
  * The signal autopsy compares wins with losses on their decision-bar features and proposes the entry conditions that separate them (the `scope` template).
  * The gate-cost audit scores the BUYs a gate withheld, so a gate that blocks winners becomes a lesson to remove it.
  * The setup shadow scores every gate value on the same BUY decisions.
* **A gap remains.** The `scope` template is the nearest thing to "do more of what wins", and it is not tested on shadow strategies, because nothing scores scope alternatives forward without trades. Recording scope alternatives in the setup shadow would close it.

## Limits

* **Production may still fail to load history.** If it does, the run row now says why, and the next step follows from that: a bars snapshot, or the database's own daily bars as a fallback.
* **Memory.** A point-in-time run peaks near 1.6 GB on the deployed worker. Strategy discovery was killed by the memory guard on that universe. The engine's setting is unchanged here, because the point-in-time universe was adopted by a pre-registered rule.
* **An adopted entry cap would also limit the forward experiments' entries**, since it is a book-wide setting. Nothing adopts one today.
* **Paused strategies are still skipped**, with the status in the reason.
* **The local run used empty strategy parameters**, not the live rows' adopted gates, so its numbers are a smoke test of the path, not the production verdict.

## Reproduce

```
META_LABEL_HISTORY_DIR=<cache dir> PYTHONPATH=. python "../../../research_notes/Learning loop 20261111/engine_e2e.py"
```

Run it from `artifacts/api-server/backend`. It never contacts the deployed app.
