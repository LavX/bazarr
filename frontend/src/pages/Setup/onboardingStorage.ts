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
 * instance has already written. It is copied, never removed. An install served
 * from the root has no base URL, so the un-namespaced key is not a leftover for
 * it, it is the key it still reads and writes. Deleting it would take a root
 * install's cursor and answers away the first time a subpath install on the
 * same origin loaded. A stale copy left behind costs nothing: every install has
 * its own key from its first write onwards, and the legacy value is only ever
 * read by one that has nothing of its own yet.
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
