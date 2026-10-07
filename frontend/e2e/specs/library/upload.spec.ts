/**
 * Uploading a subtitle to a real media file through the page's own upload
 * flow: the toolbar's Upload button opens the file chooser, the modal lists
 * the chosen file, the upload writes it next to the video, and the modal
 * closes on its own.
 *
 * Fixme, not skipped: no run of this suite has a library yet. The harness
 * starts empty containers, the read-only set that runs against a real
 * install excludes stateful specs, and the suite has no way to seed a
 * library, so this spec has no instance it can run against. It turns on
 * when the suite grows a seeded-library run; the body below is that run's
 * test.
 */
import { expect, test } from "@e2e/fixtures";
import { enabledArrKinds, movies } from "@e2e/lib/library";
import { completeOnboarding } from "@e2e/lib/shared";
import { skipWhatsNew } from "@e2e/lib/whatsNew";

const UPLOAD_NAME = "e2e-upload.srt";
/** The upload itself is fast once the modal has the file. */
const UPLOAD_CAP_MS = 30_000;

const SRT = [
  "1",
  "00:00:01,000 --> 00:00:02,000",
  "uploaded by the end-to-end suite",
  "",
].join("\n");

test.describe("subtitle upload", { tag: ["@library", "@stateful"] }, () => {
  test.fixme(
    true,
    "No run of this suite has a library yet: the harness starts empty containers, the read-only set that runs against a real install excludes stateful specs, and the suite has no way to seed a library. Turn this on when the suite grows a seeded-library run.",
  );

  test.beforeEach(async ({ bazarr, page }) => {
    await skipWhatsNew(page);
    await completeOnboarding(bazarr);
  });

  test("an uploaded subtitle lands on the movie and the modal closes", async ({
    page,
    api,
  }) => {
    const kinds = await enabledArrKinds(api);
    if (kinds.length === 0) {
      test.skip(
        true,
        "Skipped: this instance has no enabled Sonarr, Radarr or Sportarr, so there is no media file to upload to",
      );
      return;
    }
    // The movie needs a language profile for the modal's language options,
    // and the profile's first language is what the modal preselects, so that
    // one must have no subtitle on the movie yet: the upload then writes a
    // new file instead of overwriting an existing one.
    const library = await movies(api);
    const withProfile = library.filter((movie) => movie.profileId !== null);
    if (withProfile.length === 0) {
      test.skip(
        true,
        "Skipped: this instance has no movie with a language profile to upload to",
      );
      return;
    }
    const profiles = await api.get("/api/system/languages/profiles");
    expect(profiles.ok()).toBe(true);
    const profileList = (await profiles.json()) as {
      profileId: number;
      items: { language: string }[];
    }[];
    const movie =
      withProfile.find((entry) => {
        const profile = profileList.find(
          (candidate) => candidate.profileId === entry.profileId,
        );
        const first = profile?.items[0];
        const taken = new Set(entry.subtitles.map((s) => s.code2));
        return first !== undefined && !taken.has(first.language);
      }) ?? null;
    if (movie === null) {
      test.skip(
        true,
        "Skipped: every movie with a profile already has a subtitle in the language its profile offers first",
      );
      return;
    }
    const before = new Set(
      movie.subtitles
        .map((subtitle) => subtitle.path)
        .filter((path): path is string => typeof path === "string"),
    );

    await page.goto(`/movies/${movie.id}`);
    await expect(page.getByText(movie.title).first()).toBeVisible();

    // The toolbar's Upload opens the browser's file chooser; the page's own
    // drop zone is what accepts the file.
    const chooser = page.waitForEvent("filechooser");
    await page.getByRole("button", { name: "Upload", exact: true }).click();
    const file = await chooser;
    await file.setFiles([
      { name: UPLOAD_NAME, mimeType: "text/plain", buffer: Buffer.from(SRT) },
    ]);

    const dialog = page.getByRole("dialog", { name: "Upload Subtitles" });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByText(UPLOAD_NAME)).toBeVisible();

    // The movie's profile supplies the languages; the first one is what the
    // upload uses. It is preselected when the profile has one, and chosen
    // here when it is not.
    const row = dialog.getByRole("row").filter({ hasText: UPLOAD_NAME });
    const language = row.getByRole("combobox").first();
    if ((await language.inputValue()) === "") {
      await language.click();
      await page.getByRole("option").first().click();
    }

    await dialog.getByRole("button", { name: "Upload", exact: true }).click();
    await expect(dialog).toBeHidden({ timeout: UPLOAD_CAP_MS });

    // The subtitle is on the instance now, next to the video, read back
    // through the API's own id[] filter.
    await expect
      .poll(async () => {
        const fresh = await movies(api, movie.id);
        return (fresh[0]?.subtitles ?? []).some(
          (subtitle) => subtitle.path !== null && !before.has(subtitle.path),
        );
      })
      .toBe(true);
  });
});
