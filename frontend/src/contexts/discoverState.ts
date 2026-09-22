import type {
  DigitalReleaseContext,
  DiscoverContext,
  DiscoverDownloadFeedback,
  DiscoverPreviewFeedback,
  DiscoverSearchProgress,
  DiscoverSearchSnapshot,
  DiscoverSelection,
  DiscoverSubtitleResult,
  MetadataEpisode,
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
  /**
   * The explicitly chosen library copy, or undefined for title-only matching.
   * Opaque: the server resolves it against the confirmed target every time.
   */
  copyId?: string;
}

export interface DiscoverBrowsing {
  recentContext: RecentEpisodeContext | null;
  digitalRegion: string;
  releaseContext: DigitalReleaseContext | null;
  pagePosition: { target: string; focusId: string; scrollY: number } | null;
  trendingFilter: TrendingMediaType;
  featuredSourceId: string | null;
  manualSearch: boolean;
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
  optionsOpen: boolean;
  returnTarget: string;
  focusId: string;
  scrollY: number;
}

/**
 * The rows a running search has already published, under the context it is
 * searching. It exists only while that search runs: the moment the server
 * answers, the completed snapshot owns the list, and every other outcome
 * (failure, a changed selection, leaving the title, signing out) drops it.
 *
 * It is deliberately not a snapshot. A snapshot is a finished answer with
 * coverage, a checked time and a cache status; this is an unfinished list, and
 * typing it as the same thing would invite a reader to treat it as settled.
 */
export interface DiscoverLiveResults {
  context: DiscoverContext;
  results: DiscoverSubtitleResult[];
}

