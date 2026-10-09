/**
 * The What's New modal opens by itself on the first authenticated load of a
 * browser that has not seen the current release, and it sits over the page
 * until it is closed. Specs that are about something else mark it seen first.
 *
 * The release and the storage key are the app's own, not copies: a release
 * that moves the tour version moves the suite with it.
 */
import type { Page } from "@playwright/test";
import { latestWhatsNewVersion } from "@/data/whatsNew";
import { WHATS_NEW_SEEN_KEY } from "@/utilities/whatsNew";

/** The release the modal announces. */
export const WHATS_NEW_VERSION = latestWhatsNewVersion;

/** Where the app records the release a browser has already been shown. */
export { WHATS_NEW_SEEN_KEY };

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
    [WHATS_NEW_SEEN_KEY, WHATS_NEW_VERSION] as const,
  );
}
