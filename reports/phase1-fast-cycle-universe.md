# Phase 1: fast decision cycle and expanded paper universe

Branch `phase1/fast-cycle-universe`. Paper only. No broker calls, order-submission
changes, kill-switch, readiness-gate weakening or live-trading changes.

## 1. Why a cycle took 200–290 s

`run_stock_paper_signal_cycle` ran 8 strategies x 4 symbols = 32 sequential
`create_stock_paper_signal` / `create_stock_paper_research_signal` calls. Each call:

| step | cost per call |
|---|---|
| resolve strategy row | 1 query |
| `trusted_history` (daily frame, 260 rows) | 2 queries + validation |
| `trusted_intraday_observation` → `feed_status` | scans the whole session's 1m bars, no-trade minutes (twice), latest bar, then another latest-bar query: about 5 queries plus a per-minute Python gap walk |
| strategy compute | under 2 ms, except `model_predictive_long`: about 115 ms locally (LogReg + 40-tree RF x 3 horizons), probably 300+ ms on a 0.5–2 vCPU VM |
| ledger event + `commit` | 1 round trip |

So each symbol's data was loaded **8 times**, the model was refitted on every cycle,
and the cycle made 32 commits. Remote-DB latency multiplied by about 8 queries per
signal, plus model refits, explains 6–9 s per signal.

Note: this signal path builds no news or macro context. That lives in the separate
`trade_candidates` snapshot, which is not part of the paper decision cycle, so
nothing there needed batching.

The per-order gates (`reserve_stock_paper_order` → launch admission:
broker status, portfolio risk and audit chain) still run once per *actionable* order.
They were left untouched because they belong to the other agent and are safety gates.

## 2. What changed

### Batched signal pass (`app/services/stock_paper_signal_batch.py`)
`generate_cycle_signals(db, symbols, strategies)` replaces the per-pair calls:

* Each strategy row is resolved **once per cycle**, with the same
  "oldest active row of this type and scope" rule as before.
* Each symbol's trusted daily frame and execution observation are loaded **once**
  and shared by all strategies, so data loads are O(symbols).
* Strategy outputs are memoized on a content hash of the daily frame plus the
  strategy type, parameters and symbol. All strategies are deterministic (the RF uses
  `random_state=42`), and daily frames change only after the twice-daily import, so
  warm cycles do no strategy compute.
* All `StrategySignal` rows and ledger events are written with **one flush and one
  commit**.
* Rows, `features` payloads, events and the returned dicts come from the same helpers
  `_create_stock_paper_signal` now uses (`_resolve_signal_strategy`,
  `_load_signal_inputs`, `_build_stock_paper_signal`, `_record_signal_generated`,
  `_stock_paper_signal_result` in `stock_paper_ledger.py`; only the signal-creation
  region was touched). A test asserts batched and per-pair outputs match field by
  field.
* Error semantics are unchanged. Unknown strategy → `StockPaperError` (it takes
  precedence, as before). Untrusted data → `StockPaperError` for that symbol only. A
  compute exception fails only that pair. One known difference: a failed *batch
  commit* now fails all of that cycle's signals instead of one.

The autotrader keeps its phase-1/phase-2 governance exactly: the research-only,
active and exploration routing, exits before entries, the no-pyramiding rule, the
protective stop, idempotency keys, and `reserve_stock_paper_order` →
`dispatch_reserved_order`. The cycle result now includes `timing` and
`signal_batch` stats (loads, computes, cache hits, ms per phase), and both are logged.

### Sector / correlation cap
For scheduled entries (both active and exploration), a BUY of a **new** name is
skipped when its sector ETF group already has `max_positions_per_sector` open or
pending-buy names. The limit comes from the risk-rule key, else
`PAPER_MAX_POSITIONS_PER_SECTOR` (default 3; 0 disables). Index ETFs
(SPY/QQQ/IWM/DIA) form their own group, and unmapped symbols are their own group.
Entries submitted in the same cycle count toward the cap. The cap is enforced in the
autotrader rather than in `reserve_stock_paper_order`, to avoid the other agent's
reserve path. The existing DB risk caps (`max_open_positions=10`,
`max_symbol_exposure=0.1`, etc.) are unchanged and still enforced in reserve.

