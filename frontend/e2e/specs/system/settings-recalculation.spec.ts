/**
 * A settings save from the page queues the missing-subtitles recalculation:
 * staging a language profile in Settings and applying it with the save
 * control asks for the same pass an API save does, and the finished pass is
 * in the Jobs drawer.
 *
 * Nothing is staged through the API, so the pass the spec sees is the one the
 * save control itself asked for. With no library, the pass finishes at once.
 */
import { expect, test } from "@e2e/fixtures";
import { liveChannel } from "@e2e/lib/jobs";
import { completeOnboarding } from "@e2e/lib/shared";
import { skipWhatsNew } from "@e2e/lib/whatsNew";
import type { Page } from "@playwright/test";

test.beforeEach(async ({ page, bazarr }) => {
  await skipWhatsNew(page);
  await completeOnboarding(bazarr);
});

/** The drawer's section for one status, headed by that status. */
function jobsSection(page: Page, title: "Failed" | "Completed") {
  return page
    .getByRole("dialog", { name: "Jobs Manager" })
    .getByRole("heading", { level: 3, name: title, exact: true })
    .locator("xpath=ancestor::div[contains(@class, 'mantine-Stack-root')][1]");
}

/** The page's save control once it holds this many staged changes. */
function saveControl(page: Page, staged: number) {
  return page.getByRole("button", {
    name: `Save ${staged} pending change${staged !== 1 ? "s" : ""}`,
    exact: true,
  });
}

/** The response of a settings save, once the page asks for it. */
function settingsSave(page: Page) {
  return page.waitForResponse(
    (response) =>
      response.url().includes("/api/system/settings") &&
      response.request().method() === "POST",
  );
}

test.describe("settings saves", { tag: ["@jobs", "@stateful"] }, () => {
  test("a saved language profile queues the missing-subtitles pass", async ({
    page,
    api,
  }) => {
    // The live channel has to be connected before the drawer's card can
    // arrive: the jobs query never goes stale, so the finished pass reaches it
    // through the live channel.
    const channel = liveChannel(page);
    await page.goto("/settings/languages");
    await channel;

    // A profile needs an enabled language, and the fresh install has none.
    // The filter's label is not passed to its combobox, but the section holds
    // no other one.
    const filter = page
      .getByRole("heading", { level: 4, name: "Subtitles Language" })
      .locator("xpath=ancestor::div[contains(@class, 'mantine-Stack-root')][1]")
      .getByRole("combobox");
    await filter.click();
    await filter.fill("English");
    await page.getByRole("option", { name: "English", exact: true }).click();

    const savedLanguage = settingsSave(page);
    await saveControl(page, 1).click();
    expect((await savedLanguage).ok()).toBe(true);

    // The save's reload clears the form, and a profile staged before that
    // lands is thrown away with it. The button below is already enabled by
    // the staged language, so the reload needs its own wait.
    await expect(saveControl(page, 1)).toBeHidden();

    // The button reads as Add New Profile only once the reload brought the
    // enabled language the profile needs.
    const addProfile = page.getByRole("button", { name: "Add New Profile" });
    await expect(addProfile).toBeEnabled();
    await addProfile.click();

    const dialog = page.getByRole("dialog", { name: "Edit Languages Profile" });
    await dialog.getByLabel("Name").fill("E2E UI Recalc");
    await dialog
      .getByRole("button", { name: "Add Language", exact: true })
      .click();
    await dialog.getByRole("button", { name: "Save", exact: true }).click();
    await expect(dialog).toBeHidden();

    const savedProfile = settingsSave(page);
    await saveControl(page, 1).click();
    expect((await savedProfile).ok()).toBe(true);

    const name = "Recalculating missing subtitles";
    await expect
      .poll(
        async () => {
          const response = await api.get("/api/system/jobs?status=completed");
          const { data } = (await response.json()) as {
            data: { job_name: string }[];
          };
          return data.some((job) => job.job_name === name);
        },
        { timeout: 30_000 },
      )
      .toBe(true);

    // The save's reload clears the form again; the row below comes from the
    // server's list once it has. Its pass is in the drawer.
    await expect(saveControl(page, 1)).toBeHidden();
    await expect(
      page.getByRole("cell", { name: "E2E UI Recalc" }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Jobs Manager" }).click();
    const card = jobsSection(page, "Completed")
      .getByText(name, { exact: true })
      .locator("xpath=ancestor::div[contains(@class, 'mantine-Card-root')][1]");
    await expect(card).toBeVisible();
  });
});
