import {
  createContext,
  PropsWithChildren,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useReducer,
  useRef,
} from "react";
import {
  useDiscoverDownload,
  useDiscoverPreview,
  useDiscoverSearch,
} from "@/apis/hooks/discover";
import api from "@/apis/raw";
import type {
  DiscoverContext as SearchContext,
  DiscoverDownloadFeedback,
  DiscoverPreviewFeedback,
  DiscoverSelection,
  DiscoverSubtitleResult,
} from "@/types/discover";
import { writeStoredValue } from "@/utilities/browserStorage";
import { filenameFromContentDisposition, saveBlobAs } from "@/utilities/files";
import {
  copyTargetKey,
  DISCOVER_LANGUAGE_KEY,
  DiscoverBrowsing,
  discoverContextKey,
  DiscoverDraft,
  discoverReducer,
  DiscoverState,
  initialDiscoverState,
  recentEpisodeMismatch,
  searchSelection,
} from "./discoverState";

interface DiscoverContextValue {
  state: DiscoverState;
  rememberPage: (key: string, page: DiscoverState) => void;
  restorePage: (key: string) => boolean;
  updateBrowsing: (changes: Partial<DiscoverBrowsing>) => void;
  updateDraft: (changes: Partial<DiscoverDraft>) => void;
  /**
   * Preselect a subtitle language from the reader's language profile. Only an
   * empty language is ever seeded, nothing is written to browser storage, and
   * the state says it was seeded so the page can show where it came from.
   */
  seedLanguage: (language: string) => void;
  findSubtitles: (refresh?: boolean) => Promise<void>;
  downloadSubtitle: (row: DiscoverSubtitleResult) => Promise<void>;
  previewSubtitle: (row: DiscoverSubtitleResult) => Promise<void>;
  closePreview: () => void;
  /**
   * Retire in-flight search, download and preview responses without touching
   * filed results. Leaving the title is the owner going away: a response that
   * lands after it belongs to a selection the reader has already left.
   */
  cancelPending: () => void;
  searchAgain: () => Promise<void>;
}

const DiscoverContext = createContext<DiscoverContextValue | null>(null);

