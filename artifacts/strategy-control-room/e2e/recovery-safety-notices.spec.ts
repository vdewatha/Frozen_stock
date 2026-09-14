import { expect, test } from "./fixtures/authenticated";

const recovery = {
  status: "revalidation_required",
  flatten_policy: "none",
  cooldown_until: null,
  pause_reason: "Fresh monitoring evidence is required before recovery",
  last_known_good_model_run_id: "model-run-fixture",
  last_known_good_binding_id: 41,
  last_monitor_heartbeat_at: "2026-09-12T15:59:00Z",
  last_watchdog_heartbeat_at: "2026-09-12T15:58:00Z",
  last_revalidation_at: null,
  accounting_review_required: false,
  accounting_reviewed_at: null,
  accounting_reviewed_by: null,
  accounting_review_reason: null,
  automatic_review_enabled: true,
  automatic_review_status: "not_required",
  account_status: "reconciled",
  accounting_residual: false,
  account_reconciliation_required: false,
  account_halt_reason: null,
  accounting_verified: true,
  costs_known: true,
  last_monitoring_preflight: {
    status: "blocked",
    actor: "automatic_recovery",
    reason: "Fresh monitoring evidence is not clear",
    created_at: "2026-09-12T16:00:01Z",
    payload: {
      checked_at: "2026-09-12T16:00:00Z",
    },
  },
  events: [],
};

test("operator keeps the persisted blocked monitoring notice after refresh", async ({ page, authenticateAs }) => {
  let recoveryFetches = 0;
  await page.route("**/api/stock-paper/recovery", route => {
    if (route.request().method() !== "GET") {
      throw new Error(`Unsafe recovery mutation attempted: ${route.request().method()}`);
    }
    recoveryFetches += 1;
    return route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(recovery),
    });
  });

  await authenticateAs("operator");

  const panel = page.getByTestId("panel-stock-recovery");
  const notice = panel.getByTestId("monitoring-preflight-block");
  const expectedCheckedTime = await page.evaluate(() => new Date("2026-09-12T16:00:00Z").toLocaleString());
  await expect(notice).toBeVisible();
  await expect(notice).toContainText("Status: blocked");
  await expect(notice).toContainText("Reason: Fresh monitoring evidence is not clear");
  await expect(notice).toContainText(`Checked: ${expectedCheckedTime}`);
  await expect.poll(() => recoveryFetches).toBeGreaterThan(0);

  await expect(panel.getByRole("button", { name: "Cancel orders" })).toBeVisible();
  await expect(panel.getByRole("button", { name: "Cancel and flatten" })).toBeVisible();
  await expect(panel.getByRole("button", { name: "Roll back model" })).toBeVisible();

  await panel.getByRole("button", { name: "Refresh" }).click();
  await expect.poll(() => recoveryFetches).toBeGreaterThan(1);
  await expect(notice).toBeVisible();
  await expect(notice).toContainText("Status: blocked");
  await expect(notice).toContainText("Reason: Fresh monitoring evidence is not clear");
  await expect(notice).toContainText(`Checked: ${expectedCheckedTime}`);
});

test("operator sees persisted clear monitoring evidence after refresh", async ({ page, authenticateAs }) => {
  const clearRecovery = {
    ...recovery,
    last_monitoring_preflight: {
      status: "clear",
      actor: "stock_recovery_automation",
      reason: "Monitoring evidence is fresh and clear",
      created_at: "2026-09-12T16:05:01Z",
      payload: {
        checked_at: "2026-09-12T16:05:00Z",
      },
    },
  };
  let recoveryFetches = 0;
  await page.route("**/api/stock-paper/recovery", route => {
    recoveryFetches += 1;
    return route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(clearRecovery),
    });
  });

  await authenticateAs("operator");

  const panel = page.getByTestId("panel-stock-recovery");
  const notice = panel.getByTestId("monitoring-preflight-clear");
  const expectedCheckedTime = await page.evaluate(() => new Date("2026-09-12T16:05:00Z").toLocaleString());
  await expect(notice).toBeVisible();
  await expect(notice).toContainText("Fresh monitoring evidence cleared the recovery block");
  await expect(notice).toContainText("Status: clear");
  await expect(notice).toContainText("Reason: Monitoring evidence is fresh and clear");
  await expect(notice).toContainText(`Checked: ${expectedCheckedTime}`);
  await expect.poll(() => recoveryFetches).toBeGreaterThan(0);

  await panel.getByRole("button", { name: "Refresh" }).click();
  await expect.poll(() => recoveryFetches).toBeGreaterThan(1);
  await expect(notice).toBeVisible();
  await expect(notice).toContainText("Status: clear");
  await expect(notice).toContainText(`Checked: ${expectedCheckedTime}`);
});