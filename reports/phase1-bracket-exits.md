# Phase 1: broker-side bracket exits (paper only)

Branch `phase1/bracket-exits`. Goal: exits for autonomous stock-paper entries
fire **at Alpaca**, even while the app is slow, restarting, paused or halted.

## What Alpaca allows

Sources: Alpaca "Orders at Alpaca" and "Fractional Trading" docs, plus Alpaca forum threads.

- **Bracket** (`order_class: "bracket"`) needs `take_profit.limit_price` and
  `stop_loss.stop_price`. `stop_loss.limit_price` is optional and makes the stop
  a stop-limit. Only `time_in_force` `day` or `gtc` is allowed, and
  `extended_hours` is not supported. For a limit entry, the stop has to be at
  least $0.01 below the entry limit price.
- The exit legs only activate after the entry has **completely** filled.
  Cancelling any leg cancels the rest of the group. When one exit fills, the
  other is cancelled. A partial take-profit fill shrinks the stop leg's
  quantity.
- **OTO** is the same with only one exit leg. It cannot be replaced.
- **OCO** places exits on an existing position. Its parent is a limit
  take-profit and its child is the stop.
- **Fractional/notional**: only simple orders with `day` TIF are allowed.
  Alpaca rejects bracket, OCO and OTO orders on fractional quantities ("fractional
  orders must be simple orders").
- `GET /v2/orders?nested=false` (our collector) lists each leg as its own order
  with its own Alpaca-generated `client_order_id`. A leg has no parent-id field.
  The parent links to its legs only through its nested `legs` array, which
  appears in the POST response and in single-order lookups.
- Selling with exit legs live: two closing orders on the same shares are rejected
  unless they are grouped OCO. A sell submitted while a leg holds the shares
  fails for insufficient quantity.

## Design chosen

Entries are **whole-share GTC bracket orders**. A bracket is the only form that
places the entry and both exits in one atomic broker request. That leaves no
window, no matter how slow the app is, where a filled entry has no exits.
Sizing produces fractional quantities, so:

- The risk-sized quantity is **rounded down to whole shares** when exits are
  enabled. If it rounds to 0, the trade is **skipped**, with a reason naming
  Alpaca's whole-share rule. The reason is never hidden. At $100k equity, scheduled
  sizing (1% = $1,000) still trades every symbol under $1,000. Exploration
  sizing (0.25% = $250) skips symbols priced above $250 (for example MSFT),
  which is expected.
- I chose **GTC** over DAY. DAY legs expire at the close, so overnight holdings
  would have no broker stop until the app re-armed exits at the next open. That
  breaks the "app is paused" goal. An unfilled GTC entry from a previous session
  is cancelled at the start of the next cycle (`cancel_stale_exit_entries`).
  That keeps the old DAY-entry behaviour, and Alpaca cancels its held legs with
  it.
- The alternative was a simple entry followed by an OCO once filled. I rejected
  it: the shares are unprotected between the fill and the app noticing it, and
  that is exactly the failure mode this phase removes. An OCO on fractional
  shares is not allowed anyway.
- Setting both `position_stop_loss_pct` and `position_take_profit_pct` to 0
  falls back to the old fractional simple DAY entry. Setting only one to 0 sends
  an OTO with the remaining leg.

### Risk-rule keys
- `position_stop_loss_pct` already existed (default 0.03). It now also sets the
  broker stop leg.
- `position_take_profit_pct` is new (default 0.03).
- Both can now be updated through the risk-settings API, bounded to 0–0.5.
  The control-room UI does not expose them yet.
- Prices: the stop is rounded down and the take-profit up, to a valid tick
  ($0.01 at or above $1, $0.0001 below). Both are kept at least one tick away
  from the entry limit.

### Ledger and attribution
- New columns on `stock_paper_orders`: `order_class`, `parent_order_id`,
  `leg_role` (`take_profit`/`stop_loss`), `stop_price`, `take_profit_price`.
- After a successful POST, each leg in the response is stored as its own order
  row. The row has `parent_order_id` set, inherits `strategy_id`, `source`,
  `trial_id` and `evidence_id`, and leaves `signal_id` empty because that column
  is unique. Leg fills therefore attribute to the strategy lot that opened the
  position (`_strategy_holdings` and the mistake classifier key on
  `StockPaperOrder.strategy_id`).
- Reconciliation:
  - `_upsert_orders` adopts leg rows from three kinds of evidence: nested
    `legs`, legs found by client-id lookup, and `replaces` (a PATCHed leg).
  - Adopted legs are never `broker_import`, so they do not trigger the
    "externally submitted nonterminal order" halt.
  - A leg's quantity may follow the broker, since the stop shrinks after a
    partial take-profit fill. All other leg fields stay immutable.
  - An uncertain (timed-out) bracket submission recovers its legs from the
    client-id lookup.
- Brackets submitted outside the app stay `broker_import` and keep failing
  closed.

### No double-sell, never a short
- Sellable inventory (`_committed_sell_quantity`, used by both reserve and
  dispatch) counts each live OCO group **once**. A group counts only if its
  entry has filled, and only up to the filled quantity on a partial fill.
  Unfilled entries' held legs commit 0. Dispatch refuses any app-side sell that,
  together with live legs, would exceed the position.
- `_protective_stop` sells only the quantity **not** covered by a live broker
  stop leg: fractional legacy lots, or partially filled entries whose legs are
  not active yet. If the legs cover everything, it reports
  `skipped: broker-side stop-loss leg protects this position`.
- A SELL signal on a lot with live legs runs in this order:
  1. **Reserve first**, with `replacing_exit_order_ids`, so the kill switch,
     session, feed and reconciliation gates all pass before any leg is touched.
  2. Cancel the group: one DELETE, preferring the active non-held leg, or the
     entry if it is still open.
  3. Re-read every member by client id.
  4. **Dispatch only when everything is terminal and no leg filled.** If a
     cancel is still pending, the result is `deferred` and the reservation is
     resumed idempotently on the next cycle. If a leg filled, the reservation
     is released (`release_unsubmitted_reservation`) and nothing is sold.
- New entries on a symbol with live exit legs are skipped. Alpaca may reject a
  crossing opposite-side order as a potential wash trade. I could not confirm
  the exact rule in the docs, and a rejected POST currently halts the ledger,
  so skipping is the conservative choice.

### Recovery, kill switch and halts
- Halting and the kill switch do not touch broker orders. GTC exit legs keep
  protecting positions while the app is halted or down. The kill switch only
  blocks new reserve/dispatch calls.
- `cancel_open_stock_orders` (recovery cancel-all/flatten) now sends **one DELETE
  per OCO group** (`exit_cancel_targets`). A second DELETE on an
  already-cancelled sibling would be rejected, and the batch would be reported
  as failed. Siblings stay `pending_cancel` until the existing post-cancel
  reconciliation confirms them. Flatten still runs only once nothing is
  outstanding.
- Tradier sandbox: `supports_bracket_orders = False`. Dispatch refuses a bracket
  entry before the POST instead of quietly sending an entry with no exits.

### Pre-existing bug fixed along the way
`dispatch_reserved_order` re-validated **every** buy with `_validate_signal_buy`,
which requires an approved-evidence strategy. Exploration entries could be
reserved but would always fail at dispatch with "Signal strategy is not active
for paper trading". Dispatch now re-checks exploration orders with
`_validate_exploration_buy`, excluding the order itself from the lot count.

## Files changed
- `artifacts/api-server/backend/app/services/stock_paper_ledger.py`: exit plan,
  leg persistence and adoption, inventory accounting, cancel helpers, stale-entry
  cancel, dispatch payload, exploration dispatch fix, status fields.
- `artifacts/api-server/backend/app/services/stock_paper_autotrader.py`: whole-share
  bracket entries, protective-stop coverage, SELL cancel-then-sell, entry
  suppression while legs are live, stale-entry cancel.
- `artifacts/api-server/backend/app/services/stock_recovery.py`: one cancel per
  OCO group (a small change inside `cancel_open_stock_orders`).
- `artifacts/api-server/backend/app/models/stock_paper.py`: new order columns.
- `artifacts/api-server/backend/alembic/versions/0063_stock_paper_bracket_exits.py`:
  new migration.
- `artifacts/api-server/backend/app/services/risk_settings.py` and
  `app/schemas/trading.py`: configurable exit percentages.
- Tests: `tests/test_stock_paper_bracket_exits.py` (new, 22 tests) and
  `tests/test_paper_exploration.py`, where expected quantities now round to
  whole shares.

## Tests
- New file `tests/test_stock_paper_bracket_exits.py`: 22 passed.
- Related suites (bracket, autotrader, exploration, ledger, paper recovery,
  migrations): 126 passed.
- Additional related suites (forward trial, probe and transport recovery,
  mistakes, monitoring, learning cycle, live risk recovery, strategy risk,
  interim migration): 209 passed, 17 skipped.
- **Full backend suite: 1644 passed, 32 skipped, 0 failed** (206 subtests
  passed, 350.8s). The first background run was killed partway through (exit
  code 144, a signal, not a test failure). The rerun under nohup completed.
- New bracket tests cover:
  - Payload shape: `order_class`, GTC, integral `qty`, `take_profit` and
    `stop_loss`.
  - Rejection of fractional brackets, and skipping when a trade is below one
    share.
  - Leg persistence carrying `strategy_id`.
  - Child-leg reconciliation and fill attribution.
  - Uncertain-submission leg recovery.
  - No double-sell while a leg is live: reserve is blocked, dispatch is blocked,
    then cancel-confirm-dispatch succeeds.
  - A leg fill seen during cancel blocks the app sell.
  - One DELETE per group, and recovery cancel-all handling legs.
  - Stale GTC entry cancel.
  - Autotrader cooperation: protective-stop deferral and remainder sizing, SELL
    deferred, released or submitted, entry suppression.

## Deploy notes
- **Migration required:** `0063_stock_paper_bracket_exits`, which follows
  `0062_intraday_no_trade`. It is idempotent: columns and index are added only
  if missing. **Run `alembic upgrade head` against the Replit dev DB before
  publishing.** The ORM now selects the new columns, so un-migrated databases
  fail schema checks and order queries.
- Existing open positions and simple orders are untouched and get no broker
  legs. The app-side protective stop still covers them, because their stop
  coverage is 0. OCO re-arming for legacy or partially filled lots is a possible
  follow-up and is not implemented.
- Not verified against live Alpaca paper. No broker calls were made.
  - Assumed: the bracket POST response includes `legs`. If it does not,
    reconciliation adopts them from the client-id lookup.
  - Assumed: a bracket's stop leg reports `held` while the take-profit is `new`.
  - Assumed: cancelling the non-held leg cancels the group.
  - Worth one supervised paper check after deploy: confirm leg statuses, the
    one-DELETE cancel, and the response shape.
- Partially filled entries: the legs do not activate until the entry fills
  completely. Until then the filled shares rely on the app-side protective
  stop. A partially filled GTC entry still open at the next session is
  cancelled, and its filled shares keep app-side protection.
- Kill-switch caveat: if the kill switch is turned on after a SELL has cancelled
  the legs but before its deferred dispatch, that lot has only app-side
  protection, and the kill switch blocks that as well. Recovery flatten is still
  available.
