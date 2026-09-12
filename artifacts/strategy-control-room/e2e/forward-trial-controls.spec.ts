import { expect, test, type TrialRole } from "./fixtures/authenticated";

const trialId = "00000000-0000-0000-0000-000000000015";
const trial = {
  id: trialId,
  status: "paused",
  binding_id: 41,
  policy: {
    regular_sessions: 20,
    minimum_closed_trades: 30,
    minimum_decision_coverage: "0.90",
    paper_only: true,
    live_authorized: false,
  },
  lineage: {
    model_hash: "model-hash-fixture",
    cutoff_date: "2026-08-31",
    universe: ["SPY", "QQQ"],
  },
  blocked_reason: null,
  pause_reason: "fresh_complete_feed_required:SPY:sip_entitlement_unavailable",
  started_at: "2026-09-01T14:30:00Z",
  stopped_at: null,
};

test.beforeEach(async ({ page }) => {
  await page.route(`**/api/stock/forward-trials/${trialId}/metrics`, route => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({ items: [{
      as_of: "2026-09-10T21:00:00Z",
      classification: "accumulating",
      payload: {
        closed_trades: 0,
        win_rate: null,
        net_pnl: null,
        provisional_gross_pnl: null,
        expectancy: null,
        max_drawdown: null,
        benchmark_buy_hold: null,
        observed_observations: 3,
        expected_observations: 20,
        observed_sessions: 3,
        decision_coverage: null,
        costs_known: false,
      },
    }] }),
  }));
  await page.route(`**/api/stock/forward-trials/${trialId}/decisions`, route => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({ items: [{
      id: "decision-fixture",
      symbol: "SPY",
      bar_timestamp: "2026-09-10T20:59:00Z",
      action: "reject",
      qualifying: false,
      rejection_reason: "feed_entitlement_unavailable",
      lineage: trial.lineage,
      order_id: null,
    }] }),
  }));
  await page.route("**/api/stock/forward-trials/bindings/eligible", route => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({ items: [{
      binding_id: 42,
      model_run_id: "run-fixture",
      snapshot_id: "snapshot-fixture",
      eligible: true,
      reason: null,
      paper_only: true,
      live_authorized: false,
    }] }),
  }));
  await page.route("**/api/stock/forward-trials", route => {
    if (route.request().method() !== "GET") {
      throw new Error(`Unsafe forward-trial mutation attempted: ${route.request().method()}`);
    }
    return route.fulfill({ contentType: "application/json", body: JSON.stringify({ items: [trial] }) });
  });
});

for (const role of ["viewer", "operator", "admin"] as TrialRole[]) {
  test(`${role} sees the paused evidence and only permitted controls`, async ({ page, authenticateAs }) => {
    await authenticateAs(role);
    await page.getByTestId(`forward-trial-row-${trialId}`).click();

    const detail = page.getByTestId(`forward-trial-detail-${trialId}`);
    await expect(detail).toContainText("fresh_complete_feed_required:SPY:sip_entitlement_unavailable");
    await expect(detail).toContainText("model-hash-fixture");
    await expect(detail).toContainText("2026-08-31");
    await expect(detail).toContainText('["SPY","QQQ"]');
    await expect(detail).toContainText("feed_entitlement_unavailable");
    await expect(detail.getByText("Unavailable", { exact: true })).toHaveCount(7);
    await expect(detail).toContainText("Costs are unknown; performance may be optimistic.");

    const canOperate = role === "operator" || role === "admin";
    await expect(page.getByTestId("forward-trial-operator-controls")).toHaveCount(canOperate ? 1 : 0);
    await expect(page.getByTestId("button-resume-forward-trial")).toHaveCount(canOperate ? 1 : 0);
    await expect(page.getByTestId("button-stop-forward-trial")).toHaveCount(canOperate ? 1 : 0);
    await expect(page.getByTestId("forward-trial-admin-surface")).toHaveCount(role === "admin" ? 1 : 0);
    await expect(page.getByTestId("forward-trial-admin-controls")).toHaveCount(role === "admin" ? 1 : 0);
  });
}