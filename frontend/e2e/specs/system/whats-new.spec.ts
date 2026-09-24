/**
 * The release tour a browser gets once. The shared instance is onboarded, so
 * a fresh browser context is exactly a reader arriving after the upgrade.
 */
import { expect, test } from "@e2e/fixtures";
import { WHATS_NEW_VERSION } from "@e2e/lib/whatsNew";

test.describe("What's New", { tag: ["@status"] }, () => {
  test("opens once on the first load, shows every slide and stays closed", async ({
    page,
  }) => {
    await page.goto("/");
    const modal = page.getByRole("dialog", { name: "What's New" });
    await expect(modal).toBeVisible();
    await expect(
      modal.getByText(`What's new in Bazarr+ ${WHATS_NEW_VERSION}`),
    ).toBeVisible();

    const dots = modal.getByRole("button", { name: /^Go to update \d+$/ });
    const slides = await dots.count();
    expect(slides).toBeGreaterThan(0);

    for (let slide = 1; slide <= slides; slide += 1) {
      await expect(
        modal.getByRole("button", {
          name: `Go to update ${slide}`,
          exact: true,
        }),
      ).toHaveAttribute("aria-current", "true");
      await expect(modal.getByRole("heading", { level: 4 })).not.toBeEmpty();
      // The body is the paragraph right after the slide's title.
      const body = modal.locator("h4 + p");
      await expect(body).toHaveText(/\w{3,}.*\w{3,}/);
      if (slide < slides) {
        await modal.getByRole("button", { name: "Next" }).click();
      }
    }

    await modal.getByRole("button", { name: "Got it" }).click();
    await expect(modal).toBeHidden();

    await page.reload();
    await expect(
      page.getByRole("button", { name: "Jobs Manager" }),
    ).toBeVisible();
    await expect(modal).toBeHidden();
  });
});
