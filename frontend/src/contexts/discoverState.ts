import type {
  DigitalReleaseContext,
  DiscoverDownloadFeedback,
  DiscoverPreviewFeedback,
  DiscoverSearchSnapshot,
  DiscoverSelection,
  MetadataEpisode,
  MetadataShow,
  MetadataSource,
  MetadataTitle,
  RecentEpisodeContext,
  TrendingMediaType,
} from "@/types/discover";

export const DISCOVER_LANGUAGE_KEY = "bazarr.discover.subtitle-language";

export interface DiscoverDraft {
  mode?: "title" | "release";
  query?: string;
  mediaType: "movie" | "episode";
  imdbId: string;
  language: string;
  season: string;
  episode: string;
  title?: string;
  year?: number;
  showId?: number;
  showTvdbId?: number | null;
  episodeIdentity?: MetadataEpisode;
  manualConfirmed?: boolean;
  manualEntry?: boolean;
}

export interface DiscoverBrowsing {
  recentContext: RecentEpisodeContext | null;
  digitalRegion: string;
  releaseContext: DigitalReleaseContext | null;
  pagePosition: { target: string; focusId: string; scrollY: number } | null;
  trendingFilter: TrendingMediaType;
  query: string;
  mediaFilter: "movie" | "show";
  selectedType: "movie" | "show";
  selectedSeason: number | null;
  selectedEpisode: number | null;
  adoptedSourceId: string | null;
  retainedTitle: MetadataTitle | null;
  selectedId: number | string | null;
  selectedSource: MetadataSource;
  identityLoaded: boolean;
  adoptedMovieId: number | string | null;
  suggestionsClosed: boolean;
  returnTarget: string;
  focusId: string;
  scrollY: number;
}

export interface DiscoverState {
  browsing: DiscoverBrowsing;
  draft: DiscoverDraft;
  generation: number;
  status: "unsearched" | "searching" | "complete" | "partial" | "failed";
  snapshot: DiscoverSearchSnapshot | null;
  error: string | null;
  storageAvailable: boolean;
  download: DiscoverDownloadFeedback | null;
  preview: DiscoverPreviewFeedback | null;
  retiredResultIds: string[];
}

export function discoverPageKey(
  state: Pick<DiscoverState, "browsing" | "draft">,
): string {
  return JSON.stringify([
    state.draft.mode ?? "title",
    state.browsing.selectedSource,
    state.browsing.selectedType,
    state.browsing.selectedId,
  ]);
}

export function initialDiscoverState(): DiscoverState {
  let language = "";
  let storageAvailable = true;
  try {
    language = localStorage.getItem(DISCOVER_LANGUAGE_KEY) ?? "";
  } catch {
    storageAvailable = false;
  }
  return {
    browsing: {
      recentContext: null,
      digitalRegion: "US",
      releaseContext: null,
      trendingFilter: "all",
      pagePosition: null,
      query: "",
      mediaFilter: "movie",
      selectedType: "movie",
      selectedSource: "tmdb",
      selectedSeason: null,
      selectedEpisode: null,
      adoptedSourceId: null,
      retainedTitle: null,
      selectedId: null,
      identityLoaded: false,
      adoptedMovieId: null,
      suggestionsClosed: false,
      returnTarget: "/discover",
      focusId: "discover-title-query",
      scrollY: 0,
    },
    draft: {
      mode: "title",
      query: "",
      mediaType: "movie",
      imdbId: "",
      language,
      season: "",
      episode: "",
    },
    generation: 0,
    status: "unsearched",
    snapshot: null,
    error: null,
    storageAvailable,
    download: null,
    preview: null,
    retiredResultIds: [],
  };
}

