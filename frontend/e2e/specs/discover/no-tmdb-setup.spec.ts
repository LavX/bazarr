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

/**
 * The feeds below Trending. Each ends with its titles, its notice that TMDB
 * is down, or its text for a window with nothing in it.
 */
const OTHER_FEEDS = [
  {
    region: "New episodes",
    list: "New episodes",
    notice:
      /^(New episodes are temporarily unavailable|No qualifying episodes were found)/,
  },
  {
    region: "Recent digital releases",
    list: "Recent digital films",
    notice:
      /^(Digital releases are temporarily unavailable|No recent digital releases found)/,
  },
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

      // The promise is about every feed, so each has to be on the page and
      // finish. A feed that is missing, or never gets past loading, would
      // otherwise pass as one that asked for nothing.
      for (const feed of OTHER_FEEDS) {
        const region = page.getByRole("region", {
          name: feed.region,
          exact: true,
        });
        await expect(region).toBeVisible();
        const items = region
          .getByRole("list", { name: feed.list, exact: true })
          .getByRole("listitem");
        const ended = region.getByText(feed.notice);
        await expect(items.first().or(ended).first()).toBeVisible({
          timeout: FEED_TIMEOUT_MS,
        });
      }

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
