/**
 * Discover on an instance with no Sonarr, Radarr or Sportarr, which is what
 * the shared instance is. Nothing here changes it.
 */
import { expect, test } from "@e2e/fixtures";
import { openDiscover } from "@e2e/lib/discover";

test.describe("Discover without a library", { tag: ["@discover"] }, () => {
  test("the global catalog is the whole page", async ({ page }) => {
    await openDiscover(page);

    await expect(
      page.getByRole("region", { name: "Beyond your library" }),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "Your library" }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("heading", { name: "Still missing" }),
    ).toHaveCount(0);
  });

  test("the sidebar has no library pages", async ({ page }) => {
    await openDiscover(page);

    const sidebar = page.getByRole("navigation", { name: "Main navigation" });
    await expect(sidebar.getByRole("link", { name: "Discover" })).toBeVisible();
    for (const name of ["Series", "Movies", "Sports"]) {
      // A library page is a link, or a group button when it has children.
      await expect(sidebar.getByText(name, { exact: true })).toHaveCount(0);
    }
  });

  test("a notice offers to connect a library", async ({ page }) => {
    await openDiscover(page);

    const connect = page.getByRole("link", { name: "Connect a library" });
    await expect(connect).toBeVisible();
    await expect(connect).toHaveAttribute("href", "/settings/connections");
    await expect(
      page.getByText("No Sonarr or Radarr instance is connected yet."),
    ).toBeVisible();
  });
});
