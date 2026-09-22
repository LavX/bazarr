import { Environment } from "@/utilities";

const PREFIX = "bazarr.onboarding.";

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
  return base ? `${PREFIX}${name}::${base}` : `${PREFIX}${name}`;
}

/**
 * What an install left under the un-namespaced key, adopted once.
 *
 * The keys were namespaced after these values were already in browsers, and an
 * install behind a reverse proxy under a subpath is the common deployment, so
 * without this the reader most likely to be mid-wizard is the one whose cursor,
 * answer and half-filled server forms all read as absent and who starts again
 * from Welcome.
 *
 * Adopted, never merged: a legacy value is read only when this install has
 * nothing of its own under its own key, so it can never overwrite state an
 * instance has already written. It is removed as it is taken, because the
 * legacy key is the one thing two instances behind one proxy still share: left
 * there, whichever instance loaded next would adopt the same value and inherit
 * the other's place in the wizard.
 */
function adoptLegacyValue(name: string, key: string): string | null {
  const legacyKey = `${PREFIX}${name}`;
  if (legacyKey === key) {
    // No base URL, so this install never stopped using the legacy key.
    return null;
  }
  const value = localStorage.getItem(legacyKey);
  if (value === null) {
    return null;
  }
  localStorage.setItem(key, value);
  localStorage.removeItem(legacyKey);
  return value;
}

export function readOnboardingValue(name: string): string | null {
  try {
    const key = onboardingKey(name);
    const value = localStorage.getItem(key);
    return value === null ? adoptLegacyValue(name, key) : value;
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