export interface DiscoverState {
  sessionId: number;
  browsing: DiscoverBrowsing;
  draft: DiscoverDraft;
  generation: number;
  status:
    | "unsearched"
    | "searching"
    | "complete"
    | "partial"
    | "failed"
    // No provider was asked: every outcome was a skip, unmet setup, or a call
    // that never started. Mirrors the snapshot state of the same name.
    | "skipped";
  snapshot: DiscoverSearchSnapshot | null;
  live: DiscoverLiveResults | null;
  searchProgress?: DiscoverSearchProgress;
  error: string | null;
  storageAvailable: boolean;
  // True while the subtitle language was preselected from the reader's
  // language profile rather than chosen or previously used. It is shown, not
  // hidden, and it clears the moment the reader changes the language or
  // searches with it.
  languageSeeded: boolean;
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
    state.browsing.manualSearch,
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
    sessionId: 0,
    browsing: {
      recentContext: null,
      digitalRegion: "US",
      releaseContext: null,
      trendingFilter: "all",
      featuredSourceId: null,
      manualSearch: false,
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
      optionsOpen: false,
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
    live: null,
    error: null,
    storageAvailable,
    languageSeeded: false,
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
  /* eslint-disable camelcase -- the selection is the request body, so its keys
     keep the API spelling. */
  const selection: DiscoverSelection = {
    media_type: draft.mediaType,
    imdb_id: imdbId,
    language: draft.language,
  };
  /* eslint-enable camelcase */
  if (draft.mediaType === "episode") {
    if (!/^\d{1,4}$/.test(draft.season) || !/^\d{1,4}$/.test(draft.episode))
      return null;
    selection.season = Number(draft.season);
    selection.episode = Number(draft.episode);
    if (selection.episode < 1) return null;
    if (draft.manualEntry && !draft.manualConfirmed) return null;
    const identity = draft.episodeIdentity;
    if (identity?.identity_status === "conflict") return null;
    // An identity the show now contradicts cannot run a search on its own, but
    // it must not stand in the way of a confirmed manual one either: a reader
    // who confirmed season and episode by hand has taken over exactly the
    // numbering this identity would have supplied. It is left out of that
    // request, because the server re-derives the mapping from the series
    // identity and rejects a manual search carrying a superseded episode.
    const identityFits =
      identity === undefined || episodeMatchesShow(identity, draftShow(draft));
    if (!identityFits && !draft.manualConfirmed) return null;
    if (
      !draft.manualConfirmed &&
      (!identity ||
        identity.identity_status !== "resolved" ||
        identity.target_season !== selection.season ||
        identity.target_episode !== selection.episode)
    )
      return null;
    /* eslint-disable camelcase -- transport field names */
    if (identity && identityFits) selection.episode_identity = identity;
    if (draft.showId !== undefined) selection.show_id = draft.showId;
    if (draft.manualConfirmed) selection.manual_confirmed = true;
    /* eslint-enable camelcase */
  }
  if (draft.title) selection.title = draft.title;
  if (draft.year) selection.year = draft.year;
  // eslint-disable-next-line camelcase -- the transport field name
  if (draft.copyId) selection.copy_id = draft.copyId;
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

/**
 * The show an episode has to belong to, as the five compared fields. A show
 * item and the draft hold the same five under different names, so typing the
 * comparison over the fields rather than over either holder is what lets both
 * callers share one predicate instead of hand-copying it.
 */
/* eslint-disable camelcase -- the episode's show_* fields and the show item's
   own names, which are what the two callers actually hold. */
export interface ShowIdentity {
  id: number | null | undefined;
  imdb_id: string | null | undefined;
  tvdb_id: number | null | undefined;
  title: string | null | undefined;
  year: number | null | undefined;
}

/**
 * A missing value is missing whichever way a payload spells it. The read routes
 * omit a nullable id rather than sending null on some paths and send null on
 * others, and a title with no TVDB id at all is exactly the title whose
 * numbering cannot be verified, so refusing it for `null !== undefined` would
 * report a contradiction the source never made.
 */
function sameAbsent<T>(
  left: T | null | undefined,
  right: T | null | undefined,
): boolean {
  return (left ?? undefined) === (right ?? undefined);
}

export function episodeMatchesShow(
  episode: MetadataEpisode,
  show: ShowIdentity,
): boolean {
  return (
    episode.show_id === show.id &&
    sameAbsent(episode.show_imdb_id, show.imdb_id) &&
    sameAbsent(episode.show_tvdb_id, show.tvdb_id) &&
    episode.show_title === show.title &&
    sameAbsent(episode.show_year, show.year)
  );
}

/**
 * The draft's spelling of the show an episode must belong to. The draft keeps
 * the IMDb id as the reader typed it and the request lowercases and trims it,
 * so the normalisation happens here, once, rather than in a second comparison.
 */
function draftShow(draft: DiscoverDraft): ShowIdentity {
  return {
    id: draft.showId,
    imdb_id: draft.imdbId.trim().toLowerCase(),
    tvdb_id: draft.showTvdbId,
    title: draft.title,
    year: draft.year,
  };
}
/* eslint-enable camelcase */

/**
 * The target a chosen copy belongs to.
 *
 * A copy resolved for one film or one exact episode is meaningless for
 * another, so the choice is dropped structurally when this changes rather
 * than left to every caller of updateDraft to remember.
 *
 * The search mode is deliberately absent. A release query can never carry a
 * copy, because searchSelection returns before the copy line is reached, and
 * discoverContextKey already differs completely between the two modes. Adding
 * mode here would only discard a still-valid choice when a reader looks at
 * release search and comes back.
 */
export function copyTargetKey(draft: DiscoverDraft): string {
  return JSON.stringify([
    draft.mediaType,
    draft.imdbId.trim().toLowerCase(),
    draft.mediaType === "episode" ? draft.season : null,
    draft.mediaType === "episode" ? draft.episode : null,
  ]);
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
    // A different copy is a different search context: its results, preview,
    // feedback and pending responses all belong to the copy that was chosen.
    draft.copyId ?? null,
  ]);
}

type DiscoverAction =
  | { type: "restore"; state: DiscoverState; generation: number }
  | { type: "browsing"; changes: Partial<DiscoverBrowsing> }
  | {
      type: "draft";
      draft: DiscoverDraft;
      generation: number;
      storageAvailable: boolean;
      languageSeeded?: boolean;
      cached?: DiscoverState;
    }
  | { type: "progress"; generation: number; progress: DiscoverSearchProgress }
  | { type: "start"; generation: number; storageAvailable?: boolean }
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
  | { type: "cancel"; generation: number }
  | { type: "clear"; generation: number };

/**
 * The rows the reader is actually being offered: the finished snapshot's when
 * there is one, and otherwise the running search's. A row is offered the
 * moment it is rendered, so this is what decides whether a download or a
 * preview names a result that is really on the page.
 */
export function offeredResults(
  state: Pick<DiscoverState, "snapshot" | "live">,
): DiscoverSubtitleResult[] {
  return state.snapshot?.results ?? state.live?.results ?? [];
}

/**
 * Fold one observation into the rows already shown.
 *
 * Append-only, and deliberately so. The finished snapshot lists results in the
 * order the providers returned them, which is the order they are appended in
 * here, so there is no final re-ranking for this to fight with and no reason
 * to move a row a reader is already reading. Rows are matched by id, so a
 * repeated observation adds nothing, and a row that survives into the finished
 * snapshot keeps the identity its Download button was already using.
 */
