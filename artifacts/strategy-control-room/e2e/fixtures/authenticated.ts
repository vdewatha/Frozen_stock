import { expect, test as base, type Page } from "@playwright/test";

export type TrialRole = "viewer" | "operator" | "admin";

const secretNames: Record<TrialRole, string> = {
  viewer: "AUTH_VIEWER_KEY",
  operator: "AUTH_OPERATOR_KEY",
  admin: "AUTH_ADMIN_KEY",
};

async function authenticate(page: Page, role: TrialRole) {
  const secretName = secretNames[role];
  const credential = process.env[secretName];
  if (!credential) throw new Error(`${secretName} must be provided to the test process`);

  await page.goto("/");
  await page.getByLabel("Access key").fill(credential);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByText(role, { exact: true })).toBeVisible();
  await expect(page.getByLabel("Access key")).toHaveCount(0);
}

export const test = base.extend<{ authenticateAs: (role: TrialRole) => Promise<void> }>({
  authenticateAs: async ({ page }, use) => {
    await use((role) => authenticate(page, role));
  },
});

export { expect };