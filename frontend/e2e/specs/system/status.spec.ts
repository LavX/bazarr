/* eslint-disable camelcase -- API bodies keep their transport field names. */
/**
 * System > Status lists what this install actually runs. An integration that
 * is not set up has no row at all, not an empty or "not configured" one. The
 * first test reads what is configured, so it holds on a fresh container (no
 * rows) and on a real install alike.
 */
import { expect, test } from "@e2e/fixtures";
import { skipWhatsNew } from "@e2e/lib/whatsNew";
import type { APIRequestContext, Page } from "@playwright/test";

const ARR_KINDS = [
  { kind: "sonarr", row: "Sonarr Version" },
  { kind: "radarr", row: "Radarr Version" },
  { kind: "sportarr", row: "Sportarr Version" },
];
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

/**
 * The arr and media server rows the page should show, worked out from what
 * the install has configured: a product switched on, an enabled instance of
 * it, and for an arr the version it reported. A fresh container has none.
 */
async function expectedRows(api: APIRequestContext) {
  const [settings, arrs, servers, status] = await Promise.all(
    [
      "/api/system/settings",
      "/api/system/arr-instances",
      "/api/system/media-server-instances",
      "/api/system/status",
    ].map(async (path) => {
      const response = await api.get(path);
      expect(response.ok(), path).toBe(true);
      return response.json();
    }),
  );
  const general = (settings as { general: Record<string, unknown> }).general;
  const arrInstances = arrs as { kind: string; enabled: boolean }[];
  const versions = (status as { data: Record<string, unknown> }).data;
  const arrRows = ARR_KINDS.filter(
    ({ kind }) =>
      general[`use_${kind}`] === true &&
      arrInstances.some((i) => i.kind === kind && i.enabled) &&
      !!versions[`${kind}_version`],
  ).map(({ row }) => row);
  const mediaServers = (
    servers as {
      data: { kind: string; enabled: boolean; api_key_set: boolean }[];
    }
  ).data.filter(
    (s) => s.enabled && s.api_key_set && general[`use_${s.kind}`] === true,
  ).length;
  return { arrRows, mediaServers };
}

test.describe("system status", { tag: ["@status"] }, () => {
  test("arr and media server rows appear exactly for what is configured", async ({
    page,
    api,
  }) => {
    const expected = await expectedRows(api);
    await openStatus(page, await bazarrVersion(api));

    for (const { row } of ARR_KINDS) {
      await expect(page.getByText(row, { exact: true })).toHaveCount(
        expected.arrRows.includes(row) ? 1 : 0,
      );
    }
    await expect(page.getByText(MEDIA_SERVER_ROW)).toHaveCount(
      expected.mediaServers,
    );
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
      for (const { row } of ARR_KINDS) {
        await expect(page.getByText(row, { exact: true })).toHaveCount(0);
      }
    });
  },
);
