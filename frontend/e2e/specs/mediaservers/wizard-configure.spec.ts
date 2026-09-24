/**
 * The wizard's media server configure steps on a fresh install: two Jellyfins
 * and an Emby, picked on the picker after walking past the arr steps. Every
 * test starts from the wizard's entry point and writes nothing, so the file's
 * container can be shared in order.
 */
import { expect, test } from "@e2e/fixtures";
import { expectFitsViewport, WIZARD_VIEWPORT } from "@e2e/lib/fit";
import {
  BOGUS_KEY,
  BOGUS_URL,
  listMediaServers,
  serverHeading,
  setUpJellyfinTwiceAndEmby,
} from "@e2e/lib/mediaservers";
import { progress, skipStep, stepHeading } from "@e2e/lib/wizard";
import type { Page } from "@playwright/test";

test.use({ viewport: WIZARD_VIEWPORT });

const STEP_POSITIONS = [2, 3, 4];

/** Waits for the configure step at this position and returns its server. */
async function onServerStep(page: Page, position: number): Promise<string> {
  await expect(progress(page)).toHaveText(
    `Connect · Media servers ${position} of 4`,
  );
  await expect(serverHeading(page)).toHaveText(/Jellyfin|Emby/);
  return (await serverHeading(page).innerText()).trim();
}

/** The step's primary action, whatever its label says right now. */
function continueButton(page: Page) {
  return page.getByRole("button", { name: /^(Continue without|Connect) / });
}

test.describe(
  "media server configure steps",
  { tag: ["@stateful", "@mediaservers", "@wizard"] },
  () => {
    test("each step keeps Test beside Continue and fits the window", async ({
      page,
      api,
    }) => {
      await setUpJellyfinTwiceAndEmby(page);

      const seen: string[] = [];
      for (const position of STEP_POSITIONS) {
        const server = await onServerStep(page, position);
        seen.push(server);

        const testButton = page.getByRole("button", {
          name: "Test",
          exact: true,
        });
        const next = continueButton(page);
        const url = await page.getByLabel("Server URL").boundingBox();
        const testBox = await testButton.boundingBox();
        const nextBox = await next.boundingBox();
        expect(url && testBox && nextBox, `${server} controls`).toBeTruthy();
        if (!url || !testBox || !nextBox) return;
        // Same row, Test immediately left of Continue, below the fields.
        expect(
          Math.abs(
            testBox.y + testBox.height / 2 - (nextBox.y + nextBox.height / 2),
          ),
        ).toBeLessThan(2);
        expect(testBox.x + testBox.width).toBeLessThanOrEqual(nextBox.x);
        expect(nextBox.x - (testBox.x + testBox.width)).toBeLessThan(40);
        expect(testBox.y).toBeGreaterThan(url.y + url.height);

        await expectFitsViewport(page, `the ${server} step`);
        await next.click();
      }
      expect(seen.sort()).toEqual(["Emby", "Jellyfin", "Jellyfin 2"]);
      await expect(stepHeading(page, "Seerr")).toBeVisible();
      expect(await listMediaServers(api)).toEqual([]);
    });

    test("a bogus server fails its test and Do this later saves nothing", async ({
      page,
      api,
    }) => {
      await setUpJellyfinTwiceAndEmby(page);

      for (const position of STEP_POSITIONS) {
        const server = await onServerStep(page, position);
        await page.getByLabel("Server URL").pressSequentially(BOGUS_URL);
        await page.getByLabel("API Key").pressSequentially(BOGUS_KEY);
        await page.getByRole("button", { name: "Test", exact: true }).click();
        await expect(
          page.getByText(/^Connection test failed\./),
          `the ${server} test result`,
        ).toBeVisible();
        await expectFitsViewport(
          page,
          `the ${server} step after a failed test`,
        );

        await skipStep(page);
        if (position < 4) {
          await expect(progress(page)).toHaveText(
            `Connect · Media servers ${position + 1} of 4`,
          );
        }
        expect(await listMediaServers(api)).toEqual([]);
      }
      await expect(stepHeading(page, "Seerr")).toBeVisible();
    });

    test("Emby with a failed test is not saved on Continue", async ({
      page,
      api,
    }) => {
      await setUpJellyfinTwiceAndEmby(page);
      await onServerStep(page, 2);
      await skipStep(page);
      expect(await onServerStep(page, 3)).toBe("Emby");

      await page.getByLabel("Server URL").pressSequentially(BOGUS_URL);
      await page.getByLabel("API Key").pressSequentially(BOGUS_KEY);
      await page.getByRole("button", { name: "Test", exact: true }).click();
      await expect(page.getByText(/^Connection test failed\./)).toBeVisible();

      await page.getByRole("button", { name: "Connect Emby" }).click();
      await expect(
        page.getByText("Add at least one path mapping"),
      ).toBeVisible();
      await expect(serverHeading(page)).toHaveText("Emby");
      expect(await listMediaServers(api)).toEqual([]);
    });
  },
);
