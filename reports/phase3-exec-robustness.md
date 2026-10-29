# Phase 3: execution robustness (paper only)

Branch `phase3/exec-robustness`. It closes three gaps left after phase 1
(bracket exits) and found in production observation on 2026-10-27/28:

1. Some long positions have no broker-side exit. This change re-arms them with an OCO or a GTC stop.
2. The intraday feed flipped between ready and stale every minute.
3. The control room could not edit the position stop-loss and take-profit distances.

No broker calls were made. Every test uses fake gateways.

## 1. Broker-side exit re-arm

### What Alpaca allows
Sources: Alpaca's "Orders at Alpaca" page and the phase-1 research.

- `order_class: "oco"` is an exit for an existing position only. It needs
  `type: "limit"`, `take_profit.limit_price` and `stop_loss.stop_price`
  (`stop_loss.limit_price` is optional and makes the stop a stop-limit).
- For a sell OCO, the stop must be at least $0.01 below the take-profit.
- The take-profit is the parent order and the stop is its child. One leg
  filling cancels the other.
- The documentation examples use `gtc`, and the bracket rules (`day`/`gtc`)
  apply. We use `gtc` so the exit survives overnight.
- Fractional quantities can only be simple `day` orders. Stop orders on
  fractional quantities are `day` only, and bracket/OCO/OTO orders are
  rejected. So a fractional remainder cannot carry a resting broker exit.
- "OTO stop-only" needs an entry order. On an existing position, the
  stop-only equivalent is a plain `type: "stop"` GTC sell, which is allowed for
  whole shares.

### Design: `app/services/stock_exit_rearm.py` (new)
`rearm_broker_exits(db, gateway=None, now=None)`:

- **Whole shares only.** For each long position, the service splits the
  shares into strategy-owned lots, using fill attribution with the same keys
  as `_strategy_holdings`. Shares nobody owns form one unattributed lot. It
  then subtracts the live broker stop coverage per owner
  (`live_exit_orders` / `_exit_group_exposure`, counting each OCO group once).
  The whole-share part of each uncovered lot is armed. A fractional remainder
  is reported as `fractional_uncovered` and left to the app-side protective
  stop, which already sells exactly the quantity not covered by a broker stop
  leg.
- **Prices** come from `build_exit_plan(average_entry_price, ...)`, the same
  tick rounding as bracket entries: the stop is rounded down and the
  take-profit up.
  - Percentages come from the owning strategy's
    `parameters.exit_plan.{stop_loss_pct,take_profit_pct}` (or flat keys) if
    present. Otherwise they come from the `position_stop_loss_pct` /
    `position_take_profit_pct` risk rules (default 0.03 each). This is the
    integration point for the parallel per-strategy exit-plan work. If that
    work uses different keys, adjust `exit_percentages()`.
  - Price at or below the stop: skipped. The app-side protective stop exits
    the position.
  - Price at or above the take-profit: a stop-only GTC order is placed, because
    a take-profit limit below the market would sell immediately.
  - Stop distance of 0: nothing is armed.
- **No double-sell, never a short.** The armed quantity is capped by two
  limits:
  1. The ledger position less every live exit group and in-flight sell
     (`_committed_sell_quantity`). Unsubmitted reservations are excluded,
     because they hold no broker shares and dispatch re-checks them.
  2. The broker's `qty_available` from a fresh `positions()` call.

  If the ledger and broker quantities differ, the symbol is skipped. Once
  armed, the OCO group counts in `_committed_sell_quantity`, so reserve and
  dispatch refuse any app-side sell that would overlap it. This is tested.
- **Per-symbol blocks.** A symbol is skipped while any of these is open: a
  `broker_import` order, a `pending_cancel`, an open or reserved buy (bracket
  legs may still activate, and Alpaca may flag a wash trade), or an in-flight
  app-side sell. A reserved app sell less than 10 minutes old also blocks the
  symbol while trading is enabled, because it is about to replace the exits.
  A broker rejection makes the symbol back off for 60 minutes.
