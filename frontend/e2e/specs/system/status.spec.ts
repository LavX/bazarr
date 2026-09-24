/* eslint-disable camelcase -- API bodies keep their transport field names. */
/**
 * System > Status lists what this install actually runs. An integration that
 * is not set up has no row at all, not an empty or "not configured" one.
 */
import { expect, test } from "@e2e/fixtures";
import { skipWhatsNew } from "@e2e/lib/whatsNew";
import type { APIRequestContext, Page } from "@playwright/test";

const ARR_ROWS = ["Sonarr Version", "Radarr Version", "Sportarr Version"];
const MEDIA_SERVER_ROW = /^(Plex|Jellyfin|Emby|Silo) Version/;

async function openStatus(page: Page, bazarrVersion: string) {
  await page.goto("/system/status");
  await expect(page.getByText("Bazarr Version", { exact: true })).toBeVisible();
  // The rows below it render from the same answer as the version, so once the
  // version is on the page the rest of the block is too.
  // The version shares its cell with the What's new link.
  await expect(page.getByText(bazarrVersion).first()).toBeVisible();
}

async function bazarrVersion(api: APIRequestContext) {
  const response = await api.get("/api/system/status");
  expect(response.ok()).toBe(true);
  const { data } = (await response.json()) as {
    data: { bazarr_version: string };
  };
  return data.bazarr_version;
}

test.beforeEach(async ({ page }) => {
  await skipWhatsNew(page);
});

test.describe("system status", { tag: ["@status"] }, () => {
  test("an install without arr or media servers shows only Bazarr+ itself", async ({
    page,
    api,
  }) => {
    await openStatus(page, await bazarrVersion(api));

    for (const row of ARR_ROWS) {
      await expect(page.getByText(row, { exact: true })).toHaveCount(0);
    }
    await expect(page.getByText(MEDIA_SERVER_ROW)).toHaveCount(0);
    await expect(page.getByText(/not configured/i)).toHaveCount(0);
  });
});

test.describe(
  "system status with a media server",
  {
    tag: ["@status", "@stateful"],
  },
  () => {
    test("an unreachable Jellyfin gets one row that says so", async ({
      page,
      api,
    }) => {
      // The product switch and one enabled instance, as Settings saves them.
      const toggle = await api.post("/api/system/settings", {
        multipart: { "settings-general-use_jellyfin": "true" },
      });
      expect(toggle.ok()).toBe(true);
      const created = await api.post("/api/system/media-server-instances", {
        data: {
          kind: "jellyfin",
          name: "Jellyfin",
          // Port 9 is discard: nothing answers there, so the probe fails fast.
          url: "http://127.0.0.1:9",
          api_key: "e2e-not-a-real-key",
          enabled: true,
        },
      });
      expect(created.status()).toBe(201);

      await openStatus(page, await bazarrVersion(api));

      const rows = page.getByText(MEDIA_SERVER_ROW);
      await expect(rows).toHaveCount(1);
      await expect(rows).toHaveText("Jellyfin Version");
      // The page keeps asking while the first probe runs.
      await expect(page.getByText("Unreachable", { exact: true })).toBeVisible({
        timeout: 20_000,
      });
      for (const row of ARR_ROWS) {
        await expect(page.getByText(row, { exact: true })).toHaveCount(0);
      }
    });
  },
);
