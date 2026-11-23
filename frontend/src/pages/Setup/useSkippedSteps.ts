import { useCallback, useEffect, useRef, useState } from "react";
import type { WizardPhase } from "./steps/types";
import {
  readOnboardingValue,
  removeOnboardingValue,
  writeOnboardingValue,
} from "./onboardingStorage";

const STORAGE_NAME = "skipped";
const ANSWERED_NAME = "answered";
const WRITTEN_PHASES_NAME = "written-phases";

function readKeys(name: string): string[] {
  const raw = readOnboardingValue(name);
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

/** Forgets the progress records without disturbing a mounted wizard. */
export function clearPersistedSkips() {
  removeOnboardingValue(STORAGE_NAME);
  removeOnboardingValue(ANSWERED_NAME);
  removeOnboardingValue(WRITTEN_PHASES_NAME);
}

function mirror(name: string, keys: string[]) {
  if (keys.length === 0) {
    removeOnboardingValue(name);
  } else {
    writeOnboardingValue(name, JSON.stringify(keys));
  }
}

/**
 * Which steps the reader walked past, and which they answered.
 *
 * Walked past is a skip with the shell's control, or a Continue that wrote
 * nothing. Answered is a Continue that wrote something. It is what lets the
 * rail tell a visited phase from a completed one: the shell cannot read
 * whether a step was answered, because each step decides what that means, but
 * the step says whether its Continue wrote, and the shell keeps the record.
 *
 * A step answered once stays answered when it is revisited and passed again
 * with nothing new to write, and an answer after an earlier skip takes the
 * skip back. The shell's own skip control can replace a Continue answer.
 *
 * A confirmed write that can settle independently of Continue is tracked by
 * phase. It still counts when a generated step disappears or a later part of
 * the save fails. Skipping a step does not undo a write already made.
 *
 * These are mirrored to localStorage like the cursor and the intent, because
 * the providers step restarts Bazarr+ and the wizard resumes through a hard
 * redirect, which would otherwise hand every skipped phase its check back.
 */
export function useSkippedSteps() {
  const [skipped, setSkipped] = useState<string[]>(() =>
    readKeys(STORAGE_NAME),
  );
  const [answered, setAnswered] = useState<string[]>(() =>
    readKeys(ANSWERED_NAME),
  );
  const [writtenPhases, setWrittenPhases] = useState(() =>
    readKeys(WRITTEN_PHASES_NAME),
  );

  // What markUnwritten reads: the latest record, not the one of the render it was made in.
  const answeredNow = useRef(answered);
  answeredNow.current = answered;

  // Mirrored from effects, not from inside the updaters, which have to stay
  // pure or StrictMode's double invocation writes twice.
  useEffect(() => mirror(STORAGE_NAME, skipped), [skipped]);
  useEffect(() => mirror(ANSWERED_NAME, answered), [answered]);
  useEffect(() => mirror(WRITTEN_PHASES_NAME, writtenPhases), [writtenPhases]);

  const markWritten = useCallback((phase: WizardPhase) => {
    setWrittenPhases((current) =>
      current.includes(phase) ? current : [...current, phase],
    );
  }, []);

  const markSkipped = useCallback((key: string) => {
    setSkipped((current) =>
      current.includes(key) ? current : [...current, key],
    );
    // Replace the navigation outcome. Confirmed writes are tracked separately.
    setAnswered((current) => current.filter((entry) => entry !== key));
  }, []);

  const markAnswered = useCallback((key: string) => {
    setAnswered((current) =>
      current.includes(key) ? current : [...current, key],
    );
    setSkipped((current) =>
      current.includes(key)
        ? current.filter((entry) => entry !== key)
        : current,
    );
  }, []);

  /** A Continue that wrote nothing: a walk past, unless the step was answered earlier. */
  const markUnwritten = useCallback((key: string) => {
    if (answeredNow.current.includes(key)) {
      return;
    }
    setSkipped((current) =>
      current.includes(key) ? current : [...current, key],
    );
  }, []);

  const reset = useCallback(() => {
    setSkipped([]);
    setAnswered([]);
    setWrittenPhases([]);
  }, []);

  return {
    skipped,
    writtenPhases,
    markSkipped,
    markAnswered,
    markUnwritten,
    markWritten,
    reset,
  };
}