### Universe (`app/services/trading_universe.py`, `config.py`, `.replit`)
* `paper_execution_symbols` is still the single source of truth. Its default is now
  45 names: SPY QQQ IWM DIA, XLK XLF XLE XLV XLY XLP XLI XLU XLB XLRE XLC SMH, and
  AAPL MSFT NVDA AMZN GOOGL META TSLA AVGO JPM V MA UNH XOM LLY COST HD PG JNJ ABBV
  MRK PEP KO BAC WMT NFLX AMD CRM ORCL ADBE. The validator cap went from 25 to 60.
  `.replit` `PAPER_EXECUTION_SYMBOLS` carries the same list.
* `intraday_symbols()` = execution symbols ∪ the frozen research core
  (AAPL MSFT QQQ SPY). `intraday_data._symbol`, `ingest_intraday`, post-close repair,
  `fetch_iex_bars` validation and readiness all use it.
  `intraday_data.ALLOWED_SYMBOLS` intentionally **stays the frozen 4-symbol research
  core**.
* Asset rows: `ensure_paper_execution_assets()` (seed.py) creates any missing active
  `Asset` rows with sector metadata. It never reactivates an operator-deactivated
  asset. It runs in `seed_defaults` (startup) and at the start of
  `daily_market_data_import`, which then imports trusted daily history for the new
  names. `PAPER_STARTUP_REFRESH=true` queues that import on boot.
* `ingest_corporate_actions` now skips active watchlist assets outside the intraday
  universe instead of raising, which would have failed the whole daily import.

Left on the frozen 4-symbol universe on purpose, because they are governance and
evidence universes, not execution:
learning-cycle `ONE_SESSION_SYMBOLS`, forward-trial `frozen_universe`, scalp research
(`scalp_research*.py`, `PAPER_SCALP_RESEARCH_SYMBOLS`, the scheduled scalp job),
`agent_research`, `delayed_sip_research`, `online_research`, `collect_iex_research`,
and **`live_pilot`** (live safety; it must not widen).
`stock_learning_default_symbols` (cap 25) is also unchanged.
`_trial_execution_symbols` still intersects a trial's frozen universe with the
execution list.

### Per-symbol market-data gating
Before this change, any stale execution symbol made the monitor check
`data.freshness_provenance` a `breach`. Two consecutive breaches paused the whole
stock path through recovery. Readiness also blocked "Intraday feed" and "Market data".

`classify_feed_outage(failed, universe)` now decides the scope:
* **partial**: only the failed symbols are blocked. The existing per-symbol gates do
  the blocking: `reserve_stock_paper_order` checks `feed_status` per symbol, and
  `trusted_history` / `trusted_intraday_observation` fail that symbol's signals.
  The monitor reports `warning` (no pause). Readiness reports `warning` for
  "Intraday feed" / "Market data", and both are allowed paper warnings.
* **broad**: more than `PAPER_FEED_BROAD_OUTAGE_FRACTION` (default 0.2) of the
  universe failed, **or SPY/QQQ failed**, or the universe is empty. This keeps the
  old behavior: monitor `breach` → pause, readiness `blocked`.
* With a small universe the old fail-closed behavior holds: 1 of 4 is 25%, which is
  over 20%, so it is still broad.
* **Onboarding** symbols are never counted toward the broad share. These are symbols
  with no persisted intraday bar from the active provider, or (for daily data) no
  daily rows or no Asset row. They are still blocked individually. Without this rule,
  adding 41 new names would read as a 90% outage and the monitor would pause the
  stock path on deploy day. If *every* symbol is onboarding (fresh DB), the effective
  universe is empty and the outage is broad (fail-closed).

### Intraday import and rate limits (`intraday_data.ingest_intraday`)
* The newest completed window, which every symbol needs every minute, is fetched with
  **multi-symbol Alpaca requests**: `_fetch_bars_multi`, up to 50 symbols per request.
  50 x 60 bars = 3,000, so it fits in one 10,000-bar page.
* Repair/backfill windows stay per symbol but share a cycle-wide budget,
  `INTRADAY_BACKFILL_REQUEST_BUDGET` (default 40). Unspent gaps stay reported as
  missing and are retried next minute. A rotating start offset keeps the budget from
  starving the same tail symbols.
* If the batch request fails, the code falls back to per-symbol requests, so one bad
  symbol cannot fail everyone. The exception is HTTP 429: then it does not fan out,
  and every symbol reports the provider failure.
* Tradier has no multi-symbol timesales endpoint, so it keeps one request per symbol
  for the newest window. Backfill is still budgeted.

## 3. Timing and data-load counts

Measured locally on an Apple-silicon laptop, using SQLite and synthetic 260-row
frames, for 50 symbols x 8 strategies (`tests/test_stock_paper_signal_batch.py`):

