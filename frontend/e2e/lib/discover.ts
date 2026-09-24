/**
 * Gets around Discover the way a reader does: open the page, look a title up
 * in the header search box, and open it from the suggestions.
 */
import type { Page } from "@playwright/test";
import { expect } from "@playwright/test";

/** Suggestions come from the metadata service, over the real network. */
const SUGGESTION_TIMEOUT_MS = 20_000;

/**
 * Opens Discover and closes the What's New tour, which a fresh browser is
 * shown once over the first page.
 */
export async function openDiscover(page: Page) {
  await page.goto("/discover");
  const tour = page.getByRole("dialog", { name: "What's New" });
  await expect(tour).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(tour).toBeHidden();
  await expect(page.getByRole("region", { name: "Discover" })).toBeVisible();
}

/** Types into the header search and opens the suggestion called `title`. */
export async function openTitle(page: Page, query: string, title: string) {
  const search = page.getByRole("textbox", { name: "Search" });
  await search.fill(query);
  const suggestion = page.getByRole("button", { name: title, exact: true });
  await expect(suggestion).toBeVisible({ timeout: SUGGESTION_TIMEOUT_MS });
  await suggestion.click();
}

/** Picks the subtitle language on a title page. */
export async function chooseLanguage(page: Page, language: string) {
  const control = page.getByRole("combobox", { name: "Subtitle language" });
  // The picker sits below the fold on a title page, and its dropdown opens
  // where the control is, so bring the control up first.
  await control.scrollIntoViewIfNeeded();
  await control.click();
  const option = page.getByRole("option", { name: language, exact: true });
  await option.scrollIntoViewIfNeeded();
  await option.click();
  await expect(
    page.getByRole("combobox", { name: "Subtitle language" }),
  ).toHaveValue(language);
}

/** The result rows, live while providers are still answering and after. */
export function resultRows(page: Page) {
  return page
    .getByRole("list", { name: "Subtitle results" })
    .getByRole("listitem");
}

/** The progress line while providers are still answering. */
export function stillSearching(page: Page) {
  return page.getByRole("status").filter({ hasText: /[1-9]\d* pending/ });
}
