/**
 * The What's New modal opens by itself on the first authenticated load of a
 * browser that has not seen the current release, and it sits over the page
 * until it is closed. Specs that are about something else mark it seen first.
 */
import type { Page } from "@playwright/test";

/** The release the modal announces, `latestWhatsNewVersion` in the app. */
export const WHATS_NEW_VERSION = "2.7.0";

/** Where the app records the release a browser has already been shown. */
const SEEN_KEY = "bazarr-whats-new-seen";

/** Marks the current release as seen before any page of this test loads. */
export async function skipWhatsNew(page: Page): Promise<void> {
  await page.addInitScript(
    ([key, version]) => {
      try {
        window.localStorage.setItem(key, version);
      } catch {
        // A browser without storage opens the modal; the spec will say so.
      }
    },
    [SEEN_KEY, WHATS_NEW_VERSION] as const,
  );
}