export function searchSelection(
  draft: DiscoverDraft,
): DiscoverSelection | null {
  if (draft.mode === "release") {
    const query = (draft.query ?? "").trim();
    if (
      !draft.language ||
      query.length > 500 ||
      /[\p{Cc}]/u.test(query) ||
      !/[\p{L}\p{N}]/u.test(query)
    )
      return null;
    return { mode: "release", query, language: draft.language };
  }
  const imdbId = draft.imdbId.trim().toLowerCase();
  if (!/^tt\d{7,10}$/.test(imdbId) || !draft.language) return null;
  const selection: DiscoverSelection = {
    media_type: draft.mediaType,
    imdb_id: imdbId,
    language: draft.language,
  };
  if (draft.mediaType === "episode") {
    if (!/^\d{1,4}$/.test(draft.season) || !/^\d{1,4}$/.test(draft.episode))
      return null;
    selection.season = Number(draft.season);
    selection.episode = Number(draft.episode);
    if (selection.episode < 1) return null;
    if (draft.manualEntry && !draft.manualConfirmed) return null;
    const identity = draft.episodeIdentity;
    if (identity?.identity_status === "conflict") return null;
    if (
      identity &&
      (identity.show_imdb_id !== imdbId ||
        identity.show_title !== draft.title ||
        (identity.show_year ?? undefined) !== draft.year ||
        identity.show_id !== draft.showId ||
        identity.show_tvdb_id !== (draft.showTvdbId ?? null))
    )
      return null;
    if (
      !draft.manualConfirmed &&
      (!identity ||
        identity.identity_status !== "resolved" ||
        identity.target_season !== selection.season ||
        identity.target_episode !== selection.episode)
    )
      return null;
    if (identity) selection.episode_identity = identity;
    if (draft.showId !== undefined) selection.show_id = draft.showId;
    if (draft.manualConfirmed) selection.manual_confirmed = true;
  }
  if (draft.title) selection.title = draft.title;
  if (draft.year) selection.year = draft.year;
  return selection;
}

export function episodeIdentityKey(
  identity: MetadataEpisode | undefined,
): string | null {
  return identity
    ? JSON.stringify(
        Object.entries(identity).sort(([left], [right]) =>
          left.localeCompare(right),
        ),
      )
    : null;
}

export function episodeMatchesShow(
  episode: MetadataEpisode,
  show: MetadataShow,
): boolean {
  return (
    episode.show_id === show.id &&
    episode.show_imdb_id === show.imdb_id &&
    episode.show_tvdb_id === show.tvdb_id &&
    episode.show_title === show.title &&
    episode.show_year === show.year
  );
}

export function discoverContextKey(draft: DiscoverDraft): string {
  if (draft.mode === "release")
    return JSON.stringify([
      "release",
      (draft.query ?? "").trim(),
      draft.language,
    ]);
  return JSON.stringify([
    draft.mediaType,
    draft.imdbId.trim().toLowerCase(),
    draft.language,
    draft.mediaType === "episode" ? draft.season : null,
    draft.mediaType === "episode" ? draft.episode : null,
    draft.title ?? null,
    draft.year ?? null,
    "title",
    draft.mediaType === "episode" ? (draft.showId ?? null) : null,
    draft.mediaType === "episode" ? (draft.showTvdbId ?? null) : null,
    draft.mediaType === "episode"
      ? episodeIdentityKey(draft.episodeIdentity)
      : null,
    draft.mediaType === "episode" ? Boolean(draft.manualConfirmed) : null,
    draft.mediaType === "episode" ? Boolean(draft.manualEntry) : null,
  ]);
}

type DiscoverAction =
  | { type: "browsing"; changes: Partial<DiscoverBrowsing> }
  | {
      type: "draft";
      draft: DiscoverDraft;
      generation: number;
      storageAvailable: boolean;
    }
  | { type: "start"; generation: number }
  | {
      type: "success";
      generation: number;
      key: string;
      snapshot: DiscoverSearchSnapshot;
    }
  | {
      type: "failure";
      generation: number;
      key: string;
      now: number;
      message?: string;
    }
  | { type: "download"; key: string; feedback: DiscoverDownloadFeedback }
  | { type: "preview"; key: string; feedback: DiscoverPreviewFeedback }
  | { type: "close-preview" }
  | { type: "clear"; generation: number };

