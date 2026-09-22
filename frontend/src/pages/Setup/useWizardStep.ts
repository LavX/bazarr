import { useCallback, useEffect, useRef, useState } from "react";
import type { WizardStepDef } from "./steps/types";
import {
  readOnboardingValue,
  removeOnboardingValue,
  writeOnboardingValue,
} from "./onboardingStorage";
import { readPersistedIntent } from "./useOnboardingIntent";

const STORAGE_NAME = "step";

/**
 * The step list as it stood while the cursor was still a number, in order.
 *
 * A frozen copy on purpose. The registry has moved since: the media server
 * picker is on both paths now, where it used to be library-only. Resolving a
 * stored index against the live list would hand back whatever slid into that
 * slot, which on the Discover path is every later step shifted by one, so a
 * reader who upgraded on Providers would come back to Languages.
 */
const LEGACY_STEP_KEYS = [
  "welcome",
  "intent",
  "sonarr",
  "radarr",
  "sportarr",
  "media-servers",
  "seerr",
  "languages",
  "providers",
  "translator",
  "general",
  "finish",
];

/** What the old list filtered out when the answer was "discover". */
const LEGACY_LIBRARY_ONLY = new Set([
  "sonarr",
  "radarr",
  "sportarr",
  "media-servers",
]);

/** The key an old numeric cursor pointed at, for the path it was walking. */
function legacyKeyAt(index: number): string | undefined {
  const legacy =
    readPersistedIntent() === "discover"
      ? LEGACY_STEP_KEYS.filter((key) => !LEGACY_LIBRARY_ONLY.has(key))
      : LEGACY_STEP_KEYS;
  return legacy[index];
}

/** Forgets the stored cursor without disturbing a mounted wizard. */
export function clearPersistedStep() {
  removeOnboardingValue(STORAGE_NAME);
}

/**
 * The cursor as it was persisted, migrated if it is still an index.
 *
 * The cursor used to be a number, and a reader who upgrades mid-wizard has one
 * in localStorage. It is an index into the list as that version built it, so it
 * is read through the frozen legacy order above and turned into a key. An index
 * past the end of that list, or one naming a step this run does not walk, is
 * discarded rather than guessed at.
 */
function readPersistedKey(steps: WizardStepDef[]): string | null {
  const raw = readOnboardingValue(STORAGE_NAME);
  if (raw === null || raw === "") {
    return null;
  }
  if (/^\d+$/.test(raw)) {
    const migrated = legacyKeyAt(Number.parseInt(raw, 10));
    if (migrated === undefined || !steps.some((s) => s.key === migrated)) {
      removeOnboardingValue(STORAGE_NAME);
      return null;
    }
    writeOnboardingValue(STORAGE_NAME, migrated);
    return migrated;
  }
  return raw;
}

/**
 * The step to land on when the stored one is gone from the list.
 *
 * Untick a media server on the picker and the configure step it generated
 * disappears, which can happen while the reader is standing on it. The nearest
 * surviving step before it is the honest answer: it is a screen they have
 * already seen, it is never blank, and it never skips them forward past
 * something they have not answered.
 */
function nearestSurviving(
  previous: WizardStepDef[],
  steps: WizardStepDef[],
  key: string | null,
): number {
  const was = key === null ? -1 : previous.findIndex((s) => s.key === key);
  for (let i = was - 1; i >= 0; i -= 1) {
    const survivor = steps.findIndex((s) => s.key === previous[i].key);
    if (survivor >= 0) {
      return survivor;
    }
  }
  return 0;
}

export interface WizardCursor {
  index: number;
  step: WizardStepDef;
  next: () => void;
  back: () => void;
  goTo: (key: string) => void;
  reset: () => void;
}

/**
 * Tracks the active onboarding step by key and mirrors it to localStorage, so a
 * reload mid-setup lands on the same step.
 *
 * The cursor is a key rather than an index because the list is generated: a
 * reader reaches the media server picker by pressing Back out of the segment it
 * generates, changes what is ticked, and walks forward into a list that has
 * been renumbered underneath them. An index would point at whatever moved into
 * that slot. reset() clears the key, which is what "Skip setup" and completion
 * call so the wizard starts clean next time.
 */
export function useWizardStep(steps: WizardStepDef[]): WizardCursor {
  const [activeKey, setActiveKey] = useState<string | null>(() =>
    readPersistedKey(steps),
  );
  const previous = useRef(steps);
  const stepsRef = useRef(steps);
  stepsRef.current = steps;

  const found = steps.findIndex((s) => s.key === activeKey);
  const index =
    found >= 0 ? found : nearestSurviving(previous.current, steps, activeKey);
  const step = steps[index] ?? steps[0];
  const indexRef = useRef(index);
  indexRef.current = index;

  const goTo = useCallback((key: string) => {
    setActiveKey(key);
    writeOnboardingValue(STORAGE_NAME, key);
  }, []);

  // The cursor is corrected in state, never only for the render: a stored key
  // that no longer resolves is rewritten to the step actually being shown, so
  // the next reload agrees with this one.
  useEffect(() => {
    previous.current = stepsRef.current;
    if (activeKey !== null && step !== undefined && step.key !== activeKey) {
      goTo(step.key);
    }
  }, [steps, step, activeKey, goTo]);

  const next = useCallback(() => {
    const list = stepsRef.current;
    const target = list[Math.min(indexRef.current + 1, list.length - 1)];
    if (target) {
      goTo(target.key);
    }
  }, [goTo]);

  const back = useCallback(() => {
    const list = stepsRef.current;
    const target = list[Math.max(indexRef.current - 1, 0)];
    if (target) {
      goTo(target.key);
    }
  }, [goTo]);

  const reset = useCallback(() => {
    setActiveKey(null);
    clearPersistedStep();
  }, []);

  return { index, step, next, back, goTo, reset };
}
