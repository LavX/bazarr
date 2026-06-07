/**
 * Client-side state for the "What's New" wizard.
 *
 * Pure helpers only (no React / modal / data imports) so this module stays trivially
 * importable and unit-testable. The auto-open hook that wires these to the modal lives
 * in `components/modals/useWhatsNewAutoOpen.ts`.
 */

export const WHATS_NEW_SEEN_KEY = "bazarr-whats-new-seen";

export function getSeenWhatsNewVersion(): string | null {
  return localStorage.getItem(WHATS_NEW_SEEN_KEY);
}

export function markWhatsNewSeen(version: string): void {
  localStorage.setItem(WHATS_NEW_SEEN_KEY, version);
}

/**
 * Whether the wizard should auto-open on load. Shows once per latest version: when a
 * stored token is absent (fresh install or first build with the feature) or differs from
 * the latest version, and there is at least one slide to show.
 */
export function shouldAutoOpenWhatsNew(
  seen: string | null,
  latest: string,
  slideCount: number,
): boolean {
  return slideCount > 0 && seen !== latest;
}

/**
 * Live navigation bridge for the wizard.
 *
 * The modal renders outside the Router (via the app-wide ModalsProvider), and the app's
 * browser router is rebuilt whenever route data changes (see Router/index.tsx), so a
 * `navigate` captured at open time goes stale and updates the URL without changing the
 * view. Instead, a component inside the current Router (App) registers its live `navigate`
 * here, and the modal resolves it at click time.
 */
let appNavigateFn: ((to: string) => void) | null = null;

export function registerAppNavigate(fn: ((to: string) => void) | null): void {
  appNavigateFn = fn;
}

export function navigateApp(to: string): void {
  appNavigateFn?.(to);
}