export function discoverReducer(
  state: DiscoverState,
  action: DiscoverAction,
): DiscoverState {
  if (action.type === "browsing")
    return {
      ...state,
      browsing: {
        ...state.browsing,
        ...(action.changes.selectedId != null
          ? { releaseContext: null, recentContext: null }
          : {}),
        ...((action.changes.selectedSeason !== undefined &&
          action.changes.selectedSeason !== state.browsing.selectedSeason) ||
        (action.changes.selectedEpisode !== undefined &&
          action.changes.selectedEpisode !== state.browsing.selectedEpisode)
          ? { recentContext: null }
          : {}),
        ...action.changes,
      },
    };
  if (action.type === "clear") {
    return { ...initialDiscoverState(), generation: action.generation };
  }
  if (action.type === "draft") {
    const changed =
      discoverContextKey(state.draft) !== discoverContextKey(action.draft);
    return {
      ...state,
      draft: action.draft,
      generation: action.generation,
      storageAvailable: action.storageAvailable,
      ...(changed
        ? ({
            snapshot: null,
            status: "unsearched",
            error: null,
            download: null,
            preview: null,
            retiredResultIds: [],
          } as const)
        : {}),
    };
  }
  if (action.type === "start") {
    return {
      ...state,
      generation: action.generation,
      status: "searching",
      error: null,
    };
  }
  if (action.type === "close-preview") return { ...state, preview: null };
  if (action.type === "preview") {
    const { feedback } = action;
    if (
      action.key !== discoverContextKey(state.draft) ||
      (feedback.status !== "pending" &&
        state.preview?.requestId !== feedback.requestId) ||
      !state.snapshot?.results.some(
        (row) =>
          row.id === feedback.row.id &&
          row.search_id === feedback.row.search_id,
      ) ||
      (state.retiredResultIds.includes(feedback.row.id) &&
        feedback.status !== "expired")
    )
      return state;
    return {
      ...state,
      preview: feedback,
      download:
        feedback.status === "expired" &&
        state.download?.row.id === feedback.row.id &&
        state.download.row.search_id === feedback.row.search_id
          ? { ...state.download, status: "expired" }
          : state.download,
      retiredResultIds:
        feedback.status === "expired"
          ? [...new Set([...state.retiredResultIds, feedback.row.id])]
          : state.retiredResultIds,
    };
  }
  if (action.type === "download") {
    if (action.key !== discoverContextKey(state.draft)) return state;
    const { feedback } = action;
    if (
      feedback.status !== "pending" &&
      state.download?.requestId !== feedback.requestId
    )
      return state;
    if (
      !state.snapshot?.results.some(
        (row) =>
          row.id === feedback.row.id &&
          row.search_id === feedback.row.search_id,
      )
    )
      return state;
    return {
      ...state,
      download: feedback,
      preview:
        feedback.status === "expired" &&
        state.preview?.row.id === feedback.row.id
          ? { ...state.preview, status: "expired", data: undefined }
          : state.preview,
      retiredResultIds:
        feedback.status === "expired"
          ? [...new Set([...state.retiredResultIds, feedback.row.id])]
          : state.retiredResultIds,
    };
  }
  if (
    action.generation !== state.generation ||
    action.key !== discoverContextKey(state.draft)
  )
    return state;
  if (action.type === "failure") {
    const snapshot = state.snapshot
      ? {
          ...state.snapshot,
          results: state.snapshot.results.filter(
            (row) => Date.parse(row.expires_at) > action.now,
          ),
        }
      : null;
    return {
      ...state,
      snapshot,
      status: "failed",
      download: snapshot?.results.some(
        (row) => row.id === state.download?.row.id,
      )
        ? state.download
        : null,
      preview: snapshot?.results.some(
        (row) =>
          row.id === state.preview?.row.id &&
          row.search_id === state.preview.row.search_id,
      )
        ? state.preview
        : null,
      error:
        action.message ??
        (snapshot?.results.length
          ? "Refresh failed. Previous results are still shown with their original checked time."
          : "Search failed. Check your connection and try again."),
    };
  }
  // A completed server response owns handle validity and retained rows.
  const snapshot = action.snapshot;
  return {
    ...state,
    snapshot,
    status: snapshot.status,
    error: null,
    download: snapshot.results.some((row) => row.id === state.download?.row.id)
      ? state.download
      : null,
    preview: snapshot.results.some(
      (row) =>
        row.id === state.preview?.row.id &&
        row.search_id === state.preview.row.search_id,
    )
      ? state.preview
      : null,
    retiredResultIds: state.retiredResultIds.filter((id) =>
      snapshot.results.some((row) => row.id === id),
    ),
  };
}

export function recentEpisodeMismatch(
  state: Pick<DiscoverState, "browsing" | "draft">,
): boolean {
  const item = state.browsing.recentContext?.item;
  if (!item || state.draft.mode === "release") return false;
  const episode = state.draft.episodeIdentity;
  return (
    !episode ||
    item.identity_status === "conflict" ||
    item.source_id !== episode.source_id ||
    item.show_id !== episode.show_id ||
    item.season_id !== episode.season_id ||
    item.season !== episode.season ||
    item.episode !== episode.episode ||
    item.title !== episode.title ||
    item.air_date !== episode.air_date
  );
}
