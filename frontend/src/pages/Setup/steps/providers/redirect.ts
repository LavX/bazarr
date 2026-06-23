import { Environment } from "@/utilities";

/**
 * Hard-redirect back into the wizard after a provider-install restart. We use a
 * full document navigation (not react-router) on purpose: once Sonarr exists,
 * needsOnboarding is false, so the auto-redirect will not return the user to
 * /setup. A hard navigation remounts the app, the wizard reads the persisted
 * step (= providers), and lands on the configure sub-stage because installed
 * providers now exist. Kept as a tiny named function so tests can mock it.
 */
export function redirectToSetup(): void {
  window.location.href = `${Environment.baseUrl}/setup`;
}
