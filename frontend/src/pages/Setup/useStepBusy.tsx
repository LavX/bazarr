import {
  createContext,
  FunctionComponent,
  PropsWithChildren,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

interface StepBusyContextValue {
  busy: boolean;
  setBusy: (busy: boolean) => void;
}

const StepBusyContext = createContext<StepBusyContextValue | null>(null);

/**
 * Whether the step on screen is in the middle of work that leaving would break.
 *
 * The shell owns the skip control, so it is the one thing on the page that can
 * take a step away from itself. That is fine for a step whose work starts when
 * the reader presses Continue, and wrong for the providers step: it installs
 * over the network and then restarts Bazarr+ to load what it staged. Skipping
 * mid-run left the installs finishing into a component nobody was rendering,
 * restarted the application under a reader who had moved on, and armed a health
 * poll whose cleanup had already gone.
 */
export const StepBusyProvider: FunctionComponent<PropsWithChildren> = ({
  children,
}) => {
  const [busy, setBusy] = useState(false);
  const value = useMemo(() => ({ busy, setBusy }), [busy]);
  return (
    <StepBusyContext.Provider value={value}>
      {children}
    </StepBusyContext.Provider>
  );
};

/** Read by the shell, to decide whether leaving the step is safe. */
export function useStepBusy(): boolean {
  return useContext(StepBusyContext)?.busy ?? false;
}

/**
 * Declared by a step that is doing work it has to finish. Cleared when the work
 * ends and when the step unmounts, so nothing can leave the flag stuck on.
 */
export function useReportStepBusy(busy: boolean) {
  const setBusy = useContext(StepBusyContext)?.setBusy;
  useEffect(() => {
    if (setBusy === undefined) {
      return;
    }
    setBusy(busy);
    return () => setBusy(false);
  }, [busy, setBusy]);
}
