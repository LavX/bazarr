/**
 * The fit check itself, on pages written here rather than served by Bazarr+.
 * It needs a real layout engine, so it runs in Playwright, but it imports
 * Playwright's own test object: the suite's would start a container for it.
 */
import { expectFitsViewport, SUITE_VIEWPORT } from "@e2e/lib/fit";
import { expect, test } from "@playwright/test";

const page = (body: string) =>
  `<!doctype html><html><body style="margin:0">${body}</body></html>`;

test.describe("the fit check", { tag: ["@harness"] }, () => {
  test.use({ viewport: SUITE_VIEWPORT });

  test("passes a page that fits the window", async ({ page: tab }) => {
    await tab.setContent(page('<main style="height:200px">fits</main>'));
    await expectFitsViewport(tab);
  });

  test("fails a page that runs past the bottom of the window", async ({
    page: tab,
  }) => {
    await tab.setContent(page('<main style="height:3000px">tall</main>'));
    await expect(expectFitsViewport(tab)).rejects.toThrow(/pageOverflow/);
  });

  test("fails a page wider than the window", async ({ page: tab }) => {
    // Nothing on it scrolls: the wide child sits in a box that lets it spill,
    // so only the page itself grows a horizontal scrollbar.
    await tab.setContent(
      page('<main><div style="width:3000px;height:10px">wide</div></main>'),
    );
    await expect(expectFitsViewport(tab)).rejects.toThrow(/pageOverflowX/);
  });
});
