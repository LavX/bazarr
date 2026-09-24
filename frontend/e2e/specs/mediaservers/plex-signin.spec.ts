/**
 * Plex has no form to fill in: its configure step signs in to a Plex account.
 * Pressing Connect to Plex either starts that sign-in, in a window of its own,
 * or says plainly that Plex could not be reached. Either way nothing breaks.
 */
import { expect, test } from "@e2e/fixtures";
import { WIZARD_VIEWPORT } from "@e2e/lib/fit";
import { chooseIntent, stepHeading } from "@e2e/lib/wizard";
import type { Page } from "@playwright/test";

test.use({ viewport: WIZARD_VIEWPORT });

test.describe("Plex sign-in", { tag: ["@mediaservers"] }, () => {
  test("Connect to Plex starts sign-in or says Plex is unreachable", async ({
    page,
  }) => {
    const errors: string[] = [];
    page.on("console", (message) => {
      if (message.type() === "error") errors.push(message.text());
    });
    page.on("pageerror", (error) => errors.push(error.message));
    const popups: Page[] = [];
    page.on("popup", (popup) => popups.push(popup));

    await chooseIntent(page, "discover");
    await expect(stepHeading(page, "Media servers")).toBeVisible();
    await page.getByRole("checkbox", { name: "Plex", exact: true }).click();
    await page.getByRole("button", { name: "Set up 1 server" }).click();
    await expect(page.getByRole("heading", { level: 3 })).toHaveText("Plex");

    await page.getByRole("button", { name: "Connect to Plex" }).click();

    const signingIn = page.getByText("Complete Authentication");
    const unreachable = page
      .getByRole("alert")
      .filter({ hasText: /Could not reach Plex|Could not start Plex sign-in/ });
    // The PIN request goes out to plex.tv, so it gets longer than a local call.
    await expect(signingIn.or(unreachable)).toBeVisible({ timeout: 15_000 });

    if (await signingIn.isVisible()) {
      await expect.poll(() => popups.length).toBe(1);
      expect(popups[0].url()).toContain("plex.tv");
      await page.getByRole("button", { name: "Cancel" }).click();
      await expect(
        page.getByRole("button", { name: "Connect to Plex" }),
      ).toBeVisible();
    }
    for (const popup of popups) await popup.close();

    expect(errors).toEqual([]);
  });
});
