/* eslint-disable camelcase -- API bodies keep their transport field names. */
/**
 * Settings saves and the missing-subtitles recalculation. A save that
 * changes language profiles is answered 204 and enqueues the recalculation
 * as a queued job, which runs and completes: staging the profile in Settings
 * and applying it with the save control asks for the same pass an API save
 * does, and exactly one recalculation comes out of that one save. A save
 * refused at its validation queues no recalculation and writes nothing: no
 * recalculation, no profile, no general setting.
 *
 * The staged pass is never asked through the API, so the pass that spec
 * sees is the one the save control itself asked for. What these specs do
 * not prove is whether the save's answer waits for the recalculation to
 * finish. With an empty library the job completes at once, and it runs on
 * the backend, where no route this suite can hold touches it, so the two
 * cannot be told apart deterministically here.
 *
 * Stateful: it saves settings. It needs no library; with none the
 * recalculation runs every step it has, which is none, and completes at
 * once.
 */
import { expect, test } from "@e2e/fixtures";
import { appPath } from "@e2e/lib/base";
import { liveChannel, waitForJob } from "@e2e/lib/jobs";
import { completeOnboarding } from "@e2e/lib/shared";
import { skipWhatsNew } from "@e2e/lib/whatsNew";
import type { APIRequestContext, Page } from "@playwright/test";

test.beforeEach(async ({ page, bazarr }) => {
  await skipWhatsNew(page);
  await completeOnboarding(bazarr);
});

/** The drawer's section for one status, headed by that status. */
function jobsSection(page: Page, title: "Failed" | "Completed") {
  return page
    .getByRole("dialog", { name: "Jobs Manager" })
    .getByRole("heading", { level: 3, name: title, exact: true })
    .locator("xpath=ancestor::div[contains(@class, 'mantine-Stack-root')][1]");
}

/** The page's save control once it holds this many staged changes. */
function saveControl(page: Page, staged: number) {
  return page.getByRole("button", {
    name: `Save ${staged} pending change${staged !== 1 ? "s" : ""}`,
    exact: true,
  });
}

/** The response of a settings save, once the page asks for it. */
function settingsSave(page: Page) {
  return page.waitForResponse(
    (response) =>
      response.url().includes("/api/system/settings") &&
      response.request().method() === "POST",
  );
}

const RECALCULATION = "Recalculating missing subtitles";

