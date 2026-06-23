import { useCallback, useState } from "react";

const STORAGE_KEY = "bazarr.onboarding.step";

function readPersistedStep(): number {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw === null) {
      return 0;
    }
    const parsed = Number.parseInt(raw, 10);
    return Number.isNaN(parsed) || parsed < 0 ? 0 : parsed;
  } catch {
    // localStorage can throw in locked-down browsers; fall back to step 0.
    return 0;
  }
}

function persistStep(step: number) {
  try {
    localStorage.setItem(STORAGE_KEY, String(step));
  } catch {
    // Ignore persistence failures; the in-memory step still works.
  }
}

/**
 * Tracks the active onboarding step and mirrors it to localStorage so a reload
 * mid-setup lands the user back on the same step. reset() clears the key, which
 * is what "Skip setup" / completion call so the wizard starts clean next time.
 */
export function useWizardStep(): {
  step: number;
  setStep: (n: number) => void;
  next: () => void;
  back: () => void;
  reset: () => void;
} {
  const [step, setStepState] = useState<number>(readPersistedStep);

  const setStep = useCallback((n: number) => {
    const clamped = n < 0 ? 0 : n;
    setStepState(clamped);
    persistStep(clamped);
  }, []);

  const next = useCallback(() => {
    setStepState((current) => {
      const value = current + 1;
      persistStep(value);
      return value;
    });
  }, []);

  const back = useCallback(() => {
    setStepState((current) => {
      const value = current > 0 ? current - 1 : 0;
      persistStep(value);
      return value;
    });
  }, []);

  const reset = useCallback(() => {
    setStepState(0);
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch {
      // Ignore removal failures.
    }
  }, []);

  return { step, setStep, next, back, reset };
}
