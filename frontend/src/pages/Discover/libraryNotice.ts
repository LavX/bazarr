/**
 * Whether the reader has closed the "connect a library" notice.
 *
 * The notice answers one fact: no Sonarr, Radarr or Sportarr instance is
 * connected, so the page opens on the global catalog instead of a library half
 * that would be empty. It is said once and then stays out of the way, which
 * needs somewhere to remember the close. There is no server-side dismissal for
 * a per-reader prompt, so this follows the client-side marker the "What's New"
 * wizard already uses (`bazarr-whats-new-seen` in `utilities/whatsNew`), on the
 * shared guarded storage helpers: a locked-down browser makes these no-ops
 * rather than a throw during the page's first render.
 */

import { readStoredValue, writeStoredValue } from "@/utilities/browserStorage";

export const LIBRARY_NOTICE_DISMISSED_KEY =
  "bazarr-discover-library-notice-dismissed";

export function isLibraryNoticeDismissed(): boolean {
  return readStoredValue(LIBRARY_NOTICE_DISMISSED_KEY) === "1";
}

export function dismissLibraryNotice(): void {
  writeStoredValue(LIBRARY_NOTICE_DISMISSED_KEY, "1");
}
