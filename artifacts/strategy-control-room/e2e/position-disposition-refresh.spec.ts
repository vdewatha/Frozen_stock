import { expect, test } from "./fixtures/authenticated";

const cycleId = "203".repeat(21) + "2";

const cycle = {
  cycle_id: cycleId,
  status: "awaiting_forward_evidence",
  stage: "preflight",
  trigger: "scheduled",
  requested_by: "scheduler",
  symbols: ["SPY"],
  cutoff_date: "2026-09-17",
  horizon_days: 5,
  provider: "yfinance",
  snapshot_id: "snapshot-203",
  training_job_id: "training-203",
  model_run_id: "model-203",
  binding_id: 203,
  trial_id: "trial-203",
  active_binding_id: 203,
  active_binding_model_run_id: "model-203",
  handoff: {
    stage: "preflight",
    status: "awaiting_forward_evidence",
    binding_id: 203,
    trial_id: "trial-203",
    trial_status: "stopped",
    preflight: null,
    reason: "Forward evidence is still pending",
    report_id: null,
    report_decision: null,
    approval: {
      status: "pass",
      reason: null,
      record: null,
      expected: {
        environment: "paper",
        execution_provider: "alpaca_paper",
        provider_switch: null,
        symbols: ["SPY"],
        exposure_limits: { max_order_notional: "2500" },
        loss_limits: { max_loss: "0.02", unit: "fraction_of_baseline_equity" },
        duration_sessions: 1,
        schedule: {
          session_date: "2026-09-17",
          timezone: "America/New_York",
          start_at: "2026-09-17T09:30:00-04:00",
          end_at: "2026-09-17T16:00:00-04:00",
        },
        stop_conditions: ["paper_session_expired", "operator_stop"],
        stop_authority: "operator_and_system",
        pending_order_treatment: "cancel",
        remaining_position_policy: "flatten",
      },
      paper_only: true,
      live_authorized: false,
    },
  },
  position_handling: {
    approved_policy: "flatten",
    stop_status: "expired",
    stop_reason: "paper_session_expired",
    stopped_at: "2026-09-17T20:00:00Z",
    new_entries_stopped: true,
    handling_status: "flatten_pending",
    remaining_positions: [{ symbol: "SPY", quantity: "2.00000000", market_value: "202.00", observed_at: "2026-09-17T20:00:00Z" }],
    managed_lots: [{
      symbol: "SPY",
      entry_quantity: "2.00000000",
      exited_quantity: "1.00000000",
      remaining_quantity: "1.00000000",
      exit_reason: "paper_session_expired",
      exit_status: "partially_filled",
      exit_decided_at: "2026-09-17T20:00:00Z",
    }],
    has_exit_intent: true,
    reconciliation: {
      status: "reconciled",
      reconciliation_required: false,
      last_reconciled_at: "2026-09-17T20:00:00Z",
    },
  },
  gates: {},
  evidence: {},
  last_reason: "Paper session expired; entries stopped while flattening remains pending",
  paper_only: true,
  live_authorized: false,
  automatic_promotion: null,
  monitoring: {
    snapshot_id: 203,
    status: "clear",
    generated_at: "2026-09-17T20:00:00Z",
    actions: [],
  },
  recovery: {
    status: "armed",
    last_known_good_model_run_id: null,
    last_known_good_binding_id: null,
    latest_event_id: null,
    latest_event_action: null,
  },
  events: [],
  created_at: "2026-09-17T19:00:00Z",
  updated_at: "2026-09-17T20:00:00Z",
};

test("authenticated monitoring keeps stopped entries and reconciliation disposition visible", async ({
  page,
  authenticateAs,
}) => {
  await page.route("**/api/auth/config", route => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      mode: "local_role_keys",
      requires_identity_provider: false,
      paper_only: true,
      live_orders_allowed: false,
    }),
  }));
  await page.route("**/api/dashboard", route => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      paper_account_value: null,
      daily_pl: null,
      total_pl: null,
      metrics_status: "unknown",
      performance_note: "Costs are unknown",
      active_strategies: 0,
      paused_strategies: 0,
      best_strategy: null,
      worst_strategy: null,
      open_paper_trades: 0,
      risk_state: "blocked",
      equity_curve: [],
      strategies: [],
      recent_trades: [],
      experiments: [],
      risk_rules: [],
      generated_at: "2026-09-17T20:00:00Z",
    }),
  }));
  await page.route("**/api/auth/session", route => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      role: "viewer",
      identity: "viewer",
      permissions: ["viewer"],
      auth_method: "local_role_key",
      environment: "paper",
      live_mode: "blocked",
      live_orders_allowed: false,
    }),
  }));
  await page.route("**/api/stock/learning-cycles/**", route => {
    if (route.request().method() !== "GET") {
      throw new Error(`Unsafe learning-cycle mutation attempted: ${route.request().method()}`);
    }
    const pathname = new URL(route.request().url()).pathname;
    if (pathname.endsWith("/schedule-control")) {
      return route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          paused: false,
          pause_reason: null,
          updated_by: "scheduler",
          updated_at: "2026-09-17T19:00:00Z",
          paper_only: true,
          live_authorized: false,
          recovery_independent: true,
        }),
      });
    }
    if (pathname.endsWith("/launch-prerequisites")) {
      return route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          cycle_id: cycleId,
          checked_at: "2026-09-17T20:00:00Z",
          status: "blocked",
          eligible_for_approval: false,
          reason: "Entries are stopped after paper-session expiry",
          gates: {},
        }),
      });
    }
    throw new Error(`Unexpected learning-cycle read: ${pathname}`);
  });
  await page.route("**/api/stock/learning-cycles*", route => {
    if (route.request().method() !== "GET") {
      throw new Error(`Unsafe learning-cycle mutation attempted: ${route.request().method()}`);
    }
    return route.fulfill({
      contentType: "application/json",
      body: JSON.stringify([cycle]),
    });
  });

  await authenticateAs("viewer");

  const panel = page.getByTestId("stock-learning-cycle-panel");
  const card = panel.getByTestId(`stock-learning-cycle-${cycleId}`);
  await expect(panel.getByText("Cycle evidence refreshed")).toBeVisible();
  await expect(card).toContainText("flatten pending");
  await expect(card).toContainText("Approved remaining-position policy: flatten");
  await expect(card).toContainText("Expiry / operator stop: expired · paper_session_expired");
  await expect(card).toContainText("New entries: stopped");
  await expect(card).toContainText("Monitoring / reconciliation: clear / reconciled");
  await expect(card).toContainText("SPY 2.00000000");
  await expect(card).toContainText("Exit reason: paper session expired");
});