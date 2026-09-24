/**
 * A subtitle search for a film that is not in any library, against the real
 * providers the wizard recommends. Reads only: the search leaves nothing on
 * the instance that another spec could trip over.
 */
import { expect, test } from "@e2e/fixtures";
import {
  chooseLanguage,
  openDiscover,
  openTitle,
  resultRows,
  stillSearching,
} from "@e2e/lib/discover";
import { ensureRecommendedProviders } from "@e2e/lib/providers";

/** The first rows need the fastest providers to answer, well inside the wall. */
const FIRST_ROWS_TIMEOUT_MS = 30_000;
/** The search answers once its wall has passed, 20 seconds by default. */
const SEARCH_TIMEOUT_MS = 45_000;

interface ProviderOutcome {
  provider: string;
  status: string;
  elapsed_ms: number;
}

interface SearchSnapshot {
  coverage: { providers: ProviderOutcome[] };
}

/** The summary line ProviderCoverage writes, built from the same outcomes. */
function expectedSummary(providers: ProviderOutcome[]): string {
  const count = (statuses: string[]) =>
    providers.filter((p) => statuses.includes(p.status)).length;
  const searched = count(["success", "empty"]);
  const unverified = count(["unverified"]);
  const skipped = count(["skipped"]);
  const outOfTime = count(["not_started", "abandoned"]);
  const unavailable =
    providers.length - searched - unverified - skipped - outOfTime;
  return [
    `Search details · ${searched} searched`,
    unverified ? ` · ${unverified} unverified` : "",
    skipped ? ` · ${skipped} skipped` : "",
    outOfTime ? ` · ${outOfTime} out of time` : "",
    unavailable ? ` · ${unavailable} unavailable` : "",
  ].join("");
}

test.describe("Discover search", { tag: ["@discover", "@live"] }, () => {
  test.beforeEach(async ({ api, bazarr }) => {
    await ensureRecommendedProviders(api, bazarr, test.info());
  });

  test("a film found by name opens its title page", async ({ page }) => {
    await openDiscover(page);
    await openTitle(page, "Heat 1995", "Heat (1995)");

    const details = page.getByRole("article", { name: "Heat details" });
    await expect(details).toBeVisible();
    await expect(details.getByText("1995 · Film")).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "Find the subtitles you need" }),
    ).toBeVisible();
  });

  // One search, watched from start to end: a second one for the same film
  // would be answered from the cache, with no live phase to watch.
  test("rows arrive early and the finished search accounts for every provider", async ({
    page,
    api,
  }) => {
    const settings = await (await api.get("/api/system/settings")).json();
    const wallMs =
      Number(settings.compat_endpoint.search_timeout_seconds) * 1000;

    await openDiscover(page);
    await openTitle(page, "Heat 1995", "Heat (1995)");
    await chooseLanguage(page, "English");
    const answered = page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/discover/search") &&
        response.request().method() === "POST",
      { timeout: SEARCH_TIMEOUT_MS },
    );
    await page.getByRole("button", { name: "Find subtitles" }).click();

    // One query, so the row and the pending count are seen on the same page.
    const live = page
      .getByRole("region", { name: "Discover" })
      .filter({ has: resultRows(page).first() })
      .filter({ has: stillSearching(page) });
    await expect(live).toBeVisible({ timeout: FIRST_ROWS_TIMEOUT_MS });

    const response = await answered;
    expect(response.ok()).toBe(true);
    const snapshot = (await response.json()) as SearchSnapshot;
    const providers = snapshot.coverage.providers;

    await expect(
      page.getByRole("heading", { name: "Subtitle results" }),
    ).toBeVisible();
    await expect(resultRows(page).first()).toBeVisible();
    await expect(page.locator("#discover-coverage")).toHaveText(
      expectedSummary(providers),
    );

    // Out of time is Discover's own wall, so a provider reported that way
    // has to have been running when the wall passed. One that never started
    // ran for no time at all rather than being called slow.
    for (const outcome of providers) {
      if (outcome.status === "abandoned") {
        expect(outcome.elapsed_ms, outcome.provider).toBeGreaterThanOrEqual(
          wallMs,
        );
      }
      if (outcome.status === "not_started") {
        expect(outcome.elapsed_ms, outcome.provider).toBe(0);
      }
    }
  });
});
