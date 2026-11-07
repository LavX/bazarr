import { useState } from "react";

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

  return {
    /** The revision the next save carries. */
    forSave: () => (overwriting ? latest : draft),
    /** A save was refused because the row moved on; the next one is an accepted overwrite. */
    conflicted: () => setOverwriting(true),
    /** The draft was replaced by the values of this revision. */
    reload: (revision: number) => {
      setDraft(revision);
      setOverwriting(false);
    },
  };
}
