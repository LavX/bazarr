/* eslint-disable camelcase */

/**
 * Emby and Silo instances on Settings > Connections and System > Status. Each
 * test starts from the same two instances, made through the API against a
 * server nothing listens on, so no test depends on what an earlier one left.
 */
import { expect, test } from "@e2e/fixtures";
import type { MediaServerInstance } from "@e2e/lib/mediaservers";
import {
  BOGUS_KEY,
  BOGUS_URL,
  closeWhatsNewWhenShown,
  createMediaServer,
  deleteAllMediaServers,
  deleteMediaServer,
  listMediaServers,
  switchOnKinds,
} from "@e2e/lib/mediaservers";
import { completeOnboarding } from "@e2e/lib/shared";

let emby: MediaServerInstance;
let silo: MediaServerInstance;

test.describe(
  "media server connections",
  { tag: ["@stateful", "@mediaservers"] },
  () => {
    test.beforeEach(async ({ api, bazarr, page }) => {
      await completeOnboarding(bazarr);
      await deleteAllMediaServers(api);
      emby = await createMediaServer(api, {
        kind: "emby",
        name: "Living room Emby",
        url: BOGUS_URL,
        api_key: BOGUS_KEY,
        enabled: true,
        path_mappings: [{ local_path: "/tv", remote_path: "/media/tv" }],
      });
      silo = await createMediaServer(api, {
        kind: "silo",
        name: "Basement Silo",
        url: BOGUS_URL,
        api_key: BOGUS_KEY,
        enabled: true,
        path_mappings: [
          {
            local_path: "/movies",
            remote_path: "/media/movies",
            library_id: "1",
          },
        ],
      });
      await switchOnKinds(api, ["emby", "silo"]);
      await closeWhatsNewWhenShown(page);
    });

    test("each instance is listed under its own kind", async ({ page }) => {
      await page.goto("/settings/connections");

      await page.getByRole("tab", { name: "Emby", exact: true }).click();
      await expect(page.getByRole("region", { name: emby.name })).toBeVisible();
      await expect(page.getByRole("region", { name: silo.name })).toHaveCount(
        0,
      );

      await page.getByRole("tab", { name: "Silo", exact: true }).click();
      await expect(page.getByRole("region", { name: silo.name })).toBeVisible();
      await expect(page.getByRole("region", { name: emby.name })).toHaveCount(
        0,
      );
    });

    test("a renamed instance keeps its new name", async ({ page, api }) => {
      await page.goto("/settings/connections");
      await page.getByRole("tab", { name: "Emby", exact: true }).click();
      await page
        .getByRole("region", { name: emby.name })
        .getByRole("button", { name: "Edit" })
        .click();

      const dialog = page.getByRole("dialog", { name: "Edit Emby instance" });
      const name = dialog.getByLabel("Name");
      await name.clear();
      await name.pressSequentially("Bedroom Emby");
      await dialog.getByRole("button", { name: "Save instance" }).click();
      await expect(dialog).toBeHidden();

      await page.reload();
      await page.getByRole("tab", { name: "Emby", exact: true }).click();
      await expect(
        page.getByRole("region", { name: "Bedroom Emby" }),
      ).toBeVisible();
      const saved = await listMediaServers(api);
      expect(saved.find((row) => row.id === emby.id)?.name).toBe(
        "Bedroom Emby",
      );
    });

    test("a deleted instance is gone", async ({ page, api }) => {
      await page.goto("/settings/connections");
      await page.getByRole("tab", { name: "Silo", exact: true }).click();
      await page
        .getByRole("region", { name: silo.name })
        .getByRole("button", { name: "Delete" })
        .click();

      const dialog = page.getByRole("dialog", { name: "Delete instance" });
      await dialog.getByRole("button", { name: "Delete instance" }).click();
      await expect(dialog).toBeHidden();
      await expect(page.getByRole("region", { name: silo.name })).toHaveCount(
        0,
      );

      const left = await listMediaServers(api);
      expect(left.map((row) => row.id)).toEqual([emby.id]);
    });

    test("System > Status shows the remaining instance as unreachable", async ({
      page,
      api,
    }) => {
      await deleteMediaServer(api, silo.id);
      await page.goto("/system/status");

      // The first probe runs in the background and the page asks again every
      // few seconds until it has an answer.
      await expect(page.getByText(/^Emby Version\s*Unreachable$/)).toBeVisible({
        timeout: 20_000,
      });
      await expect(page.getByText("Silo Version")).toHaveCount(0);
    });
  },
);
