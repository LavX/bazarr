import {
  createContext,
  FunctionComponent,
  PropsWithChildren,
  useCallback,
  useContext,
  useMemo,
  useState,
} from "react";

type DraftStore = Record<string, Record<string, unknown>>;

interface StepDraftContextValue {
  drafts: DraftStore;
  patch: (stepKey: string, values: Record<string, unknown>) => void;
  clear: () => void;
}

const StepDraftContext = createContext<StepDraftContextValue | null>(null);

/**
 * What the reader typed on a step, kept while the wizard is open.
 *
 * The shell remounts a step component whenever the cursor moves (it is keyed by
 * the step), so every field held in the step's own useState was gone the moment
 * Back was pressed: fill in a Sonarr address, port and key, go back one screen
 * to re-read a question, come forward, and the form is empty again with no
 * warning that it would be.
 *
 * Memory only, never localStorage. The wizard collects hosts and API keys, and
 * a draft that outlives the tab is a credential sitting in browser storage for
 * anyone with the machine. Steps also keep their secrets out of the draft
 * entirely (see the note in each step): a reload starts those fields empty,
 * which is the honest behaviour for a password field.
 */
export const StepDraftProvider: FunctionComponent<PropsWithChildren> = ({
  children,
}) => {
  const [drafts, setDrafts] = useState<DraftStore>({});

  const patch = useCallback(
    (stepKey: string, values: Record<string, unknown>) => {
      setDrafts((current) => ({
        ...current,
        [stepKey]: { ...current[stepKey], ...values },
      }));
    },
    [],
  );

  const clear = useCallback(() => setDrafts({}), []);

  const value = useMemo(
    () => ({ drafts, patch, clear }),
    [drafts, patch, clear],
  );

  return (
    <StepDraftContext.Provider value={value}>
      {children}
    </StepDraftContext.Provider>
  );
};

const NO_DRAFTS_TO_CLEAR = () => {
  // A step mounted outside the wizard has no shared store to clear.
};

/** Drops every draft. Called when the wizard ends, however it ends. */
export function useClearStepDrafts(): () => void {
  return useContext(StepDraftContext)?.clear ?? NO_DRAFTS_TO_CLEAR;
}

/**
 * A step's draft fields, with the initial values it would otherwise have put in
 * useState.
 *
 * `stepKey` is the step's own key, handed down by the shell. A step rendered
 * outside the wizard (a unit test, say) gets no provider and simply keeps its
 * state locally, so nothing has to know whether it is inside one.
 */
export function useStepDraft<T extends Record<string, unknown>>(
  stepKey: string | undefined,
  initial: T,
): [T, (values: Partial<T>) => void] {
  const context = useContext(StepDraftContext);
  const [local, setLocal] = useState<Partial<T>>({});

  const stored = (
    context !== null && stepKey !== undefined ? context.drafts[stepKey] : local
  ) as Partial<T> | undefined;

  // Merged on every render rather than memoised on `stored`: a step's initial
  // values can arrive late (the General step reads them from the settings
  // query), and only the fields the reader actually touched are stored, so an
  // untouched field has to keep following its source.
  const value = { ...initial, ...stored } as T;

  const patch = context?.patch;
  const set = useCallback(
    (values: Partial<T>) => {
      if (patch !== undefined && stepKey !== undefined) {
        patch(stepKey, values);
        return;
      }
      setLocal((current) => ({ ...current, ...values }));
    },
    [patch, stepKey],
  );

  return [value, set];
}
