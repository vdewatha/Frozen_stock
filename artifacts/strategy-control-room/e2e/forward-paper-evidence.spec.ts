import { expect, test, type TrialRole } from "./fixtures/authenticated";

const trialId = "00000000-0000-0000-0000-000000000071";
const reportId = 71;
const reportHash = "sha256:task71-report-v3";
const trial = {
  id: trialId,
  status: "completed",
  binding_id: 71,
  policy: {
    regular_sessions: 20,
    minimum_closed_trades: 30,
    minimum_decision_coverage: "0.90",
    paper_only: true,
    live_authorized: false,
  },
  lineage: {
    model_run_id: "model-run-71",
    model_hash: "model-task71",
    snapshot_id: "snapshot-71",
    dataset_sha256: "dataset-71",
    cutoff_date: "2026-08-31",
    universe: ["SPY", "QQQ"],
    cost_assumptions: { commission: "unknown" },
    binding_hash: "binding-71",
    policy_sha256: "policy-71",
    lineage_sha256: "lineage-71",
  },
  blocked_reason: null,
  pause_reason: null,
  started_at: "2026-09-01T13:30:00Z",
  stopped_at: "2026-09-02T20:00:00Z",
};

// This is the persisted `session_evidence` shape produced by
// build_trial_session_evidence, not a client-side substitute schema.
const sessionEvidence = {
  version: 1,
  as_of: "2026-09-02T20:00:00Z",
  window: {
    started_at: trial.started_at,
    regular_sessions_required: 20,
    elapsed_regular_sessions: 2,
    complete: false,
    session_dates: ["2026-09-01", "2026-09-02"],
  },
  sessions: [
    {
      session_date: "2026-09-01",
      opened_at: "2026-09-01T13:30:00Z",
      closed_at: "2026-09-01T20:00:00Z",
      status: "elapsed",
      symbols: [
        {
          symbol: "SPY",
          expected_decisions: 1,
          observed_decisions: 1,
          verified_decisions: 1,
          duplicate_exclusions: 0,
          status: "verified",
          missing_reason: null,
          rejected_reason: null,
          historical_feed_health: { status: "verified", reason: null },
          execution: {
            status: "linked",
            order_id: 71,
            client_order_id: "trial-order-71",
            order_status: "filled",
            lot_id: 71,
            fill_ids: [71],
            broker_activity_ids: ["activity-71"],
            broker_order_ids: ["broker-order-71"],
            costs_known: false,
          },
        },
        {
          symbol: "QQQ",
          expected_decisions: 1,
          observed_decisions: 0,
          verified_decisions: 0,
          duplicate_exclusions: 0,
          status: "rejected",
          missing_reason: null,
          rejected_reason: "stale_feature",
          historical_feed_health: { status: "unknown", reason: "historical_feature_timestamp_unavailable" },
          execution: { status: "not_applicable" },
        },
      ],
    },
    {
      session_date: "2026-09-02",
      opened_at: "2026-09-02T13:30:00Z",
      closed_at: "2026-09-02T20:00:00Z",
      status: "elapsed",
      symbols: [
        {
          symbol: "SPY",
          expected_decisions: 1,
          observed_decisions: 1,
          verified_decisions: 1,
          duplicate_exclusions: 0,
          status: "verified",
          missing_reason: null,
          rejected_reason: null,
          historical_feed_health: { status: "verified", reason: null },
          execution: { status: "not_applicable" },
        },
        {
          symbol: "QQQ",
          expected_decisions: 1,
          observed_decisions: 1,
          verified_decisions: 1,
          duplicate_exclusions: 0,
          status: "verified",
          missing_reason: null,
          rejected_reason: null,
          historical_feed_health: { status: "verified", reason: null },
          execution: { status: "not_applicable" },
        },
      ],
    },
  ],
  aggregates: {
    expected_decisions: 4,
    observed_decisions: 3,
    verified_decisions: 3,
    missing_decisions: 1,
    rejected_decisions: 1,
    duplicate_exclusions: 0,
    unknown_historical_feed_health: 1,
    decision_coverage: "0.75",
    verified_decision_coverage: "0.75",
  },
  source_lineage: trial.lineage,
};

// This is the exact persisted promotion-readiness report envelope returned by
// the backend's _report_out helper.
const readinessGates = {
    regular_sessions: { status: "fail", value: "2", required: "20", reason: "required verified sessions are not complete" },
  decision_coverage: { status: "fail", value: "0.75", required: "0.90", reason: "coverage evidence is incomplete or below the frozen threshold" },
  historical_feed_health: { status: "unknown", value: "1", required: "0", reason: "historical feed or health evidence is unavailable; it was not reconstructed" },
  broker_costs: { status: "unknown", reason: "broker cost evidence is unknown" },
};
const readinessReport = {
  id: reportId,
  version: 3,
  trial_id: trialId,
  source_metric_id: 71,
  as_of: "2026-09-02T20:00:00Z",
  report_hash: reportHash,
  decision: "unknown",
  gates: readinessGates,
  lineage: trial.lineage,
  policy: trial.policy,
  evidence: {
    version: 1,
    trial_id: trialId,
    source_metric_id: 71,
    source_metric_as_of: "2026-09-02T20:00:00Z",
    as_of: "2026-09-02T20:00:00Z",
    decision: "unknown",
    gates: readinessGates,
    lineage: trial.lineage,
    policy: trial.policy,
    aggregate_metrics: {
      observed_sessions: 2,
      elapsed_regular_sessions: 2,
      evidenced_sessions: 1,
      expected_observations: 4,
      observed_observations: 3,
      decision_coverage: "0.75",
      verified_decision_coverage: "0.75",
      session_evidence: sessionEvidence,
      closed_trades: 2,
      costs_known: false,
    },
    session_evidence: sessionEvidence,
    paper_only: true,
    live_authorized: false,
    promotion_authorized: false,
  },
  paper_only: true,
  live_authorized: false,
  created_at: "2026-09-02T20:01:00Z",
  promotion_authorized: false,
};

