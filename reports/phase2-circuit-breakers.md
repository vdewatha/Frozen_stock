# Phase 2: Entry Protections (Circuit Breakers)

Branch `phase2/circuit-breakers`. Covers items 6 and 7 of
`reports/open-source-research-20261028.md`: freqtrade-style protections and a
time-of-day entry filter. freqtrade is GPL-3.0, so this is our own code. Only
the ideas came from there.

## What it does

The new module is `artifacts/api-server/backend/app/services/entry_protections.py`.
The autotrader loads it once per cycle with `load_entry_protections(...)`. It
then calls `protections.check(symbol, strategy_type)` on the **BUY branch
only**. The call comes after the existing "exit pending / already holding" skips
and before sizing and `reserve_stock_paper_order`. Both the scheduled
(`scheduled_paper_signal`) and exploration (`paper_exploration`) entries go
through this branch, so both are covered.

When a protection blocks, the result row is set to `status: "skipped"`, with
`protection: <name>` and `reason: "entry protection <name>: ..."`. It also gets
`protection_until` when the block ends at a known time. The cycle result has a
new `entry_protections` summary: the resolved config, the session drawdown and
any cycle-wide block. A skip does not count as blocked or failed.

| Protection | Name in result | Rule |
|---|---|---|
| Stop-loss guard | `stoploss_guard` | N stop-outs for a strategy within the lookback window pause that strategy's entries for the pause period, measured from the latest stop-out. An optional global count pauses all entries. |
| Exit cooldown | `exit_cooldown` | Any sell fill on a symbol, from any source, blocks re-entry on that symbol for M minutes. |
| Low-profit symbol lock | `low_profit_symbol` | If the last K closed round trips of (strategy, symbol) sum to less than 0, that strategy is locked out of that symbol for the lock period after the latest close. |
| Session drawdown lock | `session_drawdown` | If equity has fallen at least X% from its peak since the regular-session open, all entries are blocked until the session closes. |
| Time of day | `time_of_day` | No entries in the first B₁ minutes after the open or the last B₂ minutes before the close. Times come from `session_bounds`, so half-days use their early close. |
| Load failure | `unavailable` | If the protection state cannot be loaded, every entry is withheld (fail closed, entries only). Sells are unaffected. |

### How stop-outs are detected

A stop-out is **one sell order**. Several partial fills of the same order count
once. Its fills must come from one of two places:

1. **A broker exit leg with `leg_role == "stop_loss"`.** These are the stop legs
   of bracket/OTO orders. Legs inherit the entry's `strategy_id`, so they are
   attributed to that strategy.
2. **The app-side protective stop sell.** The autotrader reserves it with a
   deterministic idempotency key, which `reserve_stock_paper_order` hashes into
   `client_order_id`. The key is
   `scheduled-paper:{ET session date}:{symbol}:protective_stop:sell`, and it now
   comes from one shared helper, `protective_stop_idempotency_key`, in both
   places. The loader recomputes the hash for the order's ET creation date and
   fill date, and the day before each. These orders carry no `strategy_id`, so
   they are attributed **FIFO to the strategy lots they closed**.

Take-profit legs, signal sells, manual or recovery sells and imported broker
sells are **exits**, so they trigger the cooldown, but they are **not**
stop-outs.

### How closed trades are reconstructed

`reconstruct_trades` is a pure function. It replays the account's fills from
the last 30 days (`HISTORY_DAYS`):

- A buy opens a lot tagged with its strategy type.
- A sell attributed to a strategy closes that strategy's lots first (FIFO).
- An unattributed sell, or any remainder, closes the symbol's lots FIFO across
  strategies.
- A round trip closes when the strategy's open quantity in the symbol returns
  to 0.
- Net P&L includes broker-reported fees.
- A sell with no open lot in the window still counts as an exit event but adds
  no P&L. This can only under-count losing trades.

### How the session drawdown is measured

`peak = max(equity snapshots observed since today's open, current account equity)`,
then `dd = (peak − current) / peak`. Account equity is the broker's figure, so
it includes both realized and unrealized P&L.

