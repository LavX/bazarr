/* eslint-disable camelcase -- API bodies keep their transport field names. */
/**
 * Jobs report how they ended: a failure says why in the Jobs drawer and in a
 * notification, and a finished job's time is read in the browser's own zone.
 *
 * Both jobs need no network. A Provider Hub install of a manifest that is not
 * one fails its validation inside the job, and saving a language profile
 * queues a missing-subtitles pass that, with no library, finishes at once.
 */
import { expect, test } from "@e2e/fixtures";
import { liveChannel, waitForJob } from "@e2e/lib/jobs";
import { completeOnboarding } from "@e2e/lib/shared";
import { skipWhatsNew } from "@e2e/lib/whatsNew";
import type { Page } from "@playwright/test";

// Far from UTC, so a timestamp read in the wrong zone is hours off, not seconds.
test.use({ timezoneId: "Asia/Tokyo" });

test.beforeEach(async ({ page, bazarr }) => {
  await skipWhatsNew(page);
  await completeOnboarding(bazarr);
});

/** Opens the app and waits until finished jobs will be announced to it. */
async function openApp(page: Page) {
  const channel = liveChannel(page);
  await page.goto("/");
  await channel;
}

/** The drawer's section for one status, headed by that status. */
function jobsSection(page: Page, title: "Failed" | "Completed") {
  return page
    .getByRole("dialog", { name: "Jobs Manager" })
    .getByRole("heading", { level: 3, name: title, exact: true })
    .locator("xpath=ancestor::div[contains(@class, 'mantine-Stack-root')][1]");
}

test.describe("jobs", { tag: ["@jobs", "@stateful"] }, () => {
  test("a failed job shows its reason in the drawer and a notification", async ({
    page,
    api,
  }) => {
    await openApp(page);

    const queued = await api.post("/api/provider-hub/installations", {
      data: { manifest: { provider_id: "e2e-missing", name: "E2E Missing" } },
    });
    expect(queued.status()).toBe(202);
    const { job_id } = (await queued.json()) as { job_id: number };
    const job = await waitForJob(api, job_id, "failed");
    const reason = job.error?.message ?? "";
    expect(reason).toContain("Could not install E2E Missing");

    const toast = page.getByRole("alert").filter({ hasText: job.job_name });
    await expect(toast).toContainText(reason);

    await page.getByRole("button", { name: "Jobs Manager" }).click();
    const failed = jobsSection(page, "Failed");
    await expect(failed.getByText(job.job_name, { exact: true })).toBeVisible();
    await expect(failed.getByText(reason, { exact: true })).toBeVisible();
  });

  test("a finished job reads as just now, not hours ago", async ({
    page,
    api,
  }) => {
    await openApp(page);

    const profiles = JSON.stringify([
      {
        profileId: 1,
        name: "E2E English",
        cutoff: null,
        items: [
          {
            id: 1,
            language: "en",
            audio_exclude: "False",
            hi: "False",
            forced: "False",
          },
        ],
        mustContain: [],
        mustNotContain: [],
        originalFormat: null,
        tag: null,
      },
    ]);
    const saved = await api.post("/api/system/settings", {
      multipart: { "languages-profiles": profiles },
    });
    expect(saved.ok()).toBe(true);

    const name = "Recalculating missing subtitles";
    await expect
      .poll(async () => {
        const response = await api.get("/api/system/jobs?status=completed");
        const { data } = (await response.json()) as {
          data: { job_name: string }[];
        };
        return data.some((job) => job.job_name === name);
      })
      .toBe(true);

    await page.getByRole("button", { name: "Jobs Manager" }).click();
    const completed = jobsSection(page, "Completed");
    const card = completed
      .getByText(name, { exact: true })
      .locator("xpath=ancestor::div[contains(@class, 'mantine-Card-root')][1]");
    await expect(card).toBeVisible();
    await expect(card.locator("time")).toHaveText(
      /^\d+ (second|seconds|minute|minutes) ago$/,
    );
  });
});
