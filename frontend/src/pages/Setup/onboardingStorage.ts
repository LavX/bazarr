import { Environment } from "@/utilities";

/**
 * localStorage keys for the wizard's own bookkeeping, namespaced by base URL.
 *
 * Two Bazarr+ instances served from one host under different base URLs share an
 * origin, so they shared `bazarr.onboarding.step` and `bazarr.onboarding.intent`
 * too: answering the intent on one moved the other's cursor. The namespace is
 * appended rather than prefixed so the plain key still reads first in a
 * debugger, and an install with no base URL keeps exactly the key it had.
 */
export function onboardingKey(name: string): string {
  const base = Environment.baseUrl;
  return base
    ? `bazarr.onboarding.${name}::${base}`
    : `bazarr.onboarding.${name}`;
}

export function readOnboardingValue(name: string): string | null {
  try {
    return localStorage.getItem(onboardingKey(name));
  } catch {
    // localStorage throws in locked-down browsers. The wizard still works from
    // memory, it just cannot survive a reload.
    return null;
  }
}

export function writeOnboardingValue(name: string, value: string) {
  try {
    localStorage.setItem(onboardingKey(name), value);
  } catch {
    // Ignore persistence failures; the in-memory state still works.
  }
}

export function removeOnboardingValue(name: string) {
  try {
    localStorage.removeItem(onboardingKey(name));
  } catch {
    // Ignore removal failures.
  }
}