- **Partially filled bracket entries (gap 1b).** The legs stay `held` until the
  entry fills completely, so an OCO on top of them could oversell once they
  activate. Instead, an entry still `partially_filled` more than
  `paper_exit_rearm_partial_entry_grace_minutes` (default 5) after its first
  fill is **cancelled**. This is a single DELETE, and Alpaca cancels the held
  legs with it. After reconciliation confirms the cancel, the next run covers
  the filled shares with an OCO. Cancelling the unfilled rest of a buy only
  reduces exposure.
- **Attribution.** Re-armed rows copy the owning lot's `strategy_id`, `source`
  (for example `paper_exploration`, so exploration lot counting stays correct)
  and `evidence_id`. The newest filled entry for that strategy and symbol
  supplies them. Unattributed lots use `source = "exit_rearm"` and
  `strategy_id = NULL`.
  - Ambiguous case: several strategies hold the symbol and app-side sells
    (which carry no strategy) have reduced it. The remainder is then protected
    as one unattributed lot rather than guessed. This matches the autotrader's
    unattributed-SELL fallback.
  - Forward-trial lots (`trial_id`) are skipped. Their exits are governed by
    their approval.
- **Ledger rows.** No migration is needed; the rows use the 0063 columns.
  - The OCO root is `order_class="oco"`, `leg_role="take_profit"`,
    `parent_order_id=NULL`, with `order_type="limit"`, `limit_price` and
    `take_profit_price` set to the take-profit and `stop_price` set to the stop.
  - The stop child is `leg_role="stop_loss"`, `parent_order_id=<root id>`.
  - A stop-only order is `order_class="simple"`, `order_type="stop"`,
    `leg_role="stop_loss"`.
  - Client ids are `rearm-<sha256[:45]>`.
  - New order status values: `rejected` (definite broker 4xx), and `unknown`
    for an uncertain submission (the existing contract).
- **Ledger changes in `stock_paper_ledger.py`** (small, needed for OCO):
  - New `is_oco_exit_root()` helper.
  - `_exit_leg_row` accepts a same-side child for an OCO root. A bracket leg
    must still be the opposite side.
  - `_exit_leg_parents` accepts OCO roots as parents.
  - Reconciliation's client-id lookup recovers an OCO root's nested legs, the
    same way it does for bracket entries.
  - Without these changes, a timed-out OCO POST would surface its stop child
    as an external `broker_import` order and halt the account.
  - Existing consumers handle OCO groups correctly without changes:
    `broker_stop_coverage`, `_committed_sell_quantity`, `exit_cancel_targets`
    (one DELETE per group, on the active leg), the autotrader's protective-stop
    deferral, the SELL cancel-then-sell path, entry suppression while legs are
    live, the scorecard (`take_profit_leg`/`stop_loss_leg`) and entry
    protections (stop-loss legs).
- **Exactly-once submission**, mirroring `dispatch_reserved_order`:
  - The row is committed as `submitting` before the POST.
  - On success, the response's nested legs are recorded.
  - A broker 4xx other than 408 means the order was not created: the row
    becomes `rejected`, an event is recorded, there is no halt, and the symbol
    backs off.
  - Any other failure (timeout, 5xx) sets the row to `unknown` with
    `uncertain_submission`, halts the account with the standard "outcome is
    uncertain" reason, and stops the run. Reconciliation then finds the order
    by client id and adopts its child. This is tested.

### Kill switch, halts and recovery: decision
- **Recovery cancel/flatten always blocks re-arm.** If the recovery state is
  anything other than `armed`/`resumable` (that is cooldown, paused or
  revalidation_required), nothing is placed. Recovery wants zero resting
  orders, and a re-armed OCO would block flatten.
- **Kill switch on, or account halted:** re-arm **still runs** by default
  (`paper_exit_rearm_while_halted=True`). It only adds protective SELL exits
  capped at broker-available shares and never submits entries. This also
  covers the phase-1 caveat where the kill switch flipped between a SELL's leg
  cancel and its dispatch (gap 1c, tested). Set the flag to false to make the
  kill switch and halts block re-arm.
- Even while halted, it requires all of the following:
  - a broker snapshot younger than 5 minutes,
  - no uncertain (`unknown`/`submitting`) order anywhere on the account,
  - ledger quantity equal to the broker quantity per symbol.

  A reconciliation that fails therefore blocks re-arm.
