/**
 * The subtitle tools menu on a real media file, through the movie's Mass
 * Edit modal: the modal lists the movie's subtitle files, and the tools menu
 * it opens on one of them offers every tool group.
 *
 * Needs a library: the spec skips on an instance without an enabled arr or
 * without a movie carrying a subtitle file, which is what a fresh container
 * is. It reads only, so it runs against a real install too, which is the
 * only run of the suite that has a library today.
 */
import { expect, test } from "@e2e/fixtures";
import { appPath } from "@e2e/lib/base";
import { enabledArrKinds, movieWithSubtitle } from "@e2e/lib/library";
import { skipWhatsNew } from "@e2e/lib/whatsNew";
import { basename } from "node:path";

test.beforeEach(async ({ page }) => {
  await skipWhatsNew(page);
});

test.describe("subtitle tools", { tag: ["@library"] }, () => {
  test("the tools menu on a movie's subtitle lists every tool group", async ({
    page,
    api,
  }) => {
    const kinds = await enabledArrKinds(api);
    if (kinds.length === 0) {
      test.skip(
        true,
        "Skipped: this instance has no enabled Sonarr, Radarr or Sportarr, so there is no media file to open",
      );
      return;
    }
    const movie = await movieWithSubtitle(api);
    if (movie === null) {
      test.skip(
        true,
        "Skipped: this instance has no movie with a subtitle file on disk",
      );
      return;
    }
    const file = movie.subtitles.find(
      (subtitle) => typeof subtitle.path === "string" && subtitle.path !== "",
    ) as { path: string };

    await page.goto(appPath(`/movies/${movie.id}`));
    await expect(page.getByText(movie.title).first()).toBeVisible();

    // The subtitle tools modal, over every subtitle the movie has.
    await page.getByRole("button", { name: "More Actions" }).click();
    await page.getByRole("menuitem", { name: "Mass Edit" }).click();
    const dialog = page.getByRole("dialog", { name: "Subtitle Tools" });
    await expect(dialog).toBeVisible();
    // A real subtitle row: the file the tools would act on is listed.
    const row = dialog.getByRole("row").filter({
      hasText: basename(file.path),
    });
    await expect(row).toBeVisible();

    // That row's own checkbox, not the table's select-all one, is what
    // enables the tools on this one file.
    await row.getByRole("checkbox").check();
    const action = dialog.getByRole("button", { name: "Select Action" });
    await expect(action).toBeEnabled();
    await action.click();

    // Every group the menu carries, on the file that is really there.
    for (const group of [
      "Sync & Timing",
      "Cleanup",
      "Style & Language",
      "Actions",
    ]) {
      await expect(
        page.getByRole("menu").getByText(group, { exact: true }),
      ).toBeVisible();
    }
    await expect(
      page.getByRole("menuitem", { name: "Sync...", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("menuitem", { name: "Translate...", exact: true }),
    ).toBeVisible();
    // Nothing was clicked: the menu closes and the modal is still there.
    await page.keyboard.press("Escape");
    await expect(dialog).toBeVisible();
  });
});
