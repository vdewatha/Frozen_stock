import { expect, test } from "./fixtures/authenticated";

const cycles = [
  {
    cycle_id: "a".repeat(64),
    status: "awaiting_forward_evidence",
    stage: "promotion",
    trigger: "scheduled",
    requested_by: "scheduler",
    symbols: ["SPY"],
    cutoff_date: "2026-09-10",
    horizon_days: 5,
    provider: "yfinance",
    snapshot_id: "snapshot-pending",
    training_job_id: "training-pending",
    model_run_id: "model-pending",
    binding_id: null,
    trial_id: "trial-pending",
    active_binding_id: null,
    active_binding_model_run_id: null,
    handoff: {
      stage: "promotion",
      status: "awaiting_forward_evidence",
      binding_id: null,
      trial_id: "trial-pending",
      trial_status: "running",
      preflight: null,
      reason: "Forward evidence is still pending",
      report_id: null,
      report_decision: null,
    },
    gates: {
      forward_trial: {
        status: "unknown",
        reason: "No completed forward paper trial is linked to this cycle",
      },
      paper_only: { status: "pass", evidence: { live_authorized: false } },
    },
    evidence: {},
    last_reason: "Forward evidence is still pending; no promotion decision has been recorded",
    paper_only: true,
    live_authorized: false,
    automatic_promotion: null,
    monitoring: { snapshot_id: null, status: "unknown", generated_at: null, actions: [] },
    recovery: {
      status: "unknown",
      last_known_good_model_run_id: null,
      last_known_good_binding_id: null,
      latest_event_id: null,
      latest_event_action: null,
    },
    events: [],
    created_at: "2026-09-10T14:00:00Z",
    updated_at: "2026-09-10T14:00:00Z",
  },
  {
    cycle_id: "b".repeat(64),
    status: "blocked",
    stage: "promotion",
    trigger: "scheduled",
    requested_by: "scheduler",
    symbols: ["QQQ"],
    cutoff_date: "2026-09-09",
    horizon_days: 5,
    provider: "yfinance",
    snapshot_id: "snapshot-blocked",
    training_job_id: "training-blocked",
    model_run_id: "model-blocked",
    binding_id: 101,
    trial_id: "trial-blocked",
    active_binding_id: 101,
    active_binding_model_run_id: "model-blocked",
    handoff: {
      stage: "promotion",
      status: "blocked",
      binding_id: 101,
      trial_id: "trial-blocked",
      trial_status: "completed",
      preflight: { status: "blocked", reason: "Fresh monitoring evidence is required before promotion" },
      reason: "Fresh monitoring evidence is required before promotion",
      report_id: 2,
      report_decision: "unknown",
    },
    gates: {
      validation: { status: "fail", reason: "Registered validation evidence is incomplete" },
      forward_trial: { status: "unknown", reason: "Fresh monitoring evidence is required before promotion" },
      paper_only: { status: "pass", evidence: { live_authorized: false } },
    },
    evidence: {},
    last_reason: "Fresh monitoring evidence is required before promotion",
    paper_only: true,
    live_authorized: false,
    automatic_promotion: {
      id: 2,
      cycle_id: "b".repeat(64),
      trial_id: "trial-blocked",
      report_id: 2,
      model_run_id: "model-blocked",
      snapshot_id: "snapshot-blocked",
      decision: "blocked",
      gates: {},
      lineage: {},
      evidence: {},
      actor: "automatic_recovery",
      source_job: "scheduled_learning_cycle",
      correlation_id: "b".repeat(64),
      reason: "Fresh monitoring evidence is required before promotion",
      before_binding_id: 101,
      before_model_run_id: "model-blocked",
      after_binding_id: null,
      after_model_run_id: null,
      decision_sha256: "blocked-decision",
      paper_only: true,
      live_authorized: false,
      created_at: "2026-09-10T15:00:00Z",
    },
    monitoring: { snapshot_id: null, status: "blocked", generated_at: null, actions: [] },
    recovery: {
      status: "armed",
      last_known_good_model_run_id: "model-blocked",
      last_known_good_binding_id: 101,
      latest_event_id: null,
      latest_event_action: null,
    },
    events: [],
    created_at: "2026-09-09T14:00:00Z",
    updated_at: "2026-09-10T15:00:00Z",
  },
  {
    cycle_id: "c".repeat(64),
    status: "promoted",
    stage: "promotion",
    trigger: "scheduled",
    requested_by: "scheduler",
    symbols: ["AAPL"],
    cutoff_date: "2026-09-08",
    horizon_days: 5,
    provider: "yfinance",
    snapshot_id: "snapshot-promoted",
    training_job_id: "training-promoted",
    model_run_id: "model-promoted",
    binding_id: 204,
    trial_id: "trial-promoted",
    active_binding_id: 204,
    active_binding_model_run_id: "model-promoted",
    handoff: {
      stage: "promotion",
      status: "promoted",
      binding_id: 204,
      trial_id: "trial-promoted",
      trial_status: "completed",
      preflight: { status: "pass", reason: "Paper preflight passed" },
      reason: "Every immutable paper promotion gate passed",
      report_id: 3,
      report_decision: "pass",
    },
    gates: {
      validation: { status: "pass" },
      forward_trial: { status: "pass" },
      paper_only: { status: "pass", evidence: { live_authorized: false } },
    },
    evidence: {},
    last_reason: "Every immutable paper promotion gate passed",
    paper_only: true,
    live_authorized: false,
    automatic_promotion: {
      id: 3,
      cycle_id: "c".repeat(64),
      trial_id: "trial-promoted",
      report_id: 3,
      model_run_id: "model-promoted",
      snapshot_id: "snapshot-promoted",
      decision: "promoted",
      gates: {},
      lineage: {},
      evidence: {},
      actor: "scheduled_promotion",
      source_job: "scheduled_learning_cycle",
      correlation_id: "c".repeat(64),
      reason: "Every immutable paper promotion gate passed",
      before_binding_id: 103,
      before_model_run_id: "model-old",
      after_binding_id: 204,
      after_model_run_id: "model-promoted",
      decision_sha256: "promoted-decision",
      paper_only: true,
      live_authorized: false,
      created_at: "2026-09-10T16:00:00Z",
    },
    monitoring: { snapshot_id: 4, status: "clear", generated_at: "2026-09-10T16:00:00Z", actions: [] },
    recovery: {
      status: "armed",
      last_known_good_model_run_id: "model-old",
      last_known_good_binding_id: 103,
      latest_event_id: null,
      latest_event_action: null,
    },
    events: [],
    created_at: "2026-09-08T14:00:00Z",
    updated_at: "2026-09-10T16:00:00Z",
  },
  {
    cycle_id: "d".repeat(64),
    status: "demoted",
    stage: "recovery",
    trigger: "scheduled",
    requested_by: "watchdog",
    symbols: ["MSFT"],
    cutoff_date: "2026-09-07",
    horizon_days: 5,
    provider: "yfinance",
    snapshot_id: "snapshot-demoted",
    training_job_id: "training-demoted",
    model_run_id: "model-demoted",
    binding_id: 305,
    trial_id: "trial-demoted",
    active_binding_id: 41,
    active_binding_model_run_id: "model-prior",
    handoff: {
      stage: "recovery",
      status: "demoted",
      binding_id: 305,
      trial_id: "trial-demoted",
      trial_status: "completed",
      preflight: { status: "blocked", reason: "Monitoring evidence failed after promotion" },
      reason: "Recovery restored the prior paper binding after a monitoring pause",
      report_id: 4,
      report_decision: "fail",
    },
    gates: {
      monitoring: { status: "fail", reason: "Monitoring evidence failed after promotion" },
      paper_only: { status: "pass", evidence: { live_authorized: false } },
    },
    evidence: {},
    last_reason: "Recovery restored the prior paper binding after a monitoring pause",
    paper_only: true,
    live_authorized: false,
    automatic_promotion: null,
    monitoring: { snapshot_id: 5, status: "blocked", generated_at: "2026-09-10T17:00:00Z", actions: ["demote_model"] },
    recovery: {
      status: "recovered",
      last_known_good_model_run_id: "model-prior",
      last_known_good_binding_id: 41,
      latest_event_id: 8,
      latest_event_action: "demote_model",
    },
    events: [],
    created_at: "2026-09-07T14:00:00Z",
    updated_at: "2026-09-10T17:00:00Z",
  },
  {
    cycle_id: "e".repeat(64),
    status: "rolled_back",
    stage: "recovery",
    trigger: "manual",
    requested_by: "operator",
    symbols: ["NVDA"],
    cutoff_date: "2026-09-06",
    horizon_days: 5,
    provider: "yfinance",
    snapshot_id: "snapshot-rollback",
    training_job_id: "training-rollback",
    model_run_id: "model-rollback",
    binding_id: 406,
    trial_id: "trial-rollback",
    active_binding_id: 40,
    active_binding_model_run_id: "model-last-known-good",
    handoff: {
      stage: "recovery",
      status: "rolled_back",
      binding_id: 406,
      trial_id: "trial-rollback",
      trial_status: "completed",
      preflight: { status: "blocked", reason: "Rollback required after model demotion" },
      reason: "Rollback restored the last-known-good paper binding",
      report_id: 5,
      report_decision: "fail",
    },
    gates: {
      recovery: { status: "blocked", reason: "Rollback required after model demotion" },
      paper_only: { status: "pass", evidence: { live_authorized: false } },
    },
    evidence: {},
    last_reason: "Rollback restored the last-known-good paper binding",
    paper_only: true,
    live_authorized: false,
    automatic_promotion: null,
    monitoring: { snapshot_id: 6, status: "blocked", generated_at: "2026-09-10T18:00:00Z", actions: ["rollback"] },
    recovery: {
      status: "rolled_back",
      last_known_good_model_run_id: "model-last-known-good",
      last_known_good_binding_id: 40,
      latest_event_id: 9,
      latest_event_action: "rollback",
    },
    events: [],
    created_at: "2026-09-06T14:00:00Z",
    updated_at: "2026-09-10T18:00:00Z",
  },
];

