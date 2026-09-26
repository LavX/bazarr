/**
 * Downloading a Discover result. The download runs as a job, and the file is
 * handed to the browser as soon as the job finishes, with no second click.
 * Stateful: the job and the enabled providers stay on the instance.
 */
import { expect, test } from "@e2e/fixtures";
import {
  chooseLanguage,
  openDiscover,
  openTitle,
  resultRows,
} from "@e2e/lib/discover";
import { ensureRecommendedProviders } from "@e2e/lib/providers";
import { completeOnboarding } from "@e2e/lib/shared";
import { readFile } from "node:fs/promises";

/** A row can be downloaded as soon as it arrives, before the search ends. */
const FIRST_ROWS_TIMEOUT_MS = 30_000;
/** The provider fetch runs in the job; it has taken up to 20 seconds. */
const SAVE_TIMEOUT_MS = 45_000;

test.describe(
  "Discover download",
  { tag: ["@stateful", "@discover", "@live"] },
  () => {
    test.beforeEach(async ({ api, bazarr }) => {
      await completeOnboarding(bazarr);
      await ensureRecommendedProviders(api, bazarr, test.info());
    });

    test("a download saves itself and is listed as a finished job", async ({
      page,
    }) => {
      // Rows, the provider fetch and the save follow each other in one flow.
      test.setTimeout(test.info().timeout + 60_000);

      await openDiscover(page);
      await openTitle(page, "Heat 1995", "Heat (1995)");
      await chooseLanguage(page, "English");
      await page.getByRole("button", { name: "Find subtitles" }).click();

      const row = resultRows(page).first();
      const download = row.getByRole("button", { name: "Download SRT" });
      await expect(download).toBeEnabled({ timeout: FIRST_ROWS_TIMEOUT_MS });

      const saved = page.waitForEvent("download", {
        timeout: SAVE_TIMEOUT_MS,
      });
      await download.click();
      await expect(
        page.getByRole("button", { name: "Jobs Manager" }),
      ).toContainText("1 running");

      const file = await saved;
      const name = file.suggestedFilename();
      expect(name).toMatch(/\.srt$/);
      const text = await readFile(await file.path(), "utf8");
      expect(text).toMatch(/\d\d:\d\d:\d\d,\d{3} --> \d\d:\d\d:\d\d,\d{3}/);

      await expect(row.getByText("Saved", { exact: true })).toBeVisible();
      await expect(
        page.getByRole("status").filter({ hasText: "Saved to your device" }),
      ).toBeVisible();
      await expect(page.getByRole("button", { name: "Save SRT" })).toHaveCount(
        0,
      );

      await page.getByRole("button", { name: "Jobs Manager" }).click();
      const drawer = page.getByRole("dialog", { name: "Jobs Manager" });
      await expect(
        drawer.getByRole("heading", { name: "Completed" }),
      ).toBeVisible();
      await expect(drawer.getByText(name)).toBeVisible();
    });
  },
);
