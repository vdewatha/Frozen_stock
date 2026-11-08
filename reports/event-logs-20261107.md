# Zero-cost event logs: 2026-11-07

**What this is:** step 8 of the build plan in [Retail bot edge feasibility](Retail%20bot%20edge%20feasibility.md) ("log events at no cost"). It adds two forward logs, an earnings window and a pre-ex-dividend week, and fixes a gap in the earnings-blackout setup shadow. It also runs the one pre-registered test on an ex-dividend backfill. **That test fails (t = −0.22), so no tie-breaker is admitted.** None of this places, blocks or sizes a trade. Paper only.

Code: `app/services/event_logs.py` (its docstring is the reference), `app/services/event_log_backfill.py` (the stored backfill summary), a small fallback in `setup_shadow.py`, one helper factored out of `earnings_calendar.py`, two jobs, two beat entries, two settings, and a new section on the yardstick and learning-scoreboard endpoints. No database migration: every record is an `event_logs` audit row.

## 1. Earnings-blackout setup shadow: kept at 0 / 10 / 20, with a coverage gap fixed

The setup shadow (PR #28) still decides the earnings blackout at **0, 10 and 20 sessions** (`experiment_engine` template `earnings_blackout`). A test now pins this, and `event_log_status` reports the candidates.

**The gap.** The shadow read earnings dates only from the process cache. The cache holds a symbol only if this cycle's live blackout fetched it, and the live gate fetches only when (a) the BUY got past every earlier check (holdings, one position per symbol, the market/VIX/sector protections) and (b) the strategy's own blackout is above zero. Every other BUY recorded the 10 and 20 candidates as `unknown`, and the scorer drops unknown decisions. So the shadow had no 10/20 record exactly where it mattered: for strategies whose live blackout is off, and for BUYs a market gate stopped.

**The fix.** When a symbol is not in the process cache, `CycleContext.earnings_block` falls back to the latest stored calendar snapshot from the earnings log below. That is one database read per cycle, used only for calendars fetched within the last 8 days, with no extra provider call. Index ETFs are decided as "no earnings", as the live calendar answers for funds. A symbol with no recent snapshot stays `unknown`, as before. Each `setup_shadow` row now records `earnings_sources` (cache, snapshot, unknown lookups), so the coverage can be checked after a publish.

## 2. Earnings log (forward)

* **Calendar snapshot, weekdays at 06:10 ET (`event_log_daily_job`, `learning_fast` queue).** This is the free calendar the blackout already uses (yfinance `Ticker.calendar` through `earnings_calendar.upcoming_earnings`). It covers today's point-in-time S&P 500 members, at most **120 fetches a run** (`event_logs_calendar_fetches_per_run`), which takes about two minutes of HTTP waits and little CPU. The order is: never-fetched symbols first, then symbols reporting within 7 days (re-confirmed every run), then any calendar older than 6 days. About 500 members are covered in under a week, and each is refreshed weekly after that.
  * A single date counts as a confirmed report date. Two dates are Yahoo's estimated range and create no event. A future date that a later fetch drops is treated as rescheduled and removed. Each date records the day it was first and last seen.
  * The state is one `earnings_calendar` row per run that fetched something, and the latest row is the state. A failed fetch keeps the previous dates and is retried. A missing membership file writes nothing and reports `unavailable`.
* **Scoring (same job).** Once a report's window has closed, the job logs the **market-adjusted return from the close two sessions before the report to the close one session after it**: stock minus SPY, on yfinance adjusted closes from one batched download. The anchor is the first session on or after the report date, so a report before the open and one after the close both fall inside the window. Only members on the report date are logged.
  * `known_ex_ante` is true when the date was first seen on or before the window's opening close, which is what a trade at that close could have known.
  * Missing bars are retried for 10 days, then logged as `missing_data`. Events are `earnings_events` rows and are never logged twice.
* **The test waits.** It runs once the log holds about **6,900 effective events**: t = 2.5 for a 15 bps premium at a 5% event volatility, from the event note. Effective events discount same-date clustering, `Σ n_d / (1 + (n_d − 1)ρ)` over event dates, and only `known_ex_ante` events count. ρ is the log's own intra-date correlation once 50 dates hold two or more events. Before that it is the ex-dividend backfill's ρ = **0.077**.
* **Progress counter.** `GET /stock-paper/yardstick` → `event_logs.earnings`, mirrored in the learning scoreboard under `yardstick.event_logs`. It shows scored, ex-ante and missing counts, event dates, ρ and its source, effective events, the 6,900 target, progress and `ready_for_test`.

**How long the wait is.** At ρ = 0.077 the wait is much longer than the plan's "three and a half years before clustering". Applying that ρ to the 2025 report dates of the 149 names whose earnings history is cached, scaled to 500 names, gives about 1,850 raw reports but only about **790 effective events a year**: roughly 8-9 years to 6,900. At ρ = 0.03 it would be about 1,150 a year, or 6 years. The counter will show the real rate once the log's own ρ takes over. This is my arithmetic, not a measurement.

## 3. Ex-dividend log: backfill, the one test, and the forward log

### Pre-registration

This is in the docstring of `scripts/run_ex_dividend_backfill.py`, written before the first run.

* Universe: every S&P 500 member since 2015-01-01 (770 tickers). An event counts only if the ticker was a member on its ex-date.
* Data: yfinance adjusted closes plus the dividend column, from 2014-09-01, and SPY. Any dividend above zero marks an ex-date.
* Window: from the close six sessions before the ex-date to the close one session before it. All five sessions are cum-dividend, so the dividend never enters the return, and the price ratio is the same on adjusted and raw closes. The outcome is that return minus SPY's total return over the same closes.
* **The one test:** the mean market-adjusted return with standard errors **clustered by ex-date** (CR1). It passes if the mean is positive and t ≥ 2.5. A pass would admit it only as a zero-turnover tie-breaker, documented and not wired into entries.

### Result: fails

| | |
|---|---|
| Events (2015-01-02 to 2026-11-06) | **16,942** on 2,720 ex-dates, 475 payers |
| Mean market-adjusted return | **−0.77 bps** (median −6.4 bps, SD 352 bps, 49.0% positive) |
| Date-clustered SE / **t** | 3.57 bps / **−0.22** |
| Not deciding: iid t, week-clustered t | −0.28, −0.17 |
| Intra-date correlation | 0.077 |
| Outcome | **not admitted** (t is far below 2.5, and the sign is wrong) |

By year (mean bps, date-clustered t): 2015 −19.7 (−2.2), 2016 −4.1, 2017 −9.8, 2018 −14.3, 2019 +3.3, 2020 +21.3, 2021 +53.7 (+4.1), 2022 +28.3 (+2.5), 2023 −20.6, 2024 −20.7, 2025 −13.8, 2026 −12.3. The full-sample mean is zero, built from regimes that swing both ways. The 2021-22 run is the kind of window a selective backtest would find.

**Flags, and why the result is an upper bound.** 150 of the 770 members have no yfinance history at all, so their events cannot be scored. Examples are acquired, renamed or delisted names such as ATVI, CELG, TWTR, XLNX, SIVB and FRC, plus several 2025-26 renames and takeovers. Yahoo no longer serves BK, MMC, IPG or CMA either; this was checked one by one. The share of members without history by year: 23% in 2015, 20% in 2016, 19% in 2017, 17% in 2018, 15% in 2019, 12% in 2020, 10% in 2021, 8% in 2022, 5% in 2023, 4% in 2024, 2% in 2025 and 1% in 2026. The scored sample over-represents survivors, so the mean is labelled an **upper bound**. Even as an upper bound it is about zero. The full list is in the script's JSON output, and the first 40 are in the stored summary.

**Decision:** no ex-dividend tie-breaker. The plan's own stop rule ("stop logging") would allow dropping the forward log. It is kept because it costs one batched download a week, and it can confirm or contradict the backfill on survivorship-free data. No entry uses it.

### Forward log

`event_log_weekly_job` runs Saturdays at 12:10 ET on `learning_fast`. It makes one batched yfinance download (chunks of 100, 2 threads) of the last 70 days of adjusted closes and dividends for today's members plus SPY. It then logs each new ex-date of a member with the same window, starting the day after the backfill's last ex-date so that nothing is counted twice. Rows are `ex_dividend_events`. Members the download did not return are listed in the row.

## What changes after a publish

* Two new scheduled jobs: `event-log-daily` (weekdays 06:10 ET) and `event-log-weekly` (Saturday 12:10 ET), both on `learning_fast`. They are light: HTTP waits, one batched price download, and one audit row per run.
* The setup shadow decides the 10 and 20 blackout candidates for BUYs the cycle did not fetch a calendar for. This starts once the first calendar snapshot exists, on the first weekday 06:10 run, with full coverage in about a week.
* `GET /stock-paper/yardstick` and the learning scoreboard gain an `event_logs` section.
* Nothing trades differently. The month-end overlay (step 7) failed and was not built, so `target_weight_book.py` is unchanged.
* Settings: `event_logs_enabled` (default on; off stops both logs) and `event_logs_calendar_fetches_per_run` (default 120).

## Reproduce the backfill

```
cd artifacts/api-server/backend
PYTHONPATH=. python scripts/run_ex_dividend_backfill.py --data-dir <cache dir> \
    --membership-csv <cache>/universe/sp500_membership.csv --download \
    --output <json> --summary-module app/services/event_log_backfill.py
```

Caches live in an uncommitted scratch directory. The data cost is $0. The membership file is the cached fja05680/sp500 file, last updated 2026-08-18, so index changes after that date are not reflected.
