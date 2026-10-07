/**
 * Statistics. The tabs follow the instance: the Distribution tab exists only
 * while the endpoint is serving, and every other panel shows its own content
 * whether the history behind it is empty or not. This spec only reads, so it
 * is safe against a real install.
 */
import { expect, test } from "@e2e/fixtures";
import { skipWhatsNew } from "@e2e/lib/whatsNew";

test.beforeEach(async ({ page }) => {
  await skipWhatsNew(page);
});

test.describe("statistics", { tag: ["@status"] }, () => {
  test("the tabs follow the instance, and every panel shows its content", async ({
    page,
    api,
  }) => {
    // The Distribution tab appears only while the endpoint is serving, so
    // the page and this spec read the same setting. The endpoint answers
    // with the flag at the top of its body; anything else is a contract
    // change this spec must hear about, not silently count zero tabs.
    const response = await api.get("/api/distribution-hub/settings");
    expect(response.ok()).toBe(true);
    const settings = (await response.json()) as { enabled?: unknown };
    expect(typeof settings.enabled).toBe("boolean");
    const distribution = settings.enabled as boolean;

    await page.goto("/system/statistics");
    await expect(page).toHaveTitle(/Statistics - .+ \(System\)/);

    const tabs = page.getByRole("tab");
    await expect(tabs.filter({ hasText: "Activity" })).toBeVisible();
    await expect(tabs.filter({ hasText: "Providers" })).toBeVisible();
    await expect(tabs.filter({ hasText: "Quality" })).toBeVisible();
    await expect(tabs.filter({ hasText: "Translator" })).toBeVisible();
    await expect(tabs.filter({ hasText: "Distribution" })).toHaveCount(
      distribution ? 1 : 0,
    );

    // The activity tiles render with or without a history behind them.
    for (const tile of [
      "Downloads",
      "Per day",
      "Busiest day",
      "Arrived automatically",
    ]) {
      await expect(page.getByText(tile, { exact: true }).first()).toBeVisible();
    }
    await expect(page.getByText("Subtitles downloaded per day")).toBeVisible();

    await page.getByRole("tab", { name: "Providers" }).click();
    await expect(page.getByText("Downloads by provider")).toBeVisible();
    await expect(page.getByText("Match quality by provider")).toBeVisible();
    await expect(page.getByText("Blacklist rate")).toBeVisible();

    await page.getByRole("tab", { name: "Quality" }).click();
    await expect(page.getByText("Match quality distribution")).toBeVisible();
    await expect(page.getByText("Languages downloaded")).toBeVisible();
    await expect(page.getByText("How subtitles arrived")).toBeVisible();

    // The translator panel either reports the sidecar's figures or says it
    // cannot reach it; it never renders nothing.
    await page.getByRole("tab", { name: "Translator" }).click();
    await expect(
      page
        .getByText("Translator unavailable")
        .or(page.getByText("Queued", { exact: true })),
    ).toBeVisible();
  });
});
