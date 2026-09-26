/**
 * Settings > General > Run first-time setup on an install that finished
 * onboarding. It reopens the wizard at Welcome, wherever a previous run left
 * off.
 */
import { expect, test } from "@e2e/fixtures";
import { completeOnboarding } from "@e2e/lib/shared";
import { skipWhatsNew } from "@e2e/lib/whatsNew";
import { INTENT, stepHeading } from "@e2e/lib/wizard";
import type { APIRequestContext, Page } from "@playwright/test";

async function setupComplete(api: APIRequestContext) {
  const response = await api.get("/api/system/settings");
  expect(response.ok()).toBe(true);
  const settings = (await response.json()) as {
    general: { setup_complete: boolean };
  };
  return settings.general.setup_complete;
}

async function runSetupAgain(page: Page) {
  await page.goto("/settings/general");
  await page.getByRole("button", { name: "Run first-time setup" }).click();
  await expect(page).toHaveURL(/\/setup\/welcome$/);
  await expect(stepHeading(page, "Welcome to Bazarr+")).toBeVisible();
}

test.beforeEach(async ({ page, bazarr }) => {
  await skipWhatsNew(page);
  await completeOnboarding(bazarr);
});

test.describe("run first-time setup", { tag: ["@wizard", "@stateful"] }, () => {
  test("reopens the wizard at Welcome and clears the finished flag", async ({
    page,
    api,
  }) => {
    expect(await setupComplete(api)).toBe(true);
    await runSetupAgain(page);
    expect(await setupComplete(api)).toBe(false);
  });

  test("starts at Welcome even when an earlier run stopped on a later step", async ({
    page,
    bazarr,
  }) => {
    await runSetupAgain(page);
    await page.getByRole("button", { name: "Get started" }).click();
    await page.getByRole("radio", { name: INTENT.library }).click();
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await expect(stepHeading(page, "Sonarr")).toBeVisible();

    // Setup finished somewhere else, say in another tab, and this browser
    // still remembers the Sonarr step it was on.
    await completeOnboarding(bazarr);
    await page.goto("/settings/general");
    await expect(page).toHaveURL(/\/settings\/general$/);

    await runSetupAgain(page);
  });

  test("Leave setup after running it again lands on Discover", async ({
    page,
    api,
  }) => {
    // Leaving navigated before the settings read came back, so the landing
    // page read the old unfinished flag and reopened the wizard.
    await runSetupAgain(page);
    await page.getByRole("button", { name: "Set up later" }).click();
    const dialog = page.getByRole("dialog", { name: "Leave setup?" });
    await dialog.getByRole("button", { name: "Leave setup" }).click();
    await expect(page).toHaveURL(/\/discover$/);
    expect(await setupComplete(api)).toBe(true);
  });
});
