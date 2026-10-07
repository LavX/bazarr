/* eslint-disable camelcase -- API bodies keep their transport field names. */
/**
 * Jobs report how they ended: a failure says why in the Jobs drawer and in a
 * notification, and a finished job's time is read in the browser's own zone.
 *
 * Both jobs need no network. A raw manifest the catalog does not hold is
 * refused at the route before any job exists, so the failed job comes from a
 * local package whose zip holds no manifest and fails its validation inside
 * the job, and saving a language profile queues a missing-subtitles pass that,
 * with no library, finishes at once.
 */
import { expect, test } from "@e2e/fixtures";
import { liveChannel, waitForJob } from "@e2e/lib/jobs";
import { completeOnboarding } from "@e2e/lib/shared";
import { skipWhatsNew } from "@e2e/lib/whatsNew";
import type { Page } from "@playwright/test";

/** A valid .zip with no entries: the local install route takes it, and the
 * job fails inside on the manifest the package does not contain. */
function emptyZip() {
  // The end-of-central-directory record alone, with zero entries.
  return Buffer.from("PK\x05\x06" + "\x00".repeat(18));
}

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

    const refused = await api.post("/api/provider-hub/installations", {
      data: { manifest: { provider_id: "e2e-missing", name: "E2E Missing" } },
    });
    expect(refused.status()).toBe(400);
    expect(await refused.json()).toBe(
      "the manifest matches no catalog entry; install it by source, provider_id and version",
    );

    // The drawer is open before the job fails: the failure's notification sits
    // over the bottom controls, where the Jobs Manager button lives.
    await page.getByRole("button", { name: "Jobs Manager" }).click();

    const queued = await api.post("/api/provider-hub/installations/local", {
      multipart: {
        file: {
          name: "e2e-broken.zip",
          mimeType: "application/zip",
          buffer: emptyZip(),
        },
      },
    });
    expect(queued.status()).toBe(202);
    const { job_id } = (await queued.json()) as { job_id: number };
    const job = await waitForJob(api, job_id, "failed");
    const reason = job.error?.message ?? "";
    expect(reason).toContain(
      "Could not install e2e-broken.zip: package must contain a provider.json manifest",
    );

    const toast = page.getByRole("alert").filter({ hasText: job.job_name });
    await expect(toast).toContainText(reason);

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
