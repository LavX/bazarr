/* eslint-disable camelcase -- API bodies keep their transport field names. */
/**
 * The Sports pages. An instance without Sportarr says so instead of showing
 * an empty library, and an enabled Sportarr instance puts the pages in the
 * sidebar and renders each one, even while its leagues table is empty.
 *
 * Stateful: it switches the product on and creates a Sportarr instance,
 * pointed at a port nothing answers on, so no library is ever reached.
 */
import { expect, test } from "@e2e/fixtures";
import { appPath } from "@e2e/lib/base";
import { completeOnboarding } from "@e2e/lib/shared";
import { skipWhatsNew } from "@e2e/lib/whatsNew";
import type { APIRequestContext } from "@playwright/test";

/** Port 9 is discard: nothing answers there, so the instance stays empty. */
const BOGUS_PORT = 9;

/** One enabled Sportarr instance, as Settings > Connections saves it. */
async function enableSportarr(api: APIRequestContext) {
  // Remove yesterday's instance first, so every test starts from one.
  const listed = await api.get(
    appPath("/api/system/arr-instances?kind=sportarr"),
  );
  expect(listed.ok()).toBe(true);
  for (const instance of (await listed.json()) as { id: number }[]) {
    const removed = await api.delete(
      appPath(`/api/system/arr-instances/${instance.id}`),
    );
    expect(removed.ok()).toBe(true);
  }
  const created = await api.post(appPath("/api/system/arr-instances"), {
    data: {
      kind: "sportarr",
      name: "E2E Sportarr",
      ip: "127.0.0.1",
      port: BOGUS_PORT,
      api_key: "e2e-not-a-real-key",
      enabled: true,
    },
  });
  expect(created.ok()).toBe(true);
  const on = await api.post(appPath("/api/system/settings"), {
    multipart: { "settings-general-use_sportarr": "true" },
  });
  expect(on.ok()).toBe(true);
}

test.describe("Sports without Sportarr", { tag: ["@sports"] }, () => {
  test.beforeEach(async ({ api, page }) => {
    await skipWhatsNew(page);
    // An install that does have Sportarr shows its library instead, and this
    // spec is about the message a missing one gets.
    const response = await api.get(appPath("/api/system/arr-instances"));
    expect(response.ok()).toBe(true);
    const instances = (await response.json()) as {
      kind: string;
      enabled: boolean;
    }[];
    test.skip(
      instances.some(
        (instance) => instance.kind === "sportarr" && instance.enabled,
      ),
      "Skipped: this instance has an enabled Sportarr",
    );
  });

  test("the Sports page says what is missing instead of showing a library", async ({
    page,
  }) => {
    await page.goto(appPath("/sports"));
    await expect(page).toHaveTitle(/Sports - /);
    await expect(
      page.getByText(
        "Enable a Sportarr instance in Connections to view sports.",
      ),
    ).toBeVisible();
  });
});

test.describe(
  "Sports with an enabled Sportarr",
  {
    tag: ["@sports", "@stateful"],
  },
  () => {
    // One container for the whole file: every test of this block runs on
    // the same instance, so enableSportarr removes what an earlier test
    // left before adding its own.
    test.beforeEach(async ({ bazarr, page }) => {
      await skipWhatsNew(page);
      await completeOnboarding(bazarr);
    });

    test("the sidebar gains the Sports pages once Sportarr is enabled", async ({
      api,
      page,
    }) => {
      // The sidebar starts without Sports, so its arrival is the gain.
      // The Sports page renders its empty state only once the router's
      // status queries have settled, so that text is also the proof that
      // the sidebar below it is settled: the absence check then reads a
      // live sidebar, not a page whose queries are still pending.
      await page.goto(appPath("/sports"));
      await expect(
        page.getByText(
          "Enable a Sportarr instance in Connections to view sports.",
        ),
      ).toBeVisible();
      const sidebar = page.getByRole("navigation", { name: "Main navigation" });
      await expect(
        sidebar.getByRole("link", { name: "Sports", exact: true }),
      ).toHaveCount(0);

      await enableSportarr(api);
      await page.reload();
      await expect(
        sidebar.getByRole("link", { name: "Sports", exact: true }).first(),
      ).toBeVisible();
    });

    test("the Sports, Missing, History and Excluded pages render", async ({
      api,
      page,
    }) => {
      await enableSportarr(api);

      await page.goto(appPath("/sports"));
      await expect(page).toHaveTitle(/Sports - /);
      // The page is enabled now, so its own toolbox shows.
      await expect(
        page.getByRole("button", { name: "Sync Leagues" }),
      ).toBeVisible();

      await page.goto(appPath("/wanted/sports"));
      await expect(page).toHaveTitle(/Wanted Sports - /);
      // The wanted page's own toolbox, empty league table or not.
      await expect(
        page.getByRole("button", { name: "Search All" }),
      ).toBeVisible();
      await expect(
        page.getByRole("button", { name: "Scan All" }),
      ).toBeVisible();

      await page.goto(appPath("/history/sports"));
      await expect(page).toHaveTitle(/Sports History - /);
      // The history page's own filters and its embedded-records switch.
      await expect(
        page.getByText("Show Embedded Source records"),
      ).toBeVisible();
      await expect(
        page.getByRole("combobox", { name: "Action" }),
      ).toBeVisible();

      await page.goto(appPath("/blacklist/sports"));
      await expect(page).toHaveTitle(/Sports Excluded - /);
      // The excluded page's own empty state, the leagues table being empty.
      await expect(page.getByText("Nothing Excluded for Sports")).toBeVisible();
    });
  },
);
