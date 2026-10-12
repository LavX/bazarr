/**
 * The manual search modal on a real media file. The modal names the file it
 * searches for, the search runs against the real providers, and it ends with
 * results or a plain no-result, never with a silent empty table.
 *
 * Fixme, not skipped: no run of this suite has a library yet. The harness
 * starts empty containers, the read-only set that runs against a real
 * install excludes live specs, and the suite has no way to seed a library,
 * so this spec has no instance it can run against. It turns on when the
 * suite grows a seeded-library run; the body below is that run's test.
 */
import { expect, test } from "@e2e/fixtures";
import { appPath } from "@e2e/lib/base";
import { enabledArrKinds, movies } from "@e2e/lib/library";
import { skipWhatsNew } from "@e2e/lib/whatsNew";

/** A provider search can take its time; the modal's own flow is bounded. */
const SEARCH_CAP_MS = 60_000;

test.beforeEach(async ({ page }) => {
  await skipWhatsNew(page);
});

test.describe("manual search", { tag: ["@library", "@live"] }, () => {
  test.fixme(
    true,
    "No run of this suite has a library yet: the harness starts empty containers, the read-only set that runs against a real install excludes live specs, and the suite has no way to seed a library. Turn this on when the suite grows a seeded-library run.",
  );

  test("the modal searches a movie's file and reports what it found", async ({
    page,
    api,
  }) => {
    const kinds = await enabledArrKinds(api);
    if (kinds.length === 0) {
      test.skip(
        true,
        "Skipped: this instance has no enabled Sonarr, Radarr or Sportarr, so there is no media file to open",
      );
      return;
    }
    // The modal needs a movie with a language profile to search for; the
    // search itself does not need an existing subtitle. The profile must
    // also exist and offer a language, read the way the upload spec reads
    // it, so a profile with no items or a dangling id cannot pass for a
    // searchable movie and turn the search into a guaranteed no-result.
    const library = await movies(api);
    const withProfile = library.filter((movie) => movie.profileId !== null);
    const profiles = await api.get(appPath("/api/system/languages/profiles"));
    expect(profiles.ok()).toBe(true);
    const profileList = (await profiles.json()) as {
      profileId: number;
      items: { language: string }[];
    }[];
    const movie = withProfile.find((entry) => {
      const profile = profileList.find(
        (candidate) => candidate.profileId === entry.profileId,
      );
      return (profile?.items ?? []).some((item) => item.language);
    });
    if (movie === undefined) {
      test.skip(
        true,
        "Skipped: this instance has no movie whose language profile offers a language to search for",
      );
      return;
    }

    await page.goto(appPath(`/movies/${movie.id}`));
    await expect(page.getByText(movie.title).first()).toBeVisible();

    await page.getByRole("button", { name: "Manual Search" }).click();
    const dialog = page.getByRole("dialog", { name: "Search Subtitles" });
    await expect(dialog).toBeVisible();
    // The modal names the file it searches for.
    await expect(dialog.getByText(movie.path, { exact: false })).toBeVisible();

    // The search does not start by itself: the button still reads Search,
    // not the Searching an auto-started one would show.
    const search = dialog.getByRole("button", { name: "Search", exact: true });
    await expect(search).toBeVisible();
    await search.click();

    // It ends in one of the two outcomes the modal has, never a spinner
    // forever: a result to download, or the modal's own no-result row. A
    // search can find several results, so one visible outcome is enough.
    await expect(
      dialog
        .getByRole("button", { name: "Download", exact: true })
        .or(dialog.getByText("No result", { exact: true }))
        .first(),
    ).toBeVisible({ timeout: SEARCH_CAP_MS });
  });
});
