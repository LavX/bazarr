import {
  readOnboardingValue,
  removeOnboardingValue,
  writeOnboardingValue,
} from "./onboardingStorage";

const STORAGE_NAME = "connection-tests";

/**
 * What the connection Test said about a row the wizard saved.
 *
 * Saving never waits for a Test, so a wrong address or key saves as happily as
 * a right one, and the Finish recap used to call every saved row connected.
 * The step that writes a row records what the Test said about the values it
 * wrote, and the recap reads that back instead of taking the row's existence as
 * proof. Kept in localStorage with the rest of the wizard's bookkeeping,
 * because the providers step restarts Bazarr+ between the connection steps and
 * Finish. A browser that cannot store it reads every row as untested, which is
 * the safe side to fail on.
 */
export type ConnectionTest = "passed" | "failed" | "untested";

export function connectionTestKey(
  scope: "arr" | "media-server",
  id: number | string,
): string {
  return `${scope}:${id}`;
}

export function readConnectionTests(): Record<string, ConnectionTest> {
  const raw = readOnboardingValue(STORAGE_NAME);
  if (!raw) {
    return {};
  }
  try {
    const parsed: unknown = JSON.parse(raw);
    return parsed !== null && typeof parsed === "object"
      ? (parsed as Record<string, ConnectionTest>)
      : {};
  } catch {
    return {};
  }
}

/** What the Test said about one row. A row with no record reads as untested. */
export function readConnectionTest(key: string): ConnectionTest {
  return readConnectionTests()[key] ?? "untested";
}

/**
 * What the Test said about a media server row. Plex has no Test in the wizard:
 * its row comes from signing in and picking one of the account's servers, so
 * it counts as passed, the same as on Finish.
 */
export function readMediaServerTest(kind: string, id: string): ConnectionTest {
  return kind === "plex"
    ? "passed"
    : readConnectionTest(connectionTestKey("media-server", id));
}

export function recordConnectionTest(key: string, result: ConnectionTest) {
  writeOnboardingValue(
    STORAGE_NAME,
    JSON.stringify({ ...readConnectionTests(), [key]: result }),
  );
}

export function clearPersistedConnectionTests() {
  removeOnboardingValue(STORAGE_NAME);
}
