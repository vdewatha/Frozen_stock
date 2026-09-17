import type { Locator, Page } from "@playwright/test";
import type { AgentResearchRun } from "../src/lib/api";
import { expect, test } from "./fixtures/authenticated";

// Authentication and the viewer denial use the real API. Research responses
// are deterministic fixtures; no provider, Celery, broker or trading writes.
const endpoint = "/api/research/agent-runs";
const heading = "TradingAgents Shadow Research";
const completedResult = {
  recommendation: "HOLD" as const,
  confidence: 0.55,
  rationale: "Browser fixture: bounded context is mixed.",
  limitations: ["Research-only browser fixture"],
};

function run(status: AgentResearchRun["status"] = "queued"): AgentResearchRun {
  return {
    run_id: "00000000-0000-0000-0000-000000000210",
    symbol: "MSFT",
    status,
    eligible_for_trading: false,
    framework_version: "tradingagents-v0.4.0",
    prompt_version: "shadow-research-v1",
    model_name: "fixture-provider",
    requested_by: "researcher",
    source_snapshot: [],
    result: status === "completed" ? completedResult : null,
    evaluation: { status: "pending", horizon_days: 5, comparison: { status: "pending" } },
    usage: {},
    error: null,
    created_at: "2026-09-16T15:00:00Z",
    started_at: null,
    completed_at: null,
  };
}

function panel(page: Page) {
  return page.locator("section").filter({ has: page.getByRole("heading", { name: heading, exact: true }) });
}

async function expectResearchOnly(section: Locator, canRun: boolean) {
  await expect(section).toContainText("no broker, order, risk, approval, or model-promotion authority");
  // An allowlist catches newly introduced execution actions, not only names
  // of today's buttons. Submission is the panel's only permitted action.
  await expect(section.getByRole("button")).toHaveCount(canRun ? 1 : 0);
  if (canRun) await expect(section.getByRole("button", { name: "Run research", exact: true })).toBeVisible();
  await expect(section.getByRole("link")).toHaveCount(0);
  await expect(section.getByRole("checkbox")).toHaveCount(0);
  await expect(section.getByRole("switch")).toHaveCount(0);
}

