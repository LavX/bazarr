/**
 * Asserts that a screen fits the window it is shown in: the page itself does
 * not scroll, and no scroll container on it hides content behind a scrollbar.
 */
import type { Page } from "@playwright/test";
import { expect } from "@playwright/test";

/** The window the wizard is designed to fit without scrolling. */
export const WIZARD_VIEWPORT = { width: 1920, height: 940 };

interface FitReport {
  /** How far the document runs past the bottom of the window, in pixels. */
  pageOverflow: number;
  /** Scroll containers whose content is taller or wider than they are. */
  clipped: string[];
}

function measure(page: Page): Promise<FitReport> {
  return page.evaluate(() => {
    const describe = (element: Element) => {
      const id = element.id ? `#${element.id}` : "";
      const label = element.getAttribute("aria-label");
      const classes = Array.from(element.classList).slice(0, 2).join(".");
      return `${element.tagName.toLowerCase()}${id}${classes ? `.${classes}` : ""}${label ? `[${label}]` : ""}`;
    };
    const clipped: string[] = [];
    for (const element of Array.from(document.body.querySelectorAll("*"))) {
      const style = getComputedStyle(element);
      const scrollsY = /auto|scroll/.test(style.overflowY);
      const scrollsX = /auto|scroll/.test(style.overflowX);
      if (!scrollsY && !scrollsX) continue;
      const hiddenY = element.scrollHeight - element.clientHeight > 1;
      const hiddenX = element.scrollWidth - element.clientWidth > 1;
      if ((scrollsY && hiddenY) || (scrollsX && hiddenX)) {
        clipped.push(
          `${describe(element)} ${element.scrollWidth}x${element.scrollHeight} in ${element.clientWidth}x${element.clientHeight}`,
        );
      }
    }
    const root = document.documentElement;
    return {
      pageOverflow: Math.max(0, root.scrollHeight - window.innerHeight),
      clipped,
    };
  });
}

/**
 * Polls briefly so a layout that is still settling gets its chance, then
 * fails with what overflowed.
 */
export async function expectFitsViewport(page: Page, what = "the screen") {
  await expect
    .poll(() => measure(page), {
      message: `${what} should fit the window without scrolling`,
    })
    .toEqual({ pageOverflow: 0, clipped: [] });
}
