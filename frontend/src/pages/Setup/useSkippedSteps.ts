import { useCallback, useEffect, useState } from "react";
import {
  readOnboardingValue,
  removeOnboardingValue,
  writeOnboardingValue,
} from "./onboardingStorage";

const STORAGE_NAME = "skipped";

function readSkipped(): string[] {
  const raw = readOnboardingValue(STORAGE_NAME);
  if (!raw) {
    return [];
  }
  try {
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed)
      ? parsed.filter((key): key is string => typeof key === "string")
      : [];
  } catch {
    return [];
  }
}

/** Forgets the record without disturbing a mounted wizard. */
export function clearPersistedSkips() {
  removeOnboardingValue(STORAGE_NAME);
}

/**
 * Which steps the reader walked past with the shell's skip control, as opposed
 * to answering them with the step's own Continue.
 *
 * It is what lets the rail tell a visited phase from a completed one. The
 * shell cannot read whether a step was answered, because each step decides
 * what that means and several legitimately advance without writing anything,
 * but it does own the skip control, so "skipped" is the one half of the
 * distinction it can know for certain. A step answered after an earlier skip
 * takes the skip back.
 *
 * Mirrored to localStorage like the cursor and the intent, because the
 * providers step restarts Bazarr+ and the wizard resumes through a hard
 * redirect, which would otherwise hand every skipped phase its check back.
 */
export function useSkippedSteps() {
  const [skipped, setSkipped] = useState<string[]>(readSkipped);

  // Mirrored from an effect, not from inside the updater, which has to stay
  // pure or StrictMode's double invocation writes twice.
  useEffect(() => {
    if (skipped.length === 0) {
      clearPersistedSkips();
    } else {
      writeOnboardingValue(STORAGE_NAME, JSON.stringify(skipped));
    }
  }, [skipped]);

  const markSkipped = useCallback((key: string) => {
    setSkipped((current) =>
      current.includes(key) ? current : [...current, key],
    );
  }, []);

  const markAnswered = useCallback((key: string) => {
    setSkipped((current) =>
      current.includes(key)
        ? current.filter((entry) => entry !== key)
        : current,
    );
  }, []);

  const reset = useCallback(() => setSkipped([]), []);

  return { skipped, markSkipped, markAnswered, reset };
}
