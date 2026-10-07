/**
 * Announcements. The page shows exactly what the instance's announcements API
 * reports, one card per entry with its headline and body rendered, and says
 * so in its own words when there is nothing to show. This spec only reads, so
 * it is safe against a real install.
 */
import { expect, test } from "@e2e/fixtures";
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
    const response = await api.get("/api/system/announcements");
    expect(response.ok()).toBe(true);
    const { data } = (await response.json()) as {
      data: { text: string }[];
    };
    const announcements = data ?? [];

    await page.goto("/system/announcements");
    await expect(page).toHaveTitle(/Announcements - .+ \(System\)/);

    // The cards are one article per announcement.
    const articles = page.getByRole("article");
    if (announcements.length === 0) {
      // The page's own empty state, not a blank list.
      await expect(
        page.getByText("No announcements for now, come back later!"),
      ).toBeVisible();
      await expect(articles).toHaveCount(0);
    } else {
      await expect(articles).toHaveCount(announcements.length);
      // The first card splits its text the way the page does: the lead
      // sentence as its headline, the rest as its body. Both parts render.
      const first = articles.first();
      await expect(first).toBeVisible();
      const cardText = flat(await first.innerText());
      const trimmed = flat(announcements[0].text);
      const lead = /^([\s\S]*?[.!?])(?:\s|$)/.exec(trimmed);
      expect(cardText).toContain(lead ? lead[1] : trimmed);
      const body = lead ? trimmed.slice(lead[0].length) : "";
      if (body.length > 0) {
        // The body clamps at four lines, so its opening is what is on the
        // card; the headline and the body are asserted separately.
        expect(cardText).toContain(body.slice(0, 40));
      }
    }
  });
});
