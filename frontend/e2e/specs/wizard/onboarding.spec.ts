/**
 * The first-run wizard on a fresh install. Every test here starts from the
 * wizard's own entry point, so the file's container can be shared in order:
 * nothing a test leaves behind changes where the next one starts.
 */
import { expect, test } from "@e2e/fixtures";
import { expectFitsViewport, WIZARD_VIEWPORT } from "@e2e/lib/fit";
import {
  chooseIntent,
  INTENT,
  openWelcome,
  progress,
  skipStep,
  stepHeading,
} from "@e2e/lib/wizard";

test.use({ viewport: WIZARD_VIEWPORT });

test.describe("onboarding wizard", { tag: ["@stateful", "@wizard"] }, () => {
  test("the intent step offers both paths and fits the screen", async ({
    page,
  }) => {
    await openWelcome(page);
    await page.getByRole("button", { name: "Get started" }).click();

    await expect(
      stepHeading(page, "What do you want Bazarr+ to do for you?"),
    ).toBeVisible();
    await expect(
      page.getByRole("radio", { name: INTENT.library }),
    ).toBeVisible();
    await expect(
      page.getByRole("radio", { name: INTENT.discover }),
    ).toBeVisible();
    await expectFitsViewport(page, "the intent step");
  });

  test("an untouched Sonarr step writes nothing", async ({ page, api }) => {
    await chooseIntent(page, "library");
    await expect(stepHeading(page, "Sonarr")).toBeVisible();

    await page.getByRole("button", { name: "Continue without Sonarr" }).click();
    await expect(stepHeading(page, "Radarr")).toBeVisible();

    const response = await api.get("/api/system/arr-instances");
    expect(response.status()).toBe(200);
    expect(await response.json()).toEqual([]);
  });

  test("several media servers can be picked and each gets its own step", async ({
    page,
  }) => {
    await chooseIntent(page, "discover");
    await expect(stepHeading(page, "Media servers")).toBeVisible();

    await page.getByRole("checkbox", { name: "Jellyfin", exact: true }).click();
    await page.getByRole("checkbox", { name: "Emby", exact: true }).click();
    await page.getByRole("button", { name: "Add another Jellyfin" }).click();
    await expectFitsViewport(page, "the media server picker");

    await page.getByRole("button", { name: "Set up 3 servers" }).click();

    const configured: string[] = [];
    for (const position of [2, 3, 4]) {
      await expect(progress(page)).toHaveText(
        `Connect · Media servers ${position} of 4`,
      );
      const heading = page.getByRole("heading", { level: 3 });
      await expect(heading).toHaveText(/Jellyfin|Emby/);
      configured.push((await heading.innerText()).trim());
      await expectFitsViewport(page, `media server step ${position}`);
      await skipStep(page);
    }
    await expect(stepHeading(page, "Seerr")).toBeVisible();
    expect(configured.sort()).toEqual(["Emby", "Jellyfin", "Jellyfin 2"]);
  });

  test("Set up later asks first and Run first-time setup returns to Welcome", async ({
    page,
  }) => {
    await chooseIntent(page, "library");
    await expect(stepHeading(page, "Sonarr")).toBeVisible();

    await page.getByRole("button", { name: "Set up later" }).click();
    const dialog = page.getByRole("dialog", { name: "Leave setup?" });
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: "Leave setup" }).click();
    await expect(page).toHaveURL(/\/discover$/);

    await page.goto("/settings/general");
    await page.getByRole("button", { name: "Run first-time setup" }).click();
    await expect(page).toHaveURL(/\/setup\/welcome$/);
    await expect(stepHeading(page, "Welcome to Bazarr+")).toBeVisible();
  });

  test(
    "the providers install step fits with the real catalog",
    { tag: "@live" },
    async ({ page }) => {
      // Installing from the catalog, the restart and the resume take minutes.
      test.setTimeout(test.info().timeout + 300_000);

      await chooseIntent(page, "discover");
      await page
        .getByRole("button", { name: "Continue without a server" })
        .click();
      await expect(stepHeading(page, "Seerr")).toBeVisible();
      await skipStep(page);

      await expect(stepHeading(page, "Subtitle languages")).toBeVisible();
      // The step preselects the browser's language, English under the en-US
      // locale the config pins, so Continue is ready without touching it.
      const next = page.getByRole("button", { name: "Continue", exact: true });
      await expect(next).toBeEnabled();
      await next.click();

      await expect(stepHeading(page, "Add subtitle providers")).toBeVisible();
      const recommended = page.getByRole("button", {
        name: /^Install \d+ recommended providers?$/,
      });
      await expect(recommended).toBeVisible({ timeout: 30_000 });
      await expectFitsViewport(page, "the install stage before installing");

      await recommended.click();
      await expect(
        page.getByText(/^Installing provider \d+ of \d+/),
      ).toBeVisible();
      await expectFitsViewport(page, "the install stage while installing");

      // The run ends in a restart and the wizard reloads onto the configure
      // stage by itself once Bazarr+ is back.
      const configure = stepHeading(page, "Enable and configure providers");
      await expect(configure).toBeVisible({ timeout: 300_000 });
      await expectFitsViewport(page, "the configure stage after the restart");

      // Ticking a provider can reveal its own options, so the list is re-read
      // after every click, within a bound in case one refuses to tick.
      const unticked = page.getByRole("checkbox", { checked: false });
      const limit = (await unticked.count()) * 2;
      for (let clicks = 0; clicks < limit && (await unticked.count()) > 0; ) {
        await unticked.first().click();
        clicks += 1;
      }
      await expect(unticked).toHaveCount(0);
      await expectFitsViewport(
        page,
        "the configure stage with everything enabled",
      );
    },
  );
});