export function DiscoverProvider({ children }: PropsWithChildren) {
  const [state, dispatch] = useReducer(
    discoverReducer,
    undefined,
    initialDiscoverState,
  );
  const pages = useRef(
    new Map<string, { savedAt: number; state: DiscoverState }>(),
  );
  const searches = useRef(
    new Map<string, { savedAt: number; state: DiscoverState }>(),
  );
  const authenticated = useRef(true);
  const generation = useRef(0);
  const draft = useRef(state.draft);
  const currentState = useRef(state);
  currentState.current = state;
  const downloadSequence = useRef(0);
  const previewSequence = useRef(0);
  const { mutateAsync: fetchPreview, reset: resetPreview } =
    useDiscoverPreview();
  const { mutateAsync: fetchDownload, reset: resetDownload } =
    useDiscoverDownload();
  const mutation = useDiscoverSearch();
  const { mutateAsync, reset } = mutation;

  useEffect(() => {
    const onAuth = (event: WindowEventMap["app-auth-changed"]) => {
      authenticated.current = event.detail.authenticated;
      if (!event.detail.authenticated) {
        pages.current.clear();
        searches.current.clear();
        generation.current += 1;
        downloadSequence.current += 1;
        previewSequence.current += 1;
        draft.current = initialDiscoverState().draft;
        dispatch({ type: "clear", generation: generation.current });
        reset();
        resetDownload();
        resetPreview();
      }
    };
    window.addEventListener("app-auth-changed", onAuth);
    return () => window.removeEventListener("app-auth-changed", onAuth);
  }, [reset, resetDownload, resetPreview]);

  const rememberPage = useCallback((key: string, page: DiscoverState) => {
    if (
      !authenticated.current ||
      page.sessionId !== currentState.current.sessionId
    )
      return;
    // Keep only this browser session's recent history, never subtitle handles in storage.
    const now = Date.now();
    for (const [id, entry] of pages.current)
      if (now - entry.savedAt > 30 * 60_000) pages.current.delete(id);
    pages.current.delete(key);
    pages.current.set(key, { savedAt: now, state: page });
    while (pages.current.size > 20)
      pages.current.delete(pages.current.keys().next().value!);
  }, []);

  const restorePage = useCallback((key: string) => {
    const entry = pages.current.get(key);
    if (
      !authenticated.current ||
      !entry ||
      Date.now() - entry.savedAt > 30 * 60_000
    )
      return false;
    generation.current += 1;
    downloadSequence.current += 1;
    previewSequence.current += 1;
    draft.current = entry.state.draft;
    dispatch({
      type: "restore",
      state: entry.state,
      generation: generation.current,
    });
    return true;
  }, []);

  const updateDraft = useCallback(
    (changes: Partial<DiscoverDraft>) => {
      const merged = { ...draft.current, ...changes };
      // A copy belongs to one exact target. Retiring it here, rather than in
      // every caller, is what keeps a stale choice from surviving a change of
      // film or episode. The comparison is unconditional: a caller that
      // changes the target and supplies a copy in the same update is supplying
      // a copy for a target that no longer exists, so the copy loses.
      const next =
        copyTargetKey(merged) === copyTargetKey(draft.current)
          ? merged
          : { ...merged, copyId: undefined };
      const nextKey = discoverContextKey(next);
      const changed = nextKey !== discoverContextKey(draft.current);
      if (changed) {
        const current = currentState.current;
        if (
          authenticated.current &&
          current.sessionId === state.sessionId &&
          current.snapshot &&
          discoverContextKey(current.draft) ===
            discoverContextKey(draft.current)
        ) {
          const oldKey = discoverContextKey(current.draft);
          searches.current.delete(oldKey);
          searches.current.set(oldKey, { savedAt: Date.now(), state: current });
          while (searches.current.size > 20)
            searches.current.delete(searches.current.keys().next().value!);
        }
        generation.current += 1;
        downloadSequence.current += 1;
        previewSequence.current += 1;
      }
      draft.current = next;
      let storageAvailable = state.storageAvailable;
      if (changes.language !== undefined) {
        // Whether the write landed is what decides the session-only notice
        // under the language select, so the helper's answer is the answer.
        storageAvailable = writeStoredValue(
          DISCOVER_LANGUAGE_KEY,
          changes.language,
        );
      }
      dispatch({
        type: "draft",
        draft: next,
        generation: generation.current,
        storageAvailable,
        cached:
          changed &&
          authenticated.current &&
          searches.current.get(nextKey)?.state.sessionId ===
            currentState.current.sessionId &&
          searches.current.has(nextKey) &&
          Date.now() - searches.current.get(nextKey)!.savedAt <= 30 * 60_000
            ? searches.current.get(nextKey)!.state
            : undefined,
        // A language the reader set by hand is no longer a seeded one.
        ...(changes.language !== undefined ? { languageSeeded: false } : {}),
      });
    },
    [state.storageAvailable, state.sessionId],
  );

  const seedLanguage = useCallback((language: string) => {
    if (!language || draft.current.language) return;
    draft.current = { ...draft.current, language };
    dispatch({
      type: "draft",
      draft: draft.current,
      generation: generation.current,
      storageAvailable: currentState.current.storageAvailable,
      languageSeeded: true,
    });
  }, []);

  const runSearch = useCallback(
    async (
      refresh = false,
      captured?: { context: SearchContext; key: string },
    ) => {
      if (!captured && recentEpisodeMismatch(currentState.current)) return;
      // A recovery search reuses the context it captured, so the key it files
      // results under travels with that context instead of being recomputed
      // from the draft. If the two have drifted apart the results would belong
      // to a context the reader has already left, so nothing is filed at all.
      const key = captured?.key ?? discoverContextKey(draft.current);
      if (key !== discoverContextKey(draft.current)) return;
      const context = captured?.context ?? searchSelection(draft.current);
      if (!context) return;
      const attempt = ++generation.current;
      // A seeded language becomes the remembered one the first time it is
      // actually used for a search; a chosen language was written when chosen.
      // Either way the helper's answer is what the storage notice is made of,
      // so it is carried into the dispatch rather than dropped: a reader whose
      // storage refuses the seeded language must be told it is session-only,
      // exactly as one who chose it by hand is.
      const stored = currentState.current.languageSeeded
        ? writeStoredValue(DISCOVER_LANGUAGE_KEY, context.language)
        : undefined;
      dispatch({
        type: "start",
        generation: attempt,
        storageAvailable: stored,
      });
      // getRandomValues also works on plain HTTP LAN installations.
      const bytes = crypto.getRandomValues(new Uint8Array(16));
      const hex = Array.from(bytes, (byte) =>
        byte.toString(16).padStart(2, "0"),
      ).join("");
      const progressId = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
      const controller = new AbortController();
      let pollTimer: ReturnType<typeof setTimeout> | undefined;
      const poll = async () => {
        if (controller.signal.aborted || generation.current !== attempt) return;
        try {
          const progress = await api.discover.searchProgress(
            progressId,
            controller.signal,
          );
          dispatch({ type: "progress", generation: attempt, progress });
        } catch {
          // Losing observations does not cancel the search or discard results.
        }
        if (!controller.signal.aborted && generation.current === attempt)
          pollTimer = setTimeout(() => void poll(), 600);
      };
      pollTimer = setTimeout(() => void poll(), 200);
      try {
        // matching_mode, the resolved copy and its physical revision are all
        // server-owned. Only the opaque copy identity is ever an input.
        const selection: DiscoverSelection & {
          matching_mode?: string;
          copy?: unknown;
          file_revision?: string;
        } = { ...context };
        delete selection.matching_mode;
        delete selection.copy;
        delete selection.file_revision;
        const snapshot = await mutateAsync({
          context: selection,
          refresh,
          progressId,
        });
        dispatch({ type: "success", generation: attempt, key, snapshot });
      } catch (error) {
        const response = (
          error as {
            response?: { status?: number; data?: { message?: unknown } };
          }
        ).response;
        dispatch({
          type: "failure",
          message:
            // 409 is a chosen copy that no longer resolves. Its message names
            // the recovery, and the choice is never replaced automatically.
            (response?.status === 400 || response?.status === 409) &&
            typeof response.data?.message === "string"
              ? response.data.message
              : undefined,
          generation: attempt,
          key,
          now: Date.now(),
        });
      } finally {
        controller.abort();
        clearTimeout(pollTimer);
      }
    },
    [mutateAsync],
  );

  const findSubtitles = useCallback(
    (refresh = false) => runSearch(refresh),
    [runSearch],
  );
  const searchAgain = useCallback(async () => {
    const captured =
      currentState.current.preview ?? currentState.current.download;
    // Fails closed: feedback without its own captured key is never replayed.
    if (captured?.contextKey)
      await runSearch(true, {
        context: captured.context,
        key: captured.contextKey,
      });
  }, [runSearch]);

  const downloadSubtitle = useCallback(
    async (row: DiscoverSubtitleResult) => {
      const owner = currentState.current;
      const snapshot = owner.snapshot;
      if (
        !snapshot?.results.some(
          (candidate) =>
            candidate.id === row.id && candidate.search_id === row.search_id,
        ) ||
        owner.retiredResultIds.includes(row.id) ||
        owner.download?.status === "pending"
      )
        return;
      const key = discoverContextKey(draft.current);
      const requestId = ++downloadSequence.current;
      const feedback: DiscoverDownloadFeedback = {
        requestId,
        contextKey: key,
        context: { ...snapshot.context },
        row: { ...row },
        status: "pending",
      };
      dispatch({ type: "download", key, feedback });
      const stillCurrent = () =>
        requestId === downloadSequence.current &&
        key === discoverContextKey(draft.current) &&
        currentState.current.snapshot?.results.some(
          (candidate) =>
            candidate.id === row.id && candidate.search_id === row.search_id,
        ) &&
        !currentState.current.retiredResultIds.includes(row.id);
      try {
        if (Date.parse(row.expires_at) <= Date.now()) {
          dispatch({
            type: "download",
            key,
            feedback: { ...feedback, status: "expired" },
          });
          return;
        }
        const response = await fetchDownload({
          resultId: row.id,
          searchId: row.search_id,
        });
        if (!stillCurrent()) return;
        if (
          !response.data.size ||
          !response.data.type.includes("application/x-subrip")
        )
          throw new Error("Invalid subtitle response");
        const filename = filenameFromContentDisposition(
          response.headers["content-disposition"],
          "subtitle.srt",
        );
        saveBlobAs(response.data, filename);
        dispatch({
          type: "download",
          key,
          feedback: { ...feedback, status: "started", filename },
        });
      } catch (error) {
        if (!stillCurrent()) return;
        const status = (error as { response?: { status?: number } }).response
          ?.status;
        dispatch({
          type: "download",
          key,
          feedback: {
            ...feedback,
            status: status === 410 ? "expired" : "failed",
          },
        });
      }
    },
    [fetchDownload],
  );

  const closePreview = useCallback(() => {
    previewSequence.current += 1;
    dispatch({ type: "close-preview" });
    resetPreview();
  }, [resetPreview]);

  const cancelPending = useCallback(() => {
    generation.current += 1;
    downloadSequence.current += 1;
    previewSequence.current += 1;
    dispatch({ type: "cancel", generation: generation.current });
  }, []);
  const previewSubtitle = useCallback(
    async (row: DiscoverSubtitleResult) => {
      const owner = currentState.current;
      const snapshot = owner.snapshot;
      if (
        !snapshot?.results.some(
          (candidate) =>
            candidate.id === row.id && candidate.search_id === row.search_id,
        ) ||
        owner.retiredResultIds.includes(row.id)
      )
        return;
      const key = discoverContextKey(draft.current);
      const requestId = ++previewSequence.current;
      const feedback: DiscoverPreviewFeedback = {
        requestId,
        contextKey: key,
        context: { ...snapshot.context },
        row: { ...row },
        status: "pending",
      };
      dispatch({ type: "preview", key, feedback });
      const stillCurrent = () =>
        requestId === previewSequence.current &&
        key === discoverContextKey(draft.current) &&
        currentState.current.snapshot?.results.some(
          (candidate) =>
            candidate.id === row.id && candidate.search_id === row.search_id,
        ) &&
        !currentState.current.retiredResultIds.includes(row.id);
      try {
        if (Date.parse(row.expires_at) <= Date.now()) {
          dispatch({
            type: "preview",
            key,
            feedback: { ...feedback, status: "expired" },
          });
          return;
        }
        const data = await fetchPreview({
          resultId: row.id,
          searchId: row.search_id,
        });
        if (!stillCurrent()) return;
        if (
          data.result_id !== row.id ||
          data.search_id !== row.search_id ||
          !Array.isArray(data.cues) ||
          !data.cues.length ||
          data.cues.length > 40 ||
          data.cues.some(
            (cue) =>
              typeof cue.text !== "string" ||
              !Number.isFinite(cue.start_ms) ||
              !Number.isFinite(cue.end_ms) ||
              cue.start_ms < 0 ||
              cue.end_ms <= cue.start_ms,
          ) ||
          data.cues.reduce(
            (length, cue) => length + Array.from(cue.text).length,
            0,
          ) > 24000
        )
          throw new Error("Invalid subtitle preview response");
        dispatch({
          type: "preview",
          key,
          feedback: { ...feedback, status: "ready", data },
        });
      } catch (error) {
        if (!stillCurrent()) return;
        const status = (error as { response?: { status?: number } }).response
          ?.status;
        dispatch({
          type: "preview",
          key,
          feedback: {
            ...feedback,
            status: status === 410 ? "expired" : "failed",
          },
        });
      }
    },
    [fetchPreview],
  );

  const updateBrowsing = useCallback(
    (changes: Partial<DiscoverBrowsing>) =>
      dispatch({ type: "browsing", changes }),
    [],
  );

  const value = useMemo(
    () => ({
      state,
      rememberPage,
      restorePage,
      updateBrowsing,
      updateDraft,
      seedLanguage,
      findSubtitles,
      downloadSubtitle,
      previewSubtitle,
      closePreview,
      cancelPending,
      searchAgain,
    }),
    [
      state,
      rememberPage,
      restorePage,
      updateBrowsing,
      updateDraft,
      seedLanguage,
      findSubtitles,
      downloadSubtitle,
      previewSubtitle,
      closePreview,
      cancelPending,
      searchAgain,
    ],
  );
  return (
    <DiscoverContext.Provider value={value}>
      {children}
    </DiscoverContext.Provider>
  );
}

export function useDiscover() {
  const value = useContext(DiscoverContext);
  if (!value) throw new Error("DiscoverProvider is required");
  return value;
}

export function DiscoverSetupReturn({ children }: PropsWithChildren) {
  return <>{children}</>;
}
