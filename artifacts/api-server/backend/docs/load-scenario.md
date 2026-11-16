# Load scenario

The production freezes came from several things happening at once: a long
research job, the minutely import, the broker reconciliation and a worker
restart. Each has tests of its own; nothing measured them together. This
scenario runs them together on one machine and writes the numbers down:
queue delay, lock waits, memory per process and recovery time.

It measures. It sets no pass mark, is not a required check, and its report
says which measurements are missing instead of reporting them as clean.

## What it runs

Like the [paper soak](autonomous-paper-soak.md) it uses a disposable
PostgreSQL schema and its own non-persistent Redis. Unlike the soak it starts
no beat, API or watchdog: the driver publishes the workload itself on a fixed
timetable, so two runs do the same work.

The four workers are the hosted layout of `src/production-server.ts`
(`paper-core`, `paper-fast`, `paper-intraday`, `paper-broker`: thread pools,
prefetch 1). A test keeps the queue lists equal to that file.

| Workload | Queue | What runs |
| --- | --- | --- |
| Heavy research | `learning` | The real experiment sweep (`run_strategy_experiments`) over generated daily history, several sweeps per job, under a real learning job's advisory lock |
| Intraday import | `intraday_market_data` | The real `ingest_intraday` for one simulated session minute per tick, with its Redis lease, fed by a generated minute-bar feed |
| Broker reconciliation | `broker_reconciliation` | The real `reconcile_stock_paper_account` and exit re-arm against an in-memory paper broker; it holds the paper account row for the whole pass |
| Signal pass | `paper_trading` | Takes the paper account row the way an order reservation does, with a lock timeout; it reserves nothing |
| Probes | `learning_fast`, `market_data`, `research_market_data` | One short database round trip each, to time queues the scenario does not otherwise load |
| Recovery drill | | `SIGKILL` to one worker part-way through; a supervisor restarts it after the production pause of 2 seconds |

Cadences are compressed (an import every 5 seconds stands for every minute),
and each stream keeps production's ratio of expiry to cadence, so a tick that
waited past the next one is dropped as it would be in production.

### The fakes

`scripts/load_scenario_tasks.py` replaces the two functions every intraday
fetch goes through with a bar generator, and hands the reconciliation an
in-memory broker that reports a flat account and refuses orders. The runner
also blanks every provider credential and points the proxy variables at a port
nothing listens on, so a call that slipped past a fake fails on this machine.

The generated daily bars carry a trusted provenance label, because the
research code refuses anything else. That is why the tasks refuse to load
outside the runner, the schema must be named `load_scenario_*`, and the schema
is dropped when the run ends.

The runner refuses a `DATABASE_URL` that is not on this machine. Load never
goes to the deployed app's database, even in a schema of its own.

## Run it

From `artifacts/api-server/backend`, with a local PostgreSQL and
`redis-server` on the path:

```bash
DATABASE_URL=postgresql://user@127.0.0.1:5432/scratch \
LOAD_REPORT=reports/load-scenario-$(date -u +%Y%m%dT%H%M%SZ).json \
bash scripts/run_load_scenario.sh
```

A default run takes about four minutes: 180 seconds of load, 15 seconds to
drain, and setup. Run it twice and compare:

```bash
python3.11 scripts/report_load_scenario.py --compare first.json second.json
```

The `Load scenario` workflow does the same on GitHub when started by hand
(`workflow_dispatch`), with PostgreSQL as a service container. It uploads the
reports and worker logs and is not part of CI.

| Variable | Default | Meaning |
| --- | --- | --- |
| `LOAD_DURATION_SECONDS` | 180 | Seconds of load (60 or more) |
| `LOAD_RESEARCH_JOBS` | 10 | Research jobs queued at the start |
| `LOAD_RESEARCH_SWEEPS` | 12 | Experiment sweeps per research job |
| `LOAD_RESEARCH_BALLAST_MB` | 0 | Extra memory each research job holds while it runs, to stand in for a production-sized job |
| `LOAD_INTRADAY_INTERVAL_SECONDS` | 5 | Seconds between imports |
| `LOAD_INTRADAY_SYMBOLS` | 10 | Symbols per import |
| `LOAD_RECONCILE_INTERVAL_SECONDS` | 10 | Seconds between reconciliations |
| `LOAD_BROKER_LATENCY_MS` | 150 | Delay of each fake broker call |
| `LOAD_FEED_LATENCY_MS` | 100 | Delay of each fake feed call |
| `LOAD_SIGNAL_LOCK_TIMEOUT_MS` | 3000 | Lock timeout of the signal pass |
| `LOAD_KILL_WORKER` | `paper-core` | Worker the drill kills |
| `LOAD_KILL_AFTER_SECONDS` | 60 | When it is killed; 0 skips the drill |
| `LOAD_KEEP_SCHEMA` | false | Keep the schema for inspection |
| `LOAD_RUN_DIR` | a temporary directory | Raw samples, events and worker logs |

## Read the report

`queue_delay` has one entry per queue: seconds from publish to `task_prerun`
(`p50`, `p95`, `max`). The publisher stamps each task and the worker's
`task_prerun` hook subtracts. `never_started` and
`oldest_unstarted_wait_seconds` cover tasks still queued when the run ended; a
queue whose tasks never ran has no delay figures, not small ones.
`expired_before_start` counts ticks dropped by their expiry.

`lock_waits` comes from two sources. Once a second the driver reads
`pg_stat_activity` for the scenario's sessions waiting on a lock
(`samples_with_a_waiting_session`, `max_waiting_sessions`, `by_wait_event`).
`longest_wait_seconds` is how long the waiting statement had been running when
sampled, an upper bound on the wait. `sqlstate_counts` counts every `55P03`
(lock not available) and `40P01` (deadlock detected) the workers raised,
including ones a job caught and retried; `postgres_deadlocks` is the server's
own counter over the run.

`memory` is memory per process, sampled once a second. On Linux it is resident
memory from `app.core.memory.resident_mb` (`memory.source` is `proc`). macOS
has no `/proc` and compresses idle pages, so its resident figure understates
what a process holds; there the driver reads the physical footprint, the
figure Activity Monitor shows (`macos_phys_footprint`), and falls back to `ps`.
A macOS figure is not directly comparable with the hosted Linux VM.

`recovery` times the drill from the kill: `process_restarted_seconds` (the
supervisor started a new process), `heartbeat_restored_seconds` (the worker's
heartbeat key, the one `/api/heartbeat` reads, was written again) and
`first_task_started_seconds`. The key lives 90 seconds and outlives a dead
worker, so "restored" is the first time its remaining life goes up after the
kill. `tasks_lost_at_kill` are tasks the dead worker had already acknowledged;
`tasks_redelivered_after_kill` are late-acknowledging ones that ran again.

`tasks` lists, per task, how many were sent, started and finished, their
result status and their runtimes. `coverage.missing` names any of the four
measurements the run did not produce.

## Limits

- The data is small: 520 daily bars a symbol and ten intraday symbols. Runtimes
  and memory are far below production's; the report shows which queues wait on
  which worker and how long recovery takes, not how big production jobs are.
  `LOAD_RESEARCH_BALLAST_MB` can stand in for a known production footprint.
- The database is local. The hosted database answers a query in about 0.1
  second, so lock hold times there are longer than any local run shows.
- Nothing here exercises the Node gateway, its memory guard or the API.
