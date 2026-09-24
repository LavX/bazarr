/**
 * Leaving a single-season series for another title from the search box. The
 * season picker used to send the page back to the series it had just left.
 */
import { expect, test } from "@e2e/fixtures";
import { openDiscover, openTitle } from "@e2e/lib/discover";
import { ensureRecommendedProviders } from "@e2e/lib/providers";

test.describe(
  "Discover title navigation",
  { tag: ["@discover", "@live"] },
  () => {
    // Nothing here searches a provider, but the install restarts the shared
    // instance, so waiting for it keeps this spec off a restarting backend.
    test.beforeEach(async ({ api, bazarr }) => {
      await ensureRecommendedProviders(api, bazarr, test.info());
    });

    test("a single-season series gives way to the next title", async ({
      page,
    }) => {
      await openDiscover(page);
      await openTitle(page, "Chernobyl", "Chernobyl (2019)");
      await expect(
        page.getByRole("article", { name: "Chernobyl details" }),
      ).toBeVisible();
      // Its only season is put in the address, which is what used to fire
      // again on the way out.
      await expect(page).toHaveURL(/[?&]season=1\b/);

      await openTitle(page, "Heat 1995", "Heat (1995)");
      await expect(
        page.getByRole("article", { name: "Heat details" }),
      ).toBeVisible();
      await expect(page).toHaveURL(/[?&]movie=\d+/);
      await expect(
        page.getByRole("article", { name: "Chernobyl details" }),
      ).toHaveCount(0);
    });
  },
);
