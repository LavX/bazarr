/**
 * Discover always runs on the built-in TMDB key, so an install that never saved
 * a key of its own, which is what the shared instance is, shows the catalog
 * feeds straight away and never asks the reader to connect or set up TMDB.
 * Nothing here changes the instance.
 */
import { expect, test } from "@e2e/fixtures";
import { openDiscover } from "@e2e/lib/discover";

/** The feeds come from TMDB, over the real network. */
const FEED_TIMEOUT_MS = 30_000;

/** Copy that would send the reader off to set TMDB up first. */
const SETUP_COPY = [
  /connect\s+tmdb/i,
  /set\s+up\s+tmdb/i,
  /set\s+up\s+discover/i,
  /set\s+up\s+(recent episodes|digital releases)/i,
  /check\s+the\s+tmdb\s+key/i,
  /explore\s+beyond\s+your\s+library/i,
];

/** What each feed says while it is still asking TMDB. */
const LOADING_COPY = [
  "Loading weekly trending titles.",
  "Checking recent episodes.",
  /^Checking digital releases in /,
];

test.describe(
  "Discover on the built-in TMDB key",
  { tag: ["@discover"] },
  () => {
    test("the feeds show without asking to set up TMDB", async ({ page }) => {
      await openDiscover(page);

      const trending = page.getByRole("region", { name: "Trending this week" });
      await expect(trending).toBeVisible();
      // Titles, or a feed that says TMDB is down or had nothing this week. A
      // setup prompt, or a settings error, is neither.
      const titles = trending
        .getByRole("list", { name: "Weekly trending titles" })
        .getByRole("listitem");
      const notice = trending.getByText(
        /^(Weekly trending is temporarily unavailable|TMDB is temporarily unavailable|No titles in this weekly TMDB feed)/,
      );
      await expect(titles.first().or(notice).first()).toBeVisible({
        timeout: FEED_TIMEOUT_MS,
      });

      // Only once every feed has answered does "no prompt" mean anything.
      for (const loading of LOADING_COPY) {
        await expect(page.getByText(loading)).toHaveCount(0, {
          timeout: FEED_TIMEOUT_MS,
        });
      }
      for (const copy of SETUP_COPY) {
        await expect(page.getByText(copy)).toHaveCount(0);
      }
    });
  },
);
