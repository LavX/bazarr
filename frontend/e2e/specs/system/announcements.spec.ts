/**
 * Announcements. The page shows exactly what the instance's announcements API
 * reports, one card per entry with its headline and body rendered, and says
 * so in its own words when there is nothing to show. This spec only reads, so
 * it is safe against a real install.
 */
import { expect, test } from "@e2e/fixtures";
import { appPath } from "@e2e/lib/base";
import { skipWhatsNew } from "@e2e/lib/whatsNew";

test.beforeEach(async ({ page }) => {
  await skipWhatsNew(page);
});

/** The whitespace a browser collapses out of a text node. */
const flat = (value: string) => value.replace(/\s+/g, " ").trim();

test.describe("announcements", { tag: ["@status"] }, () => {
  test("the page shows the announcements the instance has", async ({
    page,
    api,
  }) => {
    // What the instance has, straight from the API the page reads.
    const response = await api.get(appPath("/api/system/announcements"));
    expect(response.ok()).toBe(true);
    const { data } = (await response.json()) as {
      data: { text: string }[];
    };
    expect(Array.isArray(data)).toBe(true);
    const announcements = data as { text: string }[];

    await page.goto(appPath("/system/announcements"));
    await expect(page).toHaveTitle(/Announcements - .+ \(System\)/);

    // The cards are one article per announcement, in the feed's own order:
    // the page maps the list it is handed without sorting it.
    const articles = page.getByRole("article");
    if (announcements.length === 0) {
      // The page's own empty state, not a blank list.
      await expect(
        page.getByText("No announcements for now, come back later!"),
      ).toBeVisible();
      await expect(articles).toHaveCount(0);
    } else {
      await expect(articles).toHaveCount(announcements.length);
      // Each card carries its own announcement, checked per index so a
      // page that reordered or duplicated entries cannot pass. The split
      // is computed on the trimmed raw text, exactly the way the page
      // splits it (announcementText.ts): the lead sentence is the
      // headline unless it is longer than 160 characters, in which case
      // the whole text is the body and the card opens with it instead.
      // Flattening is only for comparing against rendered text, which
      // the browser itself has flattened; the length rule reads the raw
      // text, as the page's does.
      const MAX_HEADLINE = 160;
      for (let index = 0; index < announcements.length; index += 1) {
        const card = articles.nth(index);
        await expect(card).toBeVisible();
        const trimmed = announcements[index].text.trim();
        expect(trimmed.length).toBeGreaterThan(0);
        const lead = /^([\s\S]*?[.!?])(?:\s|$)/.exec(trimmed);
        const headline = lead ? lead[1] : trimmed;
        const body = (lead ? trimmed.slice(lead[0].length) : "").trim();
        if (headline.length > MAX_HEADLINE) {
          // No headline: the whole trimmed text is the body, and the card
          // opens with it and names itself by it. The full text is what
          // the card has to show here, so a body it left out cannot pass
          // as an empty remainder.
          await expect(card.getByRole("heading")).toHaveCount(0);
          await expect(card).toContainText(flat(trimmed));
          const label = await card.getAttribute("aria-label");
          expect(label ?? "").not.toBe("");
        } else {
          // The headline verbatim in the card's heading, and the body,
          // whenever there is one, rendered below it: the links the page
          // makes of the body's URLs carry the address as their text, so
          // the body's rendered text content is the body itself.
          await expect(card.getByRole("heading")).toContainText(flat(headline));
          if (body.length > 0) {
            await expect(card).toContainText(flat(body));
          }
        }
      }
    }
  });
});
