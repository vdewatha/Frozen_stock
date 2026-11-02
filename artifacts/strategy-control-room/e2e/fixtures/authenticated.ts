import { expect, test as base, type Page } from "@playwright/test";

export type TrialRole = "viewer" | "researcher" | "operator" | "admin";

const secretNames: Record<TrialRole, string> = {
  viewer: "AUTH_VIEWER_KEY",
  researcher: "AUTH_RESEARCHER_KEY",
  operator: "AUTH_OPERATOR_KEY",
  admin: "AUTH_ADMIN_KEY",
};

async function authenticate(page: Page, role: TrialRole, destination: string) {
  const secretName = secretNames[role];
  const credential = process.env[secretName];
  if (!credential) throw new Error(`${secretName} must be provided to the test process`);

  await page.goto("/system/access");
  await page.getByLabel("Role access key", { exact: true }).fill(credential);
  await page.getByRole("button", { name: "Apply key", exact: true }).click();
  await expect(page.locator(".access-details")).toContainText(role);
  await expect(page.getByLabel("Role access key", { exact: true })).toHaveValue("");
  // Client navigation preserves the intentionally memory-only role credential.
  const root = `/${destination.split("/")[1]}`;
  await page.locator(`nav[aria-label="Main navigation"] a[href="${root}"]`).click();
  if (destination !== root) await page.locator(`.page-tabs a[href="${destination}"]`).click();
}

export const test = base.extend<{ authenticateAs: (role: TrialRole, destination: string) => Promise<void> }>({
  authenticateAs: async ({ page }, use) => {
    await use((role, destination) => authenticate(page, role, destination));
  },
});

export { expect };