- The kill switch's existing behaviour is unchanged. Reserve and dispatch still
  refuse every order under it, and nothing was weakened in readiness,
  recovery or risk gates.

### Where it runs
At the end of `stock_paper_broker_reconciliation_job` (every 5 minutes, Alpaca
paper only), right after a fresh reconciliation. It is wrapped so that a
re-arm failure never breaks reconciliation. Its summary is returned under
`exit_rearm`. The autotrader was **not** modified, to avoid conflicts with the
concurrent exit-plan/RSI(2) work. A re-arm call could later be added to the
1-minute cycle for faster coverage.

### New settings (`app/core/config.py`)
- `paper_exit_rearm_enabled` (default true)
- `paper_exit_rearm_while_halted` (default true)
- `paper_exit_rearm_partial_entry_grace_minutes` (default 5, range 1–390)

## 2. Intraday feed readiness flip

**Cause.** In `intraday_data.feed_status`, during the session:

- `expected_end = floor(now - 60s cadence - 60s late allowance) + 1min`, and
  readiness required `latest.opened_at == expected_end - 1min`.
- So bar `m` was required from exactly `m + 2:00`.
- Alpaca IEX publishes bar `m` at about `m + 2:01`, and it is ingested on the
  next 1-minute cycle. The observed example: bar 19:57Z was ingested at
  19:59:01Z.
- Each minute, therefore, the feed was "stale"/"incomplete" from `:00` until
  the ingest and "ready" afterwards.

**Fix.** A new setting, `intraday_publication_lag_seconds`, default 120 and
range 0–300. It is applied on top of the late-trade allowance:

- `current_end` is the old strict boundary. A stored bar at or after it is
  still rejected as a "future" observation. Ingestion (`_latest_completed_window`)
  is unchanged.
- `expected_end` is the newest normally published minute, `current_end`
  lagged by the setting. The missing-interval scan and the stale check use it.
  Execution is `ready` when the newest bar falls in
  `[expected_end - 1min, current_end)`.
- With the default, bar `m` must be present by `m + 4:00` (closed at `m+1`,
  plus 60s, plus 120s).
- A feed that stops updating is still `stale`/`incomplete`, and interior gaps
  still block.
- `feed_status` now also reports `publication_lag_seconds` and
  `expected_latest_open`.
- `readiness.py`, `trusted_data.validate_intraday_readiness` and every
  reserve/dispatch caller go through `feed_status`, so they all inherit the
  fix. Setting the value to 0 restores the strict rule.
- Trade-off: a genuinely stalled feed is detected up to 2 minutes later than
  before.

**Existing test changed:** `test_gap_and_stale_readiness_fail_closed`. Its
13:34 observation with only the 13:30 bar is now within the allowance, so it
moved to 13:36. It still asserts a gap fails closed, with the deferred window
ending at observed minus 3 minutes.

## 3. Control room risk limits
`artifacts/strategy-control-room/src/components/risk-settings-panel.tsx`
(Risk → Limits tab):

- New fields **Position stop-loss** and **Position take-profit** (step 0.005,
  0–0.5, matching `RISK_FIELD_BOUNDS`), each with a short hint. Defaults are
  0.03.
- The form is now built from the field list, so every editable backend risk
  field appears in the UI.
- Out-of-range values are rejected before saving.
- Integer fields are rounded in one helper, and the form reloads from the
  saved rule.
