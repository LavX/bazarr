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

    // The translator panel never renders nothing, and which of its states
    // it is in is decided by the same two endpoints the panel queries:
    // both failing is the full outage the panel reports in its own words,
    // only the status one failing is a supported partial outage that keeps
    // its job figures beside the queue-status notice, and with the status
    // answering, its queue tiles render instead.
    //
    // The tabs do not stay mounted, so the panel mounts here and both of
    // its queries start now. The two answers are waited out and are the
    // panel's own, so the branch below follows what the panel actually
    // received, and an answer that has arrived is not yet a render that
    // shows it: the answered branch first waits for the one text only the
    // settled render carries.
    const answered = Promise.all([
      page.waitForResponse((response) =>
        response.url().endsWith("/api/translator/status"),
      ),
      page.waitForResponse((response) =>
        response.url().endsWith("/api/translator/jobs"),
      ),
    ]);
    await page.getByRole("tab", { name: "Translator" }).click();
    const [statusResponse, jobsResponse] = await answered;
    const statusFailed = !statusResponse.ok();
    const jobsFailed = !jobsResponse.ok();

    const unavailable = page.getByText("Translator unavailable");
    const queueStatus = page.getByText("Queue status could not be loaded");
    const queued = page.getByText("Queued", { exact: true });
    if (statusFailed && jobsFailed) {
      // The full outage, in the panel's own words, and nothing else.
      await expect(unavailable).toBeVisible();
      await expect(queued).toHaveCount(0);
    } else if (statusFailed) {
      // The partial outage: its notice beside the job figures it kept.
      await expect(queueStatus).toBeVisible();
      await expect(page.getByText("Cost, recent jobs")).toBeVisible();
      await expect(
        page.getByText("Lines delivered", { exact: true }),
      ).toBeVisible();
    } else {
      // The status answered, so the queue tiles render. The settled
      // render is told apart from the loading one by the processing
      // tile's hint first: it names the queue's own limit, where the
      // loading render reads a dash in its place.
      const status = (await statusResponse.json()) as {
        queue?: { maxConcurrent?: unknown };
      };
      expect(typeof status.queue?.maxConcurrent).toBe("number");
      const maxConcurrent = status.queue?.maxConcurrent as number;
      await expect(
        page.getByText(`max ${maxConcurrent} concurrent`),
      ).toBeVisible();
      await expect(queued).toBeVisible();
      await expect(unavailable).toHaveCount(0);
    }
  });
});
