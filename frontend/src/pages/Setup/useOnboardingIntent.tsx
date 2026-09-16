import {
  createContext,
  FunctionComponent,
  PropsWithChildren,
  useCallback,
  useContext,
  useMemo,
  useState,
} from "react";
import type { WizardIntent } from "./steps/types";

const STORAGE_KEY = "bazarr.onboarding.intent";

function isIntent(value: string | null): value is WizardIntent {
  return value === "library" || value === "discover";
}

function readPersistedIntent(): WizardIntent | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return isIntent(raw) ? raw : null;
  } catch {
    // localStorage can throw in locked-down browsers; fall back to unanswered.
    return null;
  }
}

function persistIntent(intent: WizardIntent | null) {
  try {
    if (intent === null) {
      localStorage.removeItem(STORAGE_KEY);
    } else {
      localStorage.setItem(STORAGE_KEY, intent);
    }
  } catch {
    // Ignore persistence failures; the in-memory answer still works.
  }
}

/**
 * Forgets the stored answer without disturbing the mounted wizard.
 *
 * The Finish step calls this on its way out. Clearing the answer through React
 * state instead would re-filter the rail under a step index that still belongs
 * to the path being left, which shows the reader a step they already finished
 * in the moment between "Finish" and the app taking over.
 */
export function clearPersistedIntent() {
  persistIntent(null);
}

interface OnboardingIntentValue {
  intent: WizardIntent | null;
  setIntent: (intent: WizardIntent) => void;
  resetIntent: () => void;
}

const OnboardingIntentContext = createContext<OnboardingIntentValue | null>(
  null,
);

/**
 * The answer to "what do you want Bazarr+ to do for you?", shared by the shell
 * (which filters the rail with it) and the steps that phrase themselves around
 * it.
 *
 * It is mirrored to localStorage for the same reason the step index is: the
 * providers step installs plugins and restarts Bazarr+, and the wizard resumes
 * through a hard redirect back to /setup. Without the mirror the rail would
 * change shape under the user halfway through setup.
 */
export const OnboardingIntentProvider: FunctionComponent<PropsWithChildren> = ({
  children,
}) => {
  const [intent, setIntentState] = useState<WizardIntent | null>(
    readPersistedIntent,
  );

  const setIntent = useCallback((next: WizardIntent) => {
    setIntentState(next);
    persistIntent(next);
  }, []);

  const resetIntent = useCallback(() => {
    setIntentState(null);
    persistIntent(null);
  }, []);

  const value = useMemo(
    () => ({ intent, setIntent, resetIntent }),
    [intent, setIntent, resetIntent],
  );

  return (
    <OnboardingIntentContext.Provider value={value}>
      {children}
    </OnboardingIntentContext.Provider>
  );
};

/**
 * Reads the wizard intent. Outside the provider (a step rendered on its own in
 * a test, say) it reports the persisted answer and treats writes as no-ops, so
 * a step never has to guard for a missing provider.
 */
export function useOnboardingIntent(): OnboardingIntentValue {
  const value = useContext(OnboardingIntentContext);
  const fallback = useMemo<OnboardingIntentValue>(
    () => ({
      intent: readPersistedIntent(),
      setIntent: persistIntent,
      resetIntent: () => persistIntent(null),
    }),
    [],
  );
  return value ?? fallback;
}