/** A language profile in the shape the settings save reads. */
function profile(name: string) {
  return JSON.stringify([
    {
      profileId: 1,
      name,
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
}

/** The job queue as it stands. The endpoint answers { data: [...] }. */
async function jobs(api: APIRequestContext) {
  const response = await api.get(appPath("/api/system/jobs"));
  expect(response.ok()).toBe(true);
  const { data } = (await response.json()) as {
    data: { job_id: number; job_name: string; status: string }[];
  };
  expect(Array.isArray(data)).toBe(true);
  return data as { job_id: number; job_name: string; status: string }[];
}

/** Every recalculation job on the queue. */
async function recalculationJobs(api: APIRequestContext) {
  return (await jobs(api)).filter((job) => job.job_name === RECALCULATION);
}

/**
 * Waits until no recalculation job is pending or running, so the queue is
 * settled: a baseline taken after this cannot pick up a job an earlier
 * save, such as onboarding's, has not finished with yet.
 */
async function settleRecalculations(api: APIRequestContext) {
  await expect
    .poll(
      async () =>
        (await recalculationJobs(api)).filter(
          (job) => job.status === "pending" || job.status === "running",
        ),
      { timeout: 30_000 },
    )
    .toHaveLength(0);
}

/** The profiles the instance has, whole, as the profiles endpoint reports. */
async function profiles(api: APIRequestContext) {
  const response = await api.get(appPath("/api/system/languages/profiles"));
  expect(response.ok()).toBe(true);
  const body = (await response.json()) as unknown;
  // The endpoint answers with the bare list; anything else is a contract
  // change this spec must hear about.
  expect(Array.isArray(body)).toBe(true);
  return body;
}

/** The general settings, read the way the settings page reads them. */
async function generalSettings(api: APIRequestContext) {
  const response = await api.get(appPath("/api/system/settings"));
  expect(response.ok()).toBe(true);
  const body = (await response.json()) as { general: Record<string, unknown> };
  expect(body.general).toBeTruthy();
  return body.general;
}

/** Saves a profile and resolves once the recalculation it queues has run. */
async function saveProfileAndWait(
  api: APIRequestContext,
  name: string,
  knownJobs: Set<number>,
) {
  const saved = await api.post(appPath("/api/system/settings"), {
    multipart: { "languages-profiles": profile(name) },
  });
  expect(saved.status()).toBe(204);
  await expect
    .poll(async () => {
      const fresh = (await recalculationJobs(api)).filter(
        (job) => !knownJobs.has(job.job_id),
      );
      return fresh[0] ?? null;
    })
    .not.toBeFalsy();
  const queued = (await recalculationJobs(api)).find(
    (job) => !knownJobs.has(job.job_id),
  ) as { job_id: number };
  const job = await waitForJob(api, queued.job_id, "completed");
  expect(job.job_name).toBe(RECALCULATION);
}

test.describe(
  "settings saves and the recalculation",
  {
    tag: ["@jobs", "@stateful"],
  },
  () => {
    test("a saved language profile queues the missing-subtitles pass", async ({
      page,
      api,
    }) => {
      // The live channel has to be connected before the drawer's card can
      // arrive: the jobs query never goes stale, so the finished pass reaches it
      // through the live channel.
      const channel = liveChannel(page);
      await page.goto(appPath("/settings/languages"));
      await channel;

      // A profile needs an enabled language, and the fresh install has none.
      // The filter's label is not passed to its combobox, but the section holds
      // no other one.
      const filter = page
        .getByRole("heading", { level: 4, name: "Subtitles Language" })
        .locator(
          "xpath=ancestor::div[contains(@class, 'mantine-Stack-root')][1]",
        )
        .getByRole("combobox");
      await filter.click();
      await filter.fill("English");
      await page.getByRole("option", { name: "English", exact: true }).click();

      const savedLanguage = settingsSave(page);
      await saveControl(page, 1).click();
      expect((await savedLanguage).ok()).toBe(true);

      // The save's reload clears the form, and a profile staged before that
      // lands is thrown away with it. The button below is already enabled by
      // the staged language, so the reload needs its own wait.
      await expect(saveControl(page, 1)).toBeHidden();

      // The button reads as Add New Profile only once the reload brought the
      // enabled language the profile needs.
      const addProfile = page.getByRole("button", { name: "Add New Profile" });
      await expect(addProfile).toBeEnabled();
      await addProfile.click();

      const dialog = page.getByRole("dialog", {
        name: "Edit Languages Profile",
      });
      await dialog.getByLabel("Name").fill("E2E UI Recalc");
      await dialog
        .getByRole("button", { name: "Add Language", exact: true })
        .click();
      await dialog.getByRole("button", { name: "Save", exact: true }).click();
      await expect(dialog).toBeHidden();

      const savedProfile = settingsSave(page);
      await saveControl(page, 1).click();
      expect((await savedProfile).ok()).toBe(true);

      const name = "Recalculating missing subtitles";
      await expect
        .poll(
          async () => {
            const response = await api.get(
              appPath("/api/system/jobs?status=completed"),
            );
            const { data } = (await response.json()) as {
              data: { job_name: string }[];
            };
            return data.some((job) => job.job_name === name);
          },
          { timeout: 30_000 },
        )
        .toBe(true);

      // The save's reload clears the form again; the row below comes from the
      // server's list once it has. Its pass is in the drawer.
      await expect(saveControl(page, 1)).toBeHidden();
      await expect(
        page.getByRole("cell", { name: "E2E UI Recalc" }),
      ).toBeVisible();
      await page.getByRole("button", { name: "Jobs Manager" }).click();
      const card = jobsSection(page, "Completed")
        .getByText(name, { exact: true })
        .locator(
          "xpath=ancestor::div[contains(@class, 'mantine-Card-root')][1]",
        );
      await expect(card).toBeVisible();
    });

    test("a profile save enqueues the missing recalculation and finishes", async ({
      api,
    }) => {
      // Onboarding can leave its own recalculation still on the queue, so
      // the queue is settled first: the baseline below then contains no
      // pending job this save's could coalesce with.
      await settleRecalculations(api);
      const before = new Set((await jobs(api)).map((job) => job.job_id));

      await saveProfileAndWait(api, "E2E Recalculation", before);

      // Exactly one recalculation came out of that one save.
      expect(
        (await recalculationJobs(api)).filter((job) => !before.has(job.job_id)),
      ).toHaveLength(1);
    });

    test("a refused save queues no recalculation and writes nothing", async ({
      api,
    }) => {
      // A settled queue and a settled instance first: a profile save whose
      // recalculation has run, so nothing of this test's can be pending.
      const before = new Set((await jobs(api)).map((job) => job.job_id));
      await saveProfileAndWait(api, "E2E Sentinel Before", before);
      // Nothing recalculation-shaped is pending or running from here on, so
      // a job the refused save below might enqueue has nowhere to hide.
      await settleRecalculations(api);
      const settledJobs = new Set((await jobs(api)).map((job) => job.job_id));
      const settledProfiles = await profiles(api);
      const settledSettings = await generalSettings(api);

      // An invalid combine rule: a language the profile does not have. The
      // save is refused before its configuration, rows or queued work, and
      // the harmless general key is what proves the configuration was never
      // written either. Instance name is a real key: the settings endpoint
      // reports it under general.instance_name.
      const refused = await api.post(appPath("/api/system/settings"), {
        multipart: {
          "settings-general-instance_name": "E2E Refused Instance",
          "languages-profiles": JSON.stringify([
            {
              profileId: 2,
              name: "E2E Refused",
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
              combine: { languages: ["en", "xx"], format: "srt" },
            },
          ]),
        },
      });
      expect(refused.status()).toBe(400);
      expect(await refused.text()).toContain(
        "Invalid combine rule for profile",
      );

      // Nothing the refused save carried was written: the profiles are the
      // same whole objects as before, and the general setting it tried to
      // change never changed.
      expect(await profiles(api)).toEqual(settledProfiles);
      expect(await generalSettings(api)).toEqual(settledSettings);

      // And nothing reached the queue either, checked right here and before
      // the sentinel save below, so a job the refused save wrongly enqueued
      // cannot coalesce with a later one and slip through the count.
      expect(
        (await recalculationJobs(api)).filter(
          (job) => !settledJobs.has(job.job_id),
        ),
      ).toHaveLength(0);

      // One more real save, waited out, settles the sync-or-async question:
      // its recalculation is the only new one, so the refused save queued
      // nothing that the sentinel could have been blamed for.
      await saveProfileAndWait(api, "E2E Sentinel After", settledJobs);
      const newRecalculations = (await recalculationJobs(api)).filter(
        (job) => !settledJobs.has(job.job_id),
      );
      expect(newRecalculations).toHaveLength(1);
    });
  },
);