If no snapshot exists yet this session, the prior-close `last_equity` is the
baseline. The existing `max_daily_drawdown` gate (3%, prior close to now,
scheduled entries only, in `_validate_signal_buy`) is untouched. This lock is
an earlier, intraday, peak-based stop that applies to entries only, on both
paths. It does not replace that gate.

## Defaults and configuration

Each value is read from the active `RiskRule.value` key first, then from
`settings.paper_<key>` (environment variable `PAPER_<KEY>`). Setting a value to
`0` disables it. Unparseable or negative risk-rule values fall back to the
setting.

| Risk-rule key | Setting / env | Default |
|---|---|---|
| `stoploss_guard_count` | `PAPER_STOPLOSS_GUARD_COUNT` | 3 |
| `stoploss_guard_global_count` | `PAPER_STOPLOSS_GUARD_GLOBAL_COUNT` | 0 (off) |
| `stoploss_guard_lookback_minutes` | `PAPER_STOPLOSS_GUARD_LOOKBACK_MINUTES` | 360 (6 h) |
| `stoploss_guard_pause_minutes` | `PAPER_STOPLOSS_GUARD_PAUSE_MINUTES` | 120 (2 h) |
| `exit_cooldown_minutes` | `PAPER_EXIT_COOLDOWN_MINUTES` | 30 |
| `low_profit_trades` | `PAPER_LOW_PROFIT_TRADES` | 4 |
| `low_profit_lock_minutes` | `PAPER_LOW_PROFIT_LOCK_MINUTES` | 1440 (1 day) |
| `session_drawdown_lock_pct` | `PAPER_SESSION_DRAWDOWN_LOCK_PCT` | 0.02 |
| `entry_open_buffer_minutes` | `PAPER_ENTRY_OPEN_BUFFER_MINUTES` | 15 |
| `entry_close_buffer_minutes` | `PAPER_ENTRY_CLOSE_BUFFER_MINUTES` | 10 |

## Safety properties

- **Exits are never blocked.** The check is never called on the SELL branch,
  the protective-stop pass, exit-leg cancellation or replacement, recovery or
  manual paths. Tests assert that signal sells and protective stops still
  reserve under every protection, including the load-failure state.
- **Only restrictions were added.** The kill switch, launch admission,
  reconciliation, risk caps and the daily drawdown gate are unchanged.
- **No broker calls**, and no schema change or migration.
- **Clock injection.** Every check is a pure function of `now`.
  `EntryProtections(now=..., bounds=...)` lets tests pin the clock.

## Tests

- New: `artifacts/api-server/backend/tests/test_entry_protections.py`, 48
  tests. They cover:
  - configuration overrides and disabling;
  - time-of-day edges on a regular day (9:30, 9:44:59, 9:45, 15:49:59, 15:50,
    15:59:59, 16:00);
  - the 2026-11-27 half-day early close at 13:00 ET;
  - the stop-loss guard's threshold, window, pause, non-stop exits and global
    scope;
  - cooldown, low-profit lock and drawdown;
  - FIFO attribution and partial-fill counting;
  - the SQLite loader, which detects stop legs and protective stops and reads
    equity snapshots;
  - fail-closed loading;
  - cycle integration. Each protection skips scheduled and exploration buys,
    and never blocks signal sells or the protective stop.
- New `tests/conftest.py`: an autouse fixture sets both time-of-day buffers to
  0 for the suite. Without it, cycle tests that run during a real session's
  first 15 or last 10 minutes would be flaky. Protection tests set the buffers
  explicitly.
- Related suites (autotrader, bracket exits, exploration, signal batch, trading
  universe, plus the new file): 110 passed.
- Full suite: 1732 passed, 32 skipped, 206 subtests passed in 389 s; 0 failures.

## Deploy notes

- No migration.
- No `.replit` change is required: the defaults apply. To tune or disable a
  protection, set a risk-rule key (preferred, no redeploy) or the matching
  `PAPER_*` environment variable.
- Expected behaviour change in paper:
  - fewer entries at the open and close;
  - pauses after stop-out clusters;
  - 30-minute re-entry cooldowns;
  - locks on symbols that keep losing;
  - an entry stop after a 2% intraday giveback.

  All of these appear as `skipped` rows with the protection named, so the
  mistake and learning loop can count them.
