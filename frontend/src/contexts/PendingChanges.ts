import { createContext, useContext } from "react";

/**
 * What the settings page is holding that leaving would have to ask about:
 * `true` for staged edits, `"refresh"` for a save kept after its application
 * refresh failed with nothing staged on top of it, `false` for neither.
 */
type PendingChanges = boolean | "refresh";

const PendingChangesContext = createContext<PendingChanges>(false);

/**
 * Whether the settings page holds changes that leaving would have to ask
 * about: staged edits, or a save kept after its refresh failed. The page's
 * unsaved-changes prompt answers every navigation while this is true, so an
 * action that writes something and then navigates cannot tell whether the
 * navigation will happen, and has to settle the question before it writes.
 */
export function useHasPendingChanges() {
  return useContext(PendingChangesContext) !== false;
}

/**
 * True when the only thing pending is a failed application refresh, which the
 * reader clears by retrying it rather than by saving or discarding edits that
 * do not exist.
 */
export function useOnlyRefreshPending() {
  return useContext(PendingChangesContext) === "refresh";
}

export default PendingChangesContext.Provider;
