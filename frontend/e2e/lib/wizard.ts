/**
 * Moves through the onboarding wizard the way a reader does, one control at a
 * time. Each helper ends once the next screen is on the page.
 */
import type { Page } from "@playwright/test";
import { expect } from "@playwright/test";

export const INTENT = {
  library: "I run Sonarr, Radarr or Sportarr",
  discover: "I want to find subtitles for anything",
} as const;

/** The step heading, the one level-2 or level-3 title on the card. */
export function stepHeading(page: Page, name: string | RegExp) {
  return page.getByRole("heading", { name, exact: typeof name === "string" });
}

/** The "Getting started · 2 of 2" line under the brand in the header. */
export function progress(page: Page) {
  return page.locator("header").getByText(/ · /);
}

export async function openWelcome(page: Page) {
  await page.goto("/setup");
  await expect(stepHeading(page, "Welcome to Bazarr+")).toBeVisible();
}

export async function chooseIntent(page: Page, intent: keyof typeof INTENT) {
  await openWelcome(page);
  await page.getByRole("button", { name: "Get started" }).click();
  await page.getByRole("radio", { name: INTENT[intent] }).click();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
}

/** The shell's skip control under the card, whatever the step calls it. */
export async function skipStep(page: Page, label = "Do this later") {
  await page.getByRole("button", { name: label, exact: true }).click();
}
