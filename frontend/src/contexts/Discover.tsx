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
import { Link } from "react-router";
import { Anchor, Group, Text } from "@mantine/core";
import {
  useDiscoverDownload,
  useDiscoverPreview,
  useDiscoverSearch,
} from "@/apis/hooks/discover";
import { DiscoverPreviewModal } from "@/pages/Discover/SubtitlePreview";
import type {
  DiscoverContext as SearchContext,
  DiscoverDownloadFeedback,
  DiscoverPreviewFeedback,
  DiscoverSelection,
  DiscoverSubtitleResult,
} from "@/types/discover";
import { filenameFromContentDisposition, saveBlobAs } from "@/utilities/files";
import {
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
  updateBrowsing: (changes: Partial<DiscoverBrowsing>) => void;
  updateDraft: (changes: Partial<DiscoverDraft>) => void;
  findSubtitles: (refresh?: boolean) => Promise<void>;
  downloadSubtitle: (row: DiscoverSubtitleResult) => Promise<void>;
  previewSubtitle: (row: DiscoverSubtitleResult) => Promise<void>;
  closePreview: () => void;
  searchAgain: () => Promise<void>;
}

const DiscoverContext = createContext<DiscoverContextValue | null>(null);

export function DiscoverProvider({ children }: PropsWithChildren) {
  const [state, dispatch] = useReducer(
    discoverReducer,
    undefined,
    initialDiscoverState,
  );
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
      if (!event.detail.authenticated) {
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

  const updateDraft = useCallback(
    (changes: Partial<DiscoverDraft>) => {
      const next = { ...draft.current, ...changes };
      if (discoverContextKey(next) !== discoverContextKey(draft.current)) {
        generation.current += 1;
        downloadSequence.current += 1;
        previewSequence.current += 1;
      }
      draft.current = next;
      let storageAvailable = state.storageAvailable;
      if (changes.language !== undefined) {
        try {
          localStorage.setItem(DISCOVER_LANGUAGE_KEY, changes.language);
          storageAvailable = true;
        } catch {
          storageAvailable = false;
        }
      }
      dispatch({
        type: "draft",
        draft: next,
        generation: generation.current,
        storageAvailable,
      });
    },
    [state.storageAvailable],
  );

  const runSearch = useCallback(
    async (refresh = false, captured?: SearchContext) => {
      if (!captured && recentEpisodeMismatch(currentState.current)) return;
      const context = captured ?? searchSelection(draft.current);
      if (!context) return;
      const key = discoverContextKey(draft.current);
      const attempt = ++generation.current;
      dispatch({ type: "start", generation: attempt });
      try {
        // matching_mode is a server-owned property, never an input override.
        const selection: DiscoverSelection & { matching_mode?: string } = {
          ...context,
        };
        delete selection.matching_mode;
        const snapshot = await mutateAsync({ context: selection, refresh });
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
            response?.status === 400 &&
            typeof response.data?.message === "string"
              ? response.data.message
              : undefined,
          generation: attempt,
          key,
          now: Date.now(),
        });
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
      currentState.current.preview?.context ??
      currentState.current.download?.context;
    if (captured) await runSearch(true, captured);
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
      updateBrowsing,
      updateDraft,
      findSubtitles,
      downloadSubtitle,
      previewSubtitle,
      closePreview,
      searchAgain,
    }),
    [
      state,
      updateBrowsing,
      updateDraft,
      findSubtitles,
      downloadSubtitle,
      previewSubtitle,
      closePreview,
      searchAgain,
    ],
  );
  return (
    <DiscoverContext.Provider value={value}>
      {children}
      <DiscoverPreviewModal
        preview={state.preview}
        download={state.download}
        searching={state.status === "searching"}
        close={closePreview}
        retry={previewSubtitle}
        downloadSubtitle={downloadSubtitle}
        searchAgain={searchAgain}
      />
    </DiscoverContext.Provider>
  );
}

export function useDiscover() {
  const value = useContext(DiscoverContext);
  if (!value) throw new Error("DiscoverProvider is required");
  return value;
}

export function DiscoverSetupReturn({ children }: PropsWithChildren) {
  const { state } = useDiscover();
  return (
    <>
      {(state.draft.imdbId ||
        state.draft.query ||
        state.browsing.query ||
        state.browsing.returnTarget !== "/discover") && (
        <Group mb="md" justify="space-between">
          <Text size="sm">Your Discover selection is saved.</Text>
          <Anchor
            c="light-dark(var(--mantine-color-brand-7), var(--mantine-color-brand-4))"
            component={Link}
            to={
              /^\/discover(?:[/?#]|$)/.test(state.browsing.returnTarget)
                ? state.browsing.returnTarget
                : "/discover"
            }
            py="sm"
          >
            Return to Discover
          </Anchor>
        </Group>
      )}
      {children}
    </>
  );
}