- Saving still uses `updateRiskSettings` → `patchJson` → `authenticatedFetch`,
  which already sends `X-Action-Confirmation: confirm`, a fresh
  `X-Idempotency-Key` and `X-Action-Reason` (taken from the body's `reason`).
  The backend's admin gate and `security.py` checks are unchanged.
- `RiskSettingsUpdate` in `src/lib/api.ts` gained the two keys.

## Files changed
- `artifacts/api-server/backend/app/services/stock_exit_rearm.py` (new)
- `artifacts/api-server/backend/app/services/stock_paper_ledger.py`: OCO root
  leg adoption, about 25 lines.
- `artifacts/api-server/backend/app/services/intraday_data.py`: publication
  lag.
- `artifacts/api-server/backend/app/core/config.py`: four settings.
- `artifacts/api-server/backend/app/tasks/jobs.py`: re-arm after broker
  reconciliation.
- `artifacts/api-server/backend/tests/test_stock_exit_rearm.py` (new, 20 tests)
- `artifacts/api-server/backend/tests/test_intraday_publication_lag.py` (new,
  6 tests)
- `artifacts/api-server/backend/tests/test_intraday_data.py`: one test's
  timestamp moved.
- `artifacts/strategy-control-room/src/components/risk-settings-panel.tsx`,
  `src/lib/api.ts`, `src/components/risk-settings-panel.test.tsx` (new, 5 tests)

## Tests
- New re-arm tests (20) cover:
  - OCO payload shape (`limit`/`oco`/GTC/whole `qty`/TP/SL), root and child
    rows, coverage, one-DELETE cancel target, and idempotent second run.
  - App-sell double-sell blocked.
  - Fractional remainder and sub-share position.
  - Price through the stop; price above TP (stop-only).
  - `qty_available` cap; ledger/broker mismatch.
  - Risk-rule and strategy percentages; stop disabled.
  - Kill switch on (runs); recovery cooldown (blocked); halt flag off
    (blocked); stale snapshot; feature disabled; Tradier.
  - 4xx rejection with no halt, plus backoff.
  - Uncertain POST halts, and reconciliation then adopts the OCO without an
    external-order halt.
  - Live bracket means nothing to re-arm.
  - Gap (c): legs cancelled, kill switch on, OCO armed for the owning
    strategy, reconcile with no `broker_import`, and the OCO stop fill
    attributed to the strategy lot (holdings net 0).
  - A fresh reserved sell defers re-arm, unless the kill switch is on.
  - Gap (b): a stuck partial entry is cancelled after the grace period and
    never overlapped by an OCO.
- New feed tests (6):
  - The strict rule reproduces the flip.
  - With the lag, readiness stays ready for 8 minutes sampled every 5
    seconds, using the observed ingest timing.
  - Ready just before and after the 19:57 bar lands at 19:59:01.
  - A stopped feed goes stale.
  - An interior gap is incomplete.
  - A future bar is rejected.
- Related backend suites (re-arm, bracket exits, ledger, autotrader,
  exploration): **115 passed**.
- Intraday suites (intraday data, no-trade, publication lag, Tradier, IEX
  research, monitoring, trading universe): **106 passed**, plus 15 subtests.
- Control room:
  - `tsc --noEmit`: clean.
  - `npm test`: **39 passed, 0 failed** (5 new).
  - `vite build` (PORT/BASE_PATH set): succeeded.
  - node_modules were symlinked from the main checkout temporarily and the
    links were removed.
- **Full backend suite** (run from the worktree): **1821 passed, 32 skipped,
  0 failed** (206 subtests passed), in 379s.

## Deploy notes
- **No migration.** Re-arm uses the 0063 columns. `rejected` is a new value in
  the free-text `status` column, and `exit_rearm` a new `source` value
  (String(64)).
- **Behaviour change on deploy:**
  - The first reconciliation job after deploy, during or outside market hours,
    may place GTC OCO exits for existing legacy and uncovered positions.
    Alpaca queues GTC orders submitted outside market hours.
  - With the kill switch on, re-arm still places these protective exits. To
    hold off, set `PAPER_EXIT_REARM_ENABLED=false`, or
    `PAPER_EXIT_REARM_WHILE_HALTED=false`.
  - Recommended: one supervised paper check of the OCO response shape. The
    assumptions are that the child is listed in `legs`, the stop child's
    status is `held` or `new`, and one DELETE cancels the group.
- A live OCO holds the shares, so a manual close or reduce is refused with
  "would create a short" until the exits are cancelled. Recovery cancel-all
  does this with one DELETE per group, and the existing SELL path cancels the
  owning lot's legs first. This matches phase-1 bracket legs.
- New entries on a symbol are skipped while any exit legs are live, which
  now includes re-armed OCOs. This is existing autotrader behaviour.
- Readiness now tolerates a 2-minute provider publication lag. Set
  `INTRADAY_PUBLICATION_LAG_SECONDS` to tune it (0 restores the old strict
  rule).
- Not verified against live Alpaca paper. No broker calls or orders were made.