test("operator sees pending, blocked, promoted, recovery, and live-disabled promotion states", async ({
  page,
  authenticateAs,
}) => {
  await page.route("**/api/stock/learning-cycles*", route => {
    if (route.request().method() !== "GET") {
      throw new Error(`Unsafe learning-cycle mutation attempted: ${route.request().method()}`);
    }
    return route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(cycles),
    });
  });

  await authenticateAs("operator");

  const panel = page.getByTestId("stock-learning-cycle-panel");
  await expect(panel.getByText("Cycle evidence refreshed")).toBeVisible();

  const pending = panel.getByTestId(`stock-learning-cycle-${"a".repeat(64)}`);
  await expect(pending.getByText("awaiting forward evidence", { exact: true }).first()).toBeVisible();
  await expect(pending).toContainText("forward trial");
  await expect(pending).toContainText("· unknown");
  await expect(pending).toContainText("pending");
  await expect(pending).toContainText("missing evidence is not a pass");
  await expect(pending).toContainText("Forward evidence is still pending; no promotion decision has been recorded");
  await expect(pending).not.toContainText(/\bpromoted\b/);

  const blocked = panel.getByTestId(`stock-learning-cycle-${"b".repeat(64)}`);
  await expect(blocked.getByText("blocked", { exact: true }).first()).toBeVisible();
  await expect(blocked.getByText("promotion", { exact: true }).first()).toBeVisible();
  await expect(blocked).toContainText("Fresh monitoring evidence is required before promotion");
  await expect(blocked).toContainText("Automatic paper promotion");
  await expect(blocked.getByText("blocked", { exact: true })).toHaveCount(3);
  await expect(blocked).not.toContainText("missing evidence is not a pass");

  const promoted = panel.getByTestId(`stock-learning-cycle-${"c".repeat(64)}`);
  await expect(promoted.getByText("promoted", { exact: true }).first()).toBeVisible();
  await expect(promoted).toContainText("Every immutable paper promotion gate passed");
  await expect(promoted).toContainText("scheduled_promotion · scheduled_learning_cycle · binding 204");

  const demoted = panel.getByTestId(`stock-learning-cycle-${"d".repeat(64)}`);
  await expect(demoted.getByText("demoted", { exact: true }).first()).toBeVisible();
  await expect(demoted.getByText("recovery", { exact: true }).first()).toBeVisible();
  await expect(demoted).toContainText("Recovery restored the prior paper binding after a monitoring pause");

  const rolledBack = panel.getByTestId(`stock-learning-cycle-${"e".repeat(64)}`);
  await expect(rolledBack.getByText("rolled back", { exact: true }).first()).toBeVisible();
  await expect(rolledBack).toContainText("Rollback restored the last-known-good paper binding");

  await expect(panel).toContainText("paper-only · live trading disabled");
});