test.beforeEach(async ({ page }) => {
  await page.route(`**/api/stock/forward-trials/${trialId}/metrics`, route => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({ items: [{
      as_of: "2026-09-02T20:00:00Z",
      classification: "insufficient",
      payload: {
        closed_trades: 2,
        win_rate: null,
        net_pnl: null,
        provisional_gross_pnl: "10.00",
        expectancy: null,
        max_drawdown: null,
        benchmark_buy_hold: null,
        observed_observations: 3,
        expected_observations: 4,
        observed_sessions: 5,
        decision_coverage: "0.75",
        costs_known: false,
      },
    }] }),
  }));
  await page.route(`**/api/stock/forward-trials/${trialId}/decisions`, route => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({ items: [] }),
  }));
  await page.route(`**/api/stock/forward-trials/${trialId}/preflight`, route => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      status: "ready",
      ready: true,
      checked_at: "2026-09-02T19:59:00Z",
      regular_session: true,
      next_regular_session_open: null,
      symbols: [],
      paper_ledger: { status: "reconciled", reason: null },
      reason: null,
      paper_only: true,
      live_authorized: false,
    }),
  }));
  await page.route(`**/api/stock/forward-trials/${trialId}/promotion-readiness*`, route => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify(readinessReport),
  }));
  await page.route(`**/api/stock/forward-trials/${trialId}/promotion-readiness/reports*`, route => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({ items: [readinessReport] }),
  }));
  await page.route(`**/api/stock/forward-trials/${trialId}/promotion-readiness/reports/${reportId}/download`, route => route.fulfill({
    contentType: "application/json",
    headers: { "content-disposition": `attachment; filename="promotion-readiness-${reportId}.json"` },
    body: JSON.stringify(readinessReport),
  }));
  await page.route("**/api/stock/forward-trials/bindings/eligible", route => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({ items: [] }),
  }));
  await page.route("**/api/stock/forward-trials", route => {
    if (route.request().method() !== "GET") {
      throw new Error(`Unsafe forward-trial mutation attempted: ${route.request().method()}`);
    }
    return route.fulfill({ contentType: "application/json", body: JSON.stringify({ items: [trial] }) });
  });
});

for (const role of ["viewer", "operator", "admin"] as TrialRole[]) {
  test(`${role} can inspect session evidence without receiving authorization`, async ({ page, authenticateAs }) => {
    await authenticateAs(role);
    await page.getByTestId(`forward-trial-row-${trialId}`).click();

    const detail = page.getByTestId(`forward-trial-detail-${trialId}`);
    await expect(detail.getByTestId(`metric-forward-evidence-elapsed-sessions-${trialId}`)).toContainText("2");
    await expect(detail.getByTestId(`metric-forward-evidence-evidenced-sessions-${trialId}`)).toContainText("1");
    await expect(detail.getByTestId(`metric-forward-evidence-expected-decisions-${trialId}`)).toContainText("4");
    await expect(detail.getByTestId(`metric-forward-evidence-observed-decisions-${trialId}`)).toContainText("3");
    await detail.getByTestId(`forward-evidence-session-${trialId}-2026-09-01`).locator("summary").click();
    await expect(detail.getByTestId(`text-forward-evidence-session-rejected-reasons-${trialId}-2026-09-01`)).toContainText("stale_feature");
    await expect(detail.getByTestId(`text-forward-evidence-expected-${trialId}-2026-09-01-SPY`)).toContainText("1");
    await expect(detail.getByTestId(`text-forward-evidence-source-${trialId}-2026-09-01-SPY-0`)).toContainText("order id 71");
    await expect(detail).toContainText("Trial-owned execution evidence:");
    await expect(detail.getByTestId(`notice-forward-evidence-costs-unknown-${trialId}`)).toContainText("net performance is unavailable");
    await expect(detail.getByTestId(`notice-forward-evidence-history-unknown-${trialId}`)).toContainText("not reconstructed as verified");
    await expect(detail.getByTestId(`text-forward-trial-report-lineage-${trialId}-${reportId}`)).toContainText("model-run-71");
    await expect(detail.getByTestId(`text-forward-trial-report-sources-${trialId}-${reportId}`)).toContainText("model-run-71");
    await expect(detail.getByTestId(`status-forward-trial-report-safety-${trialId}`)).toContainText("no promotion, resume, or live authorization");
  });
}

test("report download retrieves the selected immutable version", async ({ page, authenticateAs }) => {
  await authenticateAs("viewer");
  await page.getByTestId(`forward-trial-row-${trialId}`).click();

  const downloadPromise = page.waitForEvent("download");
  await page.getByTestId(`button-download-forward-trial-report-${trialId}-${reportId}`).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe(`promotion-readiness-${reportId}.json`);
});