function liveResults(
  previous: DiscoverLiveResults | null,
  progress: DiscoverSearchProgress,
): DiscoverLiveResults | null {
  const { context, results } = progress;
  if (!context || !Array.isArray(results)) return previous;
  const rows = results.filter(
    (row) =>
      typeof row?.id === "string" &&
      typeof row.search_id === "string" &&
      typeof row.expires_at === "string",
  );
  // Rows of an earlier attempt at the same selection are not rows of this one.
  const kept =
    previous?.results.filter((row) => row.search_id === progress.search_id) ??
    [];
  const seen = new Set(kept.map((row) => row.id));
  return {
    context,
    results: [...kept, ...rows.filter((row) => !seen.has(row.id))],
  };
}

export function discoverReducer(
  state: DiscoverState,
  action: DiscoverAction,
): DiscoverState {
  if (action.type === "restore") {
    const saved = action.state;
    return {
      ...saved,
      generation: action.generation,
      status:
        saved.status === "searching"
          ? (saved.snapshot?.status ?? "unsearched")
          : saved.status,
      // A filed page holds no owner for a search that was still running, so
      // the rows that search had published so far have nobody to finish them.
      live: null,
      searchProgress: undefined,
      download: saved.download?.status === "pending" ? null : saved.download,
      preview: saved.preview?.status === "pending" ? null : saved.preview,
    };
  }
  if (action.type === "browsing")
    return {
      ...state,
      browsing: {
        ...state.browsing,
        ...(action.changes.selectedId != null
          ? { releaseContext: null, recentContext: null, manualSearch: false }
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
    return {
      ...initialDiscoverState(),
      sessionId: state.sessionId + 1,
      generation: action.generation,
    };
  }
  if (action.type === "draft") {
    const changed =
      discoverContextKey(state.draft) !== discoverContextKey(action.draft);
    return {
      ...state,
      draft: action.draft,
      generation: action.generation,
      storageAvailable: action.storageAvailable,
      languageSeeded: action.languageSeeded ?? state.languageSeeded,
      ...(changed
        ? ({
            snapshot: null,
            live: null,
            status: "unsearched",
            error: null,
            download: null,
            preview: null,
            retiredResultIds: [],
            ...(action.cached
              ? {
                  snapshot: action.cached.snapshot,
                  status: action.cached.snapshot?.status ?? "unsearched",
                  download:
                    action.cached.download?.status === "pending"
                      ? null
                      : action.cached.download,
                  preview:
                    action.cached.preview?.status === "pending"
                      ? null
                      : action.cached.preview,
                  retiredResultIds: action.cached.retiredResultIds,
                }
              : {}),
          } as const)
        : {}),
    };
  }
  if (action.type === "progress") {
    // The same two guards the whole search path uses. An observation of a
    // search the reader has moved on from is discarded here, rows and all,
    // exactly as its final response would be.
    if (action.generation !== state.generation || state.status !== "searching")
      return state;
    return {
      ...state,
      searchProgress: action.progress,
      live: liveResults(state.live, action.progress),
    };
  }
  if (action.type === "start") {
    return {
      ...state,
      generation: action.generation,
      status: "searching",
      // Nothing has been published for this search yet, and the rows of the
      // one before it are not an answer to it.
      live: null,
      searchProgress: undefined,
      error: null,
      // Searching with the language is using it: it stops being a seed, and
      // the write that promotes it is the same evidence about storage as any
      // other: if it did not land, the notice has to say so here too.
      storageAvailable: action.storageAvailable ?? state.storageAvailable,
      languageSeeded: false,
    };
  }
  if (action.type === "close-preview") return { ...state, preview: null };
  if (action.type === "cancel")
    // Leaving the title retires what was still in flight. Filed results and
    // settled feedback stay for the shared form; only the pending ones lose
    // their owner, so the status cannot stick on searching forever.
    return {
      ...state,
      generation: action.generation,
      status: state.status === "searching" ? "unsearched" : state.status,
      live: null,
      download: state.download?.status === "pending" ? null : state.download,
      preview: state.preview?.status === "pending" ? null : state.preview,
    };
  if (action.type === "preview") {
    const { feedback } = action;
    if (
      action.key !== discoverContextKey(state.draft) ||
      (feedback.status !== "pending" &&
        state.preview?.requestId !== feedback.requestId) ||
      !offeredResults(state).some(
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
      !offeredResults(state).some(
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
      live: null,
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
    live: null,
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