async function mockResearch(page: Page, initial: AgentResearchRun[] = []) {
  let items = initial;
  let reads = 0;
  const submissions: unknown[] = [];
  const unexpectedWrites: string[] = [];
  await page.route("**/api/**", async route => {
    if (["GET", "HEAD", "OPTIONS"].includes(route.request().method())) return route.fallback();
    unexpectedWrites.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`);
    await route.fulfill({ status: 409, json: { detail: "Browser test blocks unrelated mutations" } });
  });
  for (const path of ["/research/runs", "/research/agent-comparison-reports"]) {
    await page.route(`**/api${path}?**`, route => route.fulfill({
      json: { items: [], total: 0, limit: 20, offset: 0 },
    }));
  }
  await page.route(`**${endpoint}*`, async route => {
    if (route.request().method() === "POST") {
      submissions.push(route.request().postDataJSON());
      items = [run()];
      return route.fulfill({ json: { ...items[0], deduplicated: false } });
    }
    reads += 1;
    await route.fulfill({ json: { items, total: items.length, limit: 20, offset: 0 } });
  });
  return {
    submissions,
    unexpectedWrites,
    reads: () => reads,
    setRun: (value: AgentResearchRun) => { items = [value]; },
  };
}

test("managed viewer can inspect results but cannot submit or create a shadow run", async ({ page, authenticateAs }) => {
  const fixture = await mockResearch(page, [run("completed")]);
  await authenticateAs("viewer");
  const section = panel(page);
  await expect(section.getByText("Complete", { exact: true })).toBeVisible();
  await expect(section).toContainText(completedResult.rationale);
  await expect(section).toContainText("Requires Researcher access");
  await expect(section.getByLabel("Symbol", { exact: true })).toHaveCount(0);
  await expectResearchOnly(section, false);
  expect(fixture.submissions).toEqual([]);

  // APIRequestContext bypasses page mocks. Never print managed credentials.
  // The extra field is forbidden by the real request schema: if authorization
  // regresses this returns 422, not a provider job in the shared database.
  const headers = { Authorization: `Bearer ${process.env.AUTH_VIEWER_KEY!}` };
  const before = await page.request.get(endpoint, { headers });
  expect(before.status()).toBe(200);
  const beforePage = await before.json();
  const denied = await page.request.post(endpoint, {
    headers, data: { symbol: "MSFT", eligible_for_trading: false },
  });
  expect(denied.status()).toBe(403);
  const after = await page.request.get(endpoint, { headers });
  expect(after.status()).toBe(200);
  const afterPage = await after.json();
  expect(afterPage.total).toBe(beforePage.total);
  expect(afterPage.items.map((item: AgentResearchRun) => item.run_id))
    .toEqual(beforePage.items.map((item: AgentResearchRun) => item.run_id));
  expect(fixture.unexpectedWrites).toEqual([]);
});

test("managed researcher queues an approved symbol and sees the polling lifecycle without execution controls", async ({ page, authenticateAs }) => {
  const fixture = await mockResearch(page);
  await page.clock.install();
  await authenticateAs("researcher");
  const section = panel(page);
  await expect(section.getByLabel("Symbol", { exact: true }).locator("option")).toHaveText(["AAPL", "MSFT", "QQQ", "SPY"]);
  await expectResearchOnly(section, true);
  await section.getByLabel("Symbol", { exact: true }).selectOption("MSFT");
  await section.getByRole("button", { name: "Run research", exact: true }).click();
  const card = section.getByRole("article");
  await expect(card.getByText("Queued", { exact: true })).toBeVisible();
  expect(fixture.submissions).toEqual([{ symbol: "MSFT" }]);
  await expect(card).toContainText("The bounded run is queued");
  await expect(card).not.toContainText("Recommendation:");
  await expectResearchOnly(section, true);

  for (const [status, label] of [["running", "Running"], ["completed", "Complete"]] as const) {
    fixture.setRun(run(status));
    const previousReads = fixture.reads();
    await page.clock.fastForward(15_000);
    await expect.poll(fixture.reads).toBeGreaterThan(previousReads);
    await expect(card.getByText(label, { exact: true })).toBeVisible();
    await expectResearchOnly(section, true);
    await expect(card).toContainText("Not eligible for trading");
    if (status === "running") await expect(card).not.toContainText("Recommendation:");
  }
  await expect(card).toContainText("Recommendation: HOLD");
  await expect(card).toContainText("self-reported confidence 55% (not a calibrated probability)");
  await expect(card).toContainText("Forward evaluation: pending");
  await expect(card).toContainText("Baseline comparison: pending");
  expect(fixture.submissions).toHaveLength(1);
  expect(fixture.unexpectedWrites).toEqual([]);
});

for (const failure of [
  { status: "unavailable", label: "Provider unavailable", error: "LLM provider is unavailable; configure the supported AI integration" },
  { status: "failed", label: "Failed", error: "LLM provider returned malformed structured research output" },
] as const) {
  test(`researcher sees ${failure.status} with no fabricated recommendation, including after reload`, async ({ page, authenticateAs }) => {
    const fixture = await mockResearch(page);
    await page.clock.install();
    await authenticateAs("researcher");
    const section = panel(page);
    await section.getByLabel("Symbol", { exact: true }).selectOption("MSFT");
    await section.getByRole("button", { name: "Run research", exact: true }).click();
    await expect(section.getByText("Queued", { exact: true })).toBeVisible();
    fixture.setRun({ ...run(failure.status), error: failure.error });
    await page.clock.fastForward(15_000);

    async function expectFailure() {
      const card = section.getByRole("article");
      await expect(card.getByText(failure.label, { exact: true })).toBeVisible();
      await expect(card).toContainText(failure.error);
      await expect(card).not.toContainText("Recommendation:");
      await expect(card).not.toContainText("self-reported confidence");
      await expect(card).toContainText("Not eligible for trading");
      await expectResearchOnly(section, true);
    }
    await expectFailure();
    // Local role keys intentionally live only in memory, so a new navigation
    // requires real sign-in again rather than persisting secrets in storage.
    await authenticateAs("researcher");
    await expectFailure();
    expect(fixture.submissions).toEqual([{ symbol: "MSFT" }]);
    expect(fixture.unexpectedWrites).toEqual([]);
  });
}