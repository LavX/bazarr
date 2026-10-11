/**
 * A translation job with the translator unreachable. The job must fail, and
 * fail with a reason a user can act on: the queue records the failure with a
 * structured reason, the job's name says what died, and the page announces
 * the same failure.
 *
 * Fixme, not skipped: no run of this suite has a library yet. The harness
 * starts empty containers, the read-only set that runs against a real
 * install excludes stateful specs, and the suite has no way to seed a
 * library, so this spec has no instance it can run against. It turns on
 * when the suite grows a seeded-library run; the body below is that run's
 * test.
 */
/* eslint-disable camelcase -- API bodies keep their transport field names. */
import { expect, test } from "@e2e/fixtures";
import { appPath } from "@e2e/lib/base";
import { liveChannel } from "@e2e/lib/jobs";
import { enabledArrKinds, movieWithSubtitle } from "@e2e/lib/library";
import { completeOnboarding } from "@e2e/lib/shared";
import { skipWhatsNew } from "@e2e/lib/whatsNew";
import type { APIRequestContext, Page } from "@playwright/test";

/** The job validates the file, then fails at the unreachable translator. */
const JOB_CAP_MS = 60_000;
/** Port 9 is discard: nothing answers there, so the probe fails fast. */
const UNREACHABLE_URL = "http://127.0.0.1:9";
/** The connection failure the backend puts in words a user can act on. */
const CONNECTION_FAILURE =
  "Cannot connect to the AI Subtitle Translator service.";

interface Job {
  job_id: number;
  job_name: string;
  status: string;
  error: { reason: string; message: string } | null;
}

async function failedJobs(api: APIRequestContext): Promise<Job[]> {
  const response = await api.get(appPath("/api/system/jobs?status=failed"));
  expect(response.ok()).toBe(true);
  const { data } = (await response.json()) as { data: Job[] };
  return data ?? [];
}

/** Opens the app and waits until finished jobs will be announced to it. */
async function openApp(page: Page) {
  const channel = liveChannel(page);
  await page.goto(appPath("/"));
  await channel;
}

test.describe(
  "translation with the translator unreachable",
  {
    tag: ["@library", "@stateful"],
  },
  () => {
    test.fixme(
      true,
      "No run of this suite has a library yet: the harness starts empty containers, the read-only set that runs against a real install excludes stateful specs, and the suite has no way to seed a library. Turn this on when the suite grows a seeded-library run.",
    );

    test.beforeEach(async ({ api, bazarr, page }) => {
      await skipWhatsNew(page);
      await completeOnboarding(bazarr);
      // A translator that cannot be reached, so the job dies at the call.
      const saved = await api.post(appPath("/api/system/settings"), {
        multipart: {
          "settings-translator-translator_type": "openrouter",
          "settings-translator-openrouter_url": UNREACHABLE_URL,
        },
      });
      expect(saved.ok()).toBe(true);
    });

    test("the job fails and its reason names the translator", async ({
      page,
      api,
    }) => {
      const kinds = await enabledArrKinds(api);
      if (kinds.length === 0) {
        test.skip(
          true,
          "Skipped: this instance has no enabled Sonarr, Radarr or Sportarr, so there is no media file to translate",
        );
        return;
      }
      const movie = await movieWithSubtitle(api);
      if (movie === null) {
        test.skip(
          true,
          "Skipped: this instance has no movie with a subtitle file to translate",
        );
        return;
      }
      const source = movie.subtitles.find(
        (subtitle) => typeof subtitle.path === "string" && subtitle.path !== "",
      ) as { path: string; code2: string };
      const target = ["hu", "fr", "de", "es"].find(
        (code) => !movie.subtitles.some((subtitle) => subtitle.code2 === code),
      );
      if (target === undefined) {
        test.skip(
          true,
          "Skipped: this movie already has a subtitle in every language the spec could ask for",
        );
        return;
      }
      // Keyed by id: a same-named failure from an earlier run must not hide
      // this one.
      const before = new Set((await failedJobs(api)).map((job) => job.job_id));

      await openApp(page);
      // The toast the page will show when the job dies. The wait starts
      // before the job does, because the toast closes on its own and the
      // job may end while the queue poll below is still running. Starting
      // the job through the API is safe for this assertion: the app
      // announces every failure to every open client, whatever origin the
      // job carries. Only a completed job with no action to offer is
      // limited to the user who started it.
      const toast = expect(
        page.getByRole("alert").filter({ hasText: CONNECTION_FAILURE }),
      ).toBeVisible({ timeout: JOB_CAP_MS });

      // The endpoint resolves movies by their upstream radarrId, scoped by the
      // owning instance, so both travel with the request.
      const queued = await api.patch(
        appPath("/api/subtitles?action=translate"),
        {
          data: {
            id: movie.radarrId,
            arr_instance_id: movie.arr_instance_id,
            type: "movie",
            language: target,
            path: source.path,
            hi: "False",
            forced: "False",
          },
        },
      );
      expect(queued.ok()).toBe(true);

      // The queue records the failure, and the job's name says what died.
      await expect
        .poll(
          async () => {
            const fresh = (await failedJobs(api)).filter(
              (entry) => !before.has(entry.job_id),
            );
            return (
              fresh.find((entry) => (entry.error?.message ?? "") !== "") ?? null
            );
          },
          { timeout: JOB_CAP_MS },
        )
        .not.toBeFalsy();
      const job = (await failedJobs(api))
        .filter((entry) => !before.has(entry.job_id))
        .find((entry) => (entry.error?.message ?? "") !== "") as Job;
      expect(job.job_name).toMatch(/^Failed .+ \(\w{2} to \w{2}\)$/);
      // The actionable connection failure, and the structured reason with it.
      expect(job.error?.message).toContain(CONNECTION_FAILURE);
      expect(job.error?.reason ?? "").not.toBe("");

      // The page announces the same failure, with the same reason.
      await toast;
    });
  },
);