| | before (per-pair path) | after, cold (first cycle after daily import) | after, warm (every other cycle) |
|---|---|---|---|
| daily frame loads | 400 | 50 | 50 |
| intraday observation loads (`feed_status`) | 400 | 50 | 50 |
| strategy resolutions | 400 | 8 | 8 |
| strategy computes (model fits) | 400 (50) | 400 (50) | 0 |
| commits | 400 | 1 | 1 |
| signal-phase wall time | not measured at 50 symbols; extrapolated from production at about 7 s/signal: far over 20 min | 3.9 s | 0.08 s |

Estimate for a 2 vCPU VM against the hosted Postgres: the cold cycle is dominated by
50 model fits, roughly 3x slower there, so about 10–15 s. Warm cycles do about 300
queries (about 6 per symbol), so about 2–5 s at 5–15 ms per query. That is within the
under-20-s target. The order phase adds the reserve/dispatch gates per *actionable*
order only. The test suite asserts exactly 50 daily loads, 50 intraday loads,
8 strategy resolutions and 1 commit for 50 x 8, and that the cold pass takes under
20 s.

## 4. Alpaca rate-limit budget (free IEX, 200 req/min)

| source (per minute) | requests |
|---|---|
| `intraday_market_data_import` newest window, 49 intraday symbols | 1 (50 symbols per request) |
| same, repair/backfill (shared budget) | ≤ 40 (0 once the session is complete) |
| `iex_research_collection_job` (4 research symbols, one multi-symbol request) | 1 |
| forward-trial preflight (on resume only) | ≤ 1 + repairs |
| broker reconciliation / dispatch (paper trading API, separate limit bucket) | unchanged |
| **total market data, worst case** | **about 45/min** (about 22% of 200) |

The daily import (yfinance, with Yahoo chart fallback) runs twice a day at about one
request per active asset (about 45–50). That is not Alpaca. The `alpaca_iex_daily`
fallback is rarely used.

## 5. Deployment notes

* **No Alembic migration.** Asset rows are data, created idempotently by
  `ensure_paper_execution_assets` at startup and at daily import, and `Asset.sector`
  already exists. Nothing needs to run on the Replit dev DB beyond the usual
  `alembic upgrade head`.
* **.replit**: `PAPER_EXECUTION_SYMBOLS` was expanded to the 45-name list. To roll
  back, set it to the 4-symbol list again.
* New optional env settings, with defaults:
  `PAPER_FEED_BROAD_OUTAGE_FRACTION=0.2`, `PAPER_MAX_POSITIONS_PER_SECTOR=3`,
  `INTRADAY_BACKFILL_REQUEST_BUDGET=40`. A risk-rule key `max_positions_per_sector`
  overrides the sector cap.
* First session after deploy: new symbols are *onboarding*. They stay blocked per
  symbol, with readiness and the monitor at `warning` rather than broad. That lasts
  until their daily history exists (startup refresh / 08:15 import) and verified
  intraday bars exist (post-close/pre-open repair, or the first minute of the
  session). Once a symbol has any bars it counts normally. Session gaps whose newest
  bar is current and verified count as historical warnings in the monitor, not as
  failures. A stale or unverified newest bar does count toward the 20% threshold. Expect
  "Intraday feed: warning" (repair gaps) on deploy day. It is best to deploy outside
  market hours, at least about 15 minutes before the open, so post-close repair
  (budget 40 requests/min) can fill the prior session for the new names.
* `max_open_positions=10` in the DB risk rule now becomes the binding limit with 45
  names. Raise it deliberately if you want more.
* The memo cache is per worker process and bounded at 4,096 entries, about 400 used.

## 6. Tests

New tests:
* `tests/test_stock_paper_signal_batch.py`: O(symbols) loads, a single commit, the
  20 s cold bound and warm cache hits for 50 x 8; batched == per-pair output; an
  untrusted symbol is isolated; unknown-strategy and compute failures are isolated.
* `tests/test_trading_universe.py`: config default and bounds; the intraday universe
  vs the frozen core; asset seeding; partial/broad/sentinel classification; monitor
  warning vs breach; readiness warning vs blocked; one multi-symbol request for the
  newest window; the shared backfill budget; the sector cap (existing holdings,
  same-cycle entries, disabled).

Updated tests: `test_stock_paper_autotrader.py` and `test_paper_exploration.py` now
patch the batch pass with a per-pair double. `test_intraday_data.py` was updated for
the configured universe.

Related suites (15 files): **358 passed, 54 subtests passed**.
Full suite: see the final summary for the counts from this branch.
