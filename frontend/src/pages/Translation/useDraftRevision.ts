import { useRef, useState } from "react";

/**
 * The revision a form's draft was loaded from. The draft keeps its starting
 * values, so a save has to carry the revision those values came from: if
 * another tab saved since, the server refuses it and the user sees a conflict
 * instead of silently overwriting that save with an old draft.
 *
 * After a refused save the user has seen the conflict, and saving again is
 * their choice to overwrite: that save carries the newest revision. A saved
 * draft takes the saved revision, together with its values.
 */
export function useDraftRevision(latest: number) {
  const [draft, setDraft] = useState(latest);
  const [overwriting, setOverwriting] = useState(false);
  // A revision read from a state that was just loaded, for the save made right after it. It is a ref
  // because that save runs before the page has drawn the loaded state.
  const adopted = useRef<number | null>(null);

  return {
    /** The revision the next save carries. */
    forSave: () => adopted.current ?? (overwriting ? latest : draft),
    /** A save was refused because the row moved on; the next one is an accepted overwrite. */
    conflicted: () => setOverwriting(true),
    /** The user chose to overwrite what is saved at this revision, which was loaded just now. */
    adopt: (revision: number) => {
      adopted.current = revision;
    },
    /** The draft was replaced by the values of this revision. */
    reload: (revision: number) => {
      adopted.current = null;
      setDraft(revision);
      setOverwriting(false);
    },
  };
}
