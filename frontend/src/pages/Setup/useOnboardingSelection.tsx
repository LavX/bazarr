import {
  createContext,
  FunctionComponent,
  PropsWithChildren,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type {
  MediaServerKind,
  MediaServerOptions,
  PathMapping,
} from "@/apis/raw/mediaServers";
// Types only from apis/raw/mediaServers, and the kind list from the leaf
// module beside it. apis/raw/client pulls the socket layer, which pulls
// apis/raw back, so a module that reaches apis/raw/* for a value before
// anything has loaded apis/raw itself lands in the middle of that cycle and
// gets an undefined BaseApi. This provider is imported first by the step
// registry, which makes it exactly that module.
import { KIND_NAMES, kindName } from "@/pages/Settings/MediaServers/kinds";
import {
  readOnboardingValue,
  removeOnboardingValue,
  writeOnboardingValue,
} from "./onboardingStorage";

const STORAGE_NAME = "media-servers";

/**
 * One media server the reader ticked, before it is a row in the database.
 *
 * The draft id is minted here rather than taken from the server, because the
 * wizard has to name a step, a form and a save outcome for a server that does
 * not exist yet, and has to keep telling them apart when two of them are the
 * same kind. `instanceId` is what the create came back with, and is the one
 * piece that says the draft is no longer a draft.
 */
export interface MediaServerDraft {
  draftId: string;
  kind: MediaServerKind;
  name: string;
  url: string;
  apiKey: string;
  verifySsl: boolean;
  pathMappings: PathMapping[];
  options: MediaServerOptions;
  instanceId?: string;
  // The row this draft has already written while its step is still on screen.
  // Recorded the moment the create lands, before the master switch is written,
  // because pressing Back and walking forward again during a save remounts the
  // form with no memory of the request still in flight: without this, the next
  // press of Continue wrote the same server a second time. It is what says the
  // row exists, whatever else has or has not happened to it.
  savedInstanceId?: string;
  // The row landed but its kind's master switch did not, so nothing refreshes
  // yet. The step stays on screen with a message rather than advancing, and
  // the draft stays a draft until the reader has read it.
  switchFailed?: boolean;
  // A create for this draft is in the air. Held here rather than in the form,
  // because pressing Back and coming straight back remounts the form with no
  // memory of it, and the draft has nothing to show for the request until it
  // answers: the second press wrote the same server again. Deliberately not
  // persisted, so a reload, which loses the request anyway, clears it.
  submitting?: boolean;
}

/** What the step builder needs. Nothing that changes while a field is typed. */
export interface MediaServerDraftSummary {
  draftId: string;
  kind: MediaServerKind;
  instanceId?: string;
}

interface OnboardingSelectionValue {
  drafts: MediaServerDraft[];
  addDraft: (kind: MediaServerKind, takenNames?: string[]) => MediaServerDraft;
  removeDraft: (draftId: string) => void;
  removeKind: (kind: MediaServerKind) => void;
  updateDraft: (draftId: string, patch: Partial<MediaServerDraft>) => void;
  markSaved: (draftId: string, instanceId: string) => void;
  clearSelection: () => void;
}

function newDraftId(): string {
  // crypto.randomUUID is not there in every test runner or on a page served
  // over plain HTTP in older browsers, and a draft id only has to be unique
  // within one wizard run.
  const api = globalThis.crypto;
  if (api && typeof api.randomUUID === "function") {
    return api.randomUUID();
  }
  return `draft-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/**
 * The first free name for a kind. Two Embys must not both be called "Emby":
 * the name is what tells them apart everywhere the reader meets them later,
 * and the Connections list sorts by it.
 */
export function nextDraftName(kind: MediaServerKind, taken: string[]): string {
  const base = kindName(kind);
  const used = new Set(taken.map((name) => name.trim().toLowerCase()));
  if (!used.has(base.toLowerCase())) {
    return base;
  }
  for (let suffix = 2; suffix < 100; suffix += 1) {
    const candidate = `${base} ${suffix}`;
    if (!used.has(candidate.toLowerCase())) {
      return candidate;
    }
  }
  return `${base} ${newDraftId().slice(0, 4)}`;
}

export function createDraft(
  kind: MediaServerKind,
  taken: string[] = [],
): MediaServerDraft {
  return {
    draftId: newDraftId(),
    kind,
    name: nextDraftName(kind, taken),
    url: "",
    apiKey: "",
    verifySsl: true,
    // The form seeds the empty row the kinds that resolve by path need. It is
    // not state until the reader types in it.
    pathMappings: [],
    options: {},
  };
}

// The credential never goes to localStorage. Everything else does, so going
// Back to the picker and forward again, or reloading through the providers
// restart, does not empty a form the reader already filled in.
type StoredDraft = Omit<MediaServerDraft, "apiKey" | "submitting">;

function isKind(value: unknown): value is MediaServerKind {
  return typeof value === "string" && value in KIND_NAMES;
}

function readPersistedDrafts(): MediaServerDraft[] {
  const raw = readOnboardingValue(STORAGE_NAME);
  if (!raw) {
    return [];
  }
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      return [];
    }
    return parsed.flatMap((entry): MediaServerDraft[] => {
      const row = entry as Partial<StoredDraft>;
      if (typeof row?.draftId !== "string" || !isKind(row.kind)) {
        return [];
      }
      return [
        {
          draftId: row.draftId,
          kind: row.kind,
          name: typeof row.name === "string" ? row.name : kindName(row.kind),
          url: typeof row.url === "string" ? row.url : "",
          apiKey: "",
          verifySsl: row.verifySsl !== false,
          pathMappings: Array.isArray(row.pathMappings) ? row.pathMappings : [],
          options:
            row.options && typeof row.options === "object" ? row.options : {},
          ...(typeof row.instanceId === "string"
            ? { instanceId: row.instanceId }
            : {}),
          ...(typeof row.savedInstanceId === "string"
            ? { savedInstanceId: row.savedInstanceId }
            : {}),
          ...(row.switchFailed === true ? { switchFailed: true } : {}),
        },
      ];
    });
  } catch {
    return [];
  }
}

function persistDrafts(drafts: MediaServerDraft[]) {
  if (drafts.length === 0) {
    removeOnboardingValue(STORAGE_NAME);
    return;
  }
  const stored: StoredDraft[] = drafts.map(
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    ({ apiKey, submitting, ...rest }) => rest,
  );
  writeOnboardingValue(STORAGE_NAME, JSON.stringify(stored));
}

const OnboardingSelectionContext =
  createContext<OnboardingSelectionValue | null>(null);

/**
 * Which media servers the reader ticked, held above the picker step.
 *
 * It cannot live in the picker: the picker is exactly the screen a reader
 * reaches by pressing Back out of the segment it generates, and step-local
 * state is gone by the time they get there. It is mirrored to localStorage for
 * the same reason the intent is, because the providers step restarts Bazarr+
 * and the wizard resumes through a hard redirect.
 */
export const OnboardingSelectionProvider: FunctionComponent<
  PropsWithChildren
> = ({ children }) => {
  const [drafts, setDrafts] = useState<MediaServerDraft[]>(readPersistedDrafts);
  const latest = useRef(drafts);
  latest.current = drafts;

  // Mirrored from an effect, not from inside the state updater: the updater has
  // to stay pure or StrictMode's double invocation writes twice.
  useEffect(() => persistDrafts(drafts), [drafts]);

  const commit = useCallback(
    (update: (current: MediaServerDraft[]) => MediaServerDraft[]) => {
      setDrafts(update);
    },
    [],
  );

  const addDraft = useCallback(
    (kind: MediaServerKind, takenNames: string[] = []) => {
      const created = createDraft(kind, [
        ...takenNames,
        ...latest.current.map((draft) => draft.name),
      ]);
      commit((current) => [...current, created]);
      return created;
    },
    [commit],
  );

  const removeDraft = useCallback(
    (draftId: string) => {
      commit((current) => current.filter((draft) => draft.draftId !== draftId));
    },
    [commit],
  );

  const removeKind = useCallback(
    (kind: MediaServerKind) => {
      commit((current) =>
        current.filter(
          (draft) => draft.kind !== kind || draft.instanceId !== undefined,
        ),
      );
    },
    [commit],
  );

  const updateDraft = useCallback(
    (draftId: string, patch: Partial<MediaServerDraft>) => {
      commit((current) =>
        current.map((draft) =>
          draft.draftId === draftId ? { ...draft, ...patch } : draft,
        ),
      );
    },
    [commit],
  );

  const markSaved = useCallback(
    (draftId: string, instanceId: string) => {
      commit((current) =>
        current.map((draft) =>
          draft.draftId === draftId
            ? { ...draft, instanceId, apiKey: "" }
            : draft,
        ),
      );
    },
    [commit],
  );

  const clearSelection = useCallback(() => {
    setDrafts([]);
  }, []);

  const value = useMemo(
    () => ({
      drafts,
      addDraft,
      removeDraft,
      removeKind,
      updateDraft,
      markSaved,
      clearSelection,
    }),
    [
      drafts,
      addDraft,
      removeDraft,
      removeKind,
      updateDraft,
      markSaved,
      clearSelection,
    ],
  );

  return (
    <OnboardingSelectionContext.Provider value={value}>
      {children}
    </OnboardingSelectionContext.Provider>
  );
};

/**
 * Reads the media server selection. Outside the provider (a step mounted on
 * its own in a test) it reports an empty selection and treats writes as
 * no-ops, so no step has to guard for a missing provider.
 */
export function useOnboardingSelection(): OnboardingSelectionValue {
  const value = useContext(OnboardingSelectionContext);
  const fallback = useMemo<OnboardingSelectionValue>(
    () => ({
      drafts: [],
      addDraft: (kind) => createDraft(kind),
      removeDraft: () => undefined,
      removeKind: () => undefined,
      updateDraft: () => undefined,
      markSaved: () => undefined,
      clearSelection: () => undefined,
    }),
    [],
  );
  return value ?? fallback;
}

export function clearPersistedSelection() {
  removeOnboardingValue(STORAGE_NAME);
}
