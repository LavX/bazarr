export interface DiscoverIdentifiedSelection {
  mode?: "title";
  query?: never;
  media_type: "movie" | "episode";
  imdb_id: string;
  language: string;
  season?: number;
  episode?: number;
  title?: string;
  year?: number;
  show_id?: number;
  episode_identity?: MetadataEpisode;
  manual_confirmed?: boolean;
  /** Opaque. The server re-resolves and revalidates it on every use. */
  copy_id?: string;
}

export interface DiscoverReleaseSelection {
  mode: "release";
  query: string;
  language: string;
  media_type?: never;
  imdb_id?: never;
  season?: never;
  episode?: never;
  title?: never;
  year?: never;
  show_id?: never;
  episode_identity?: never;
  manual_confirmed?: never;
  copy_id?: never;
}

export type DiscoverSelection =
  | DiscoverIdentifiedSelection
  | DiscoverReleaseSelection;

/** One exact copy of a confirmed target, as the server resolved it. */
export interface DiscoverCopy {
  copy_id: string;
  media_type: "movie" | "episode";
  local_id: number;
  arr_instance_id: number | null;
  instance_name: string | null;
  series_local_id: number | null;
  title: string | null;
  episode_title: string | null;
  release: string | null;
  filename: string | null;
  source: string | null;
  resolution: string | null;
  video_codec: string | null;
  audio_codec: string | null;
  file_size: number | null;
  updated_at: string | null;
}

export interface DiscoverCopyOption extends DiscoverCopy {
  selectable: boolean;
  unavailable_reason:
    | "owner_unknown"
    | "no_stored_path"
    | "instance_missing"
    | null;
}

export interface DiscoverCopyOffer {
  items: DiscoverCopyOption[];
  truncated: boolean;
  owning_titles: number;
  match_scope: "exact_target_copy";
}

export interface DiscoverCopyContext extends DiscoverCopy {
  observed_size: number;
}

export type DiscoverContext =
  | (DiscoverIdentifiedSelection & {
      matching_mode: "title";
      /** Present only when a copy was explicitly chosen. */
      file_revision?: string;
      copy?: DiscoverCopyContext;
    })
  | (DiscoverReleaseSelection & { matching_mode: "release" });

/** Per-attribute evidence against the chosen copy. Never a timing guarantee. */
export type DiscoverCompatibility = "match" | "conflict" | "unknown";

export interface DiscoverCopyCompatibility {
  source: DiscoverCompatibility;
  resolution: DiscoverCompatibility;
  video_codec: DiscoverCompatibility;
  audio_codec: DiscoverCompatibility;
  release_group: DiscoverCompatibility;
  edition: DiscoverCompatibility;
}

export type DiscoverProviderStatus =
  | "success"
  | "empty"
  | "unverified"
  | "authentication_required"
  | "setup_required"
  | "cooldown"
  | "unreachable"
  | "timeout"
  | "error"
  | "skipped"
  | "saturated";

export interface DiscoverProviderOutcome {
  supported_media?: ("movie" | "episode")[];
  supported_languages?: string[];
  provider: string;
  status: DiscoverProviderStatus;
  reason: string | null;
  result_count: number;
  elapsed_ms: number;
  retry_at: string | null;
}

export interface DiscoverSearchProgress {
  phase: "preparing" | "searching" | "finished";
  providers: (Pick<DiscoverProviderOutcome, "provider" | "result_count"> & {
    status: DiscoverProviderStatus | "pending";
  })[];
}

export interface DiscoverSubtitleResult {
  id: string;
  search_id: string;
  provider: string;
  language: string;
  language_variant: string | null;
  release: string | null;
  scope: "full" | "forced" | "unknown";
  hearing_impaired: boolean | null;
  uploader: string | null;
  matches: string[] | null;
  compatibility_score: number | null;
  compatibility_score_max: number | null;
  rating: number | null;
  /**
   * Present whenever the search carried a chosen copy. Optional so a fixture
   * or an older snapshot without it still describes a valid result.
   */
  copy_compatibility?: DiscoverCopyCompatibility | null;
  checked_at: string;
  expires_at: string;
  stale: boolean;
}

export interface DiscoverSearchSnapshot {
  search_id: string;
  context: DiscoverContext;
  status: "complete" | "partial" | "failed";
  checked_at: string;
  attempted_at: string;
  cache_status: "fresh" | "cached" | "stale";
  coverage: {
    complete: boolean;
    configured_count: number;
    completed_count: number;
    providers: DiscoverProviderOutcome[];
  };
  results: DiscoverSubtitleResult[];
}

export interface DiscoverDownloadIdentity {
  resultId: string;
  searchId: string;
}

export interface DiscoverDownloadFeedback {
  requestId: number;
  /**
   * The draft context key this feedback was filed under. searchAgain reuses
   * the captured context, so the key it files results under has to travel
   * with that context rather than be recomputed beside it.
   */
  contextKey?: string;
  context: DiscoverContext;
  row: DiscoverSubtitleResult;
  status: "pending" | "started" | "expired" | "failed";
  filename?: string;
}

export interface DiscoverPreviewData {
  result_id: string;
  search_id: string;
  filename: string;
  cues: { start_ms: number; end_ms: number; text: string }[];
  total_cues: number;
  truncated: boolean;
}

export interface DiscoverPreviewFeedback {
  requestId: number;
  /**
   * The draft context key this feedback was filed under. searchAgain reuses
   * the captured context, so the key it files results under has to travel
   * with that context rather than be recomputed beside it.
   */
  contextKey?: string;
  context: DiscoverContext;
  row: DiscoverSubtitleResult;
  status: "pending" | "ready" | "expired" | "failed";
  data?: DiscoverPreviewData;
}

export type MetadataStatus =
  | "unconfigured"
  | "available"
  | "cached"
  | "authentication_failed"
  | "unavailable";

export type MetadataSource = "tmdb" | "omdb" | "local";

export interface LocalOwnership {
  episode_count: number;
  unknown_owners: boolean;
  truncated: boolean;
  selected_episode_owned: null;
  complete_series: null;
}
export interface LocalCopy {
  local_id: number;
  arr_instance_id: number | null;
  updated_at: string | null;
  episode_count: number | null;
}

export interface MetadataMovie {
  copies?: LocalCopy[];
  copies_truncated?: boolean;
  ownership?: LocalOwnership;
  provenance?: MetadataSource | "cached";
  source: "tmdb";
  source_id: string;
  id: number;
  media_type: "movie";
  title: string;
  year: number | null;
  imdb_id: string | null;
  mapping_status: "resolved" | "unresolved";
  overview: string;
  poster_url: string | null;
  backdrop_url: string | null;
}

export interface MetadataResponse {
  source: MetadataSource;
  fallback_revision?: string;
  primary?: {
    status: MetadataStatus;
    message: string;
    service_status?: "unavailable" | null;
  };
  fallback?: {
    status: MetadataStatus;
    message: string;
    failure_reason?: "quota" | "timeout";
  } | null;
  local_truncated?: boolean;
  truncated?: boolean;
  status: MetadataStatus;
  configured: boolean;
  revision: string;
  locale: string;
  message: string;
  checked_at: string | null;
  fetched_at: string | null;
  service_status?: "unavailable" | null;
  failure_reason?: "quota" | "timeout";
  unavailable_dependency?:
    | "show_external_ids"
    | "episode_external_ids"
    | "tvdb_episode";
  items?: MetadataTitle[];
  item?: MetadataTitle | null;
  season?: MetadataSeason | null;
  episode?: MetadataEpisode | null;
}

export interface MetadataShow extends Omit<MetadataMovie, "media_type"> {
  media_type: "show";
  tvdb_id: number | null;
  seasons:
    | {
        id: number;
        season: number;
        title: string;
        episode_count: number | null;
      }[]
    | null;
}
export interface MetadataFallbackTitle extends Omit<
  MetadataMovie,
  "source" | "id" | "media_type"
> {
  source: "omdb" | "local";
  id: string | number;
  media_type: "movie" | "show";
  tvdb_id?: number | null;
  seasons?: null;
}
export type MetadataTitle =
  | MetadataMovie
  | MetadataShow
  | MetadataFallbackTitle;
export interface MetadataSourceEpisode {
  source: "tmdb";
  source_id: string;
  show_id: number;
  season_id: number | null;
  id: number;
  season: number;
  episode: number;
  title: string;
  air_date: string | null;
}
export interface MetadataSeason {
  id: number;
  season: number;
  episodes: MetadataSourceEpisode[];
}
export interface MetadataEpisode extends MetadataSourceEpisode {
  imdb_id: string | null;
  tvdb_id: number | null;
  show_imdb_id: string | null;
  show_tvdb_id: number | null;
  show_title: string;
  show_year: number | null;
  target_season: number | null;
  target_episode: number | null;
  numbering: "tvdb_default" | null;
  identity_status: "resolved" | "unverified" | "conflict";
  absolute_episode: null;
  tvdb_absolute_number: number | null;
  mapping_updated_at: string | null;
}

export type TrendingMediaType = "all" | "movie" | "series";

export interface TrendingTitle {
  source_id: string;
  id: number;
  media_type: "movie" | "series";
  rank: number;
  title: string;
  year: number | null;
  overview: string;
  poster_url: string | null;
  backdrop_url: string | null;
}

export interface TrendingFeed {
  retry_after_ms?: number;
  source: "tmdb";
  period: "week";
  scope: "global";
  media_type: TrendingMediaType;
  revision: string;
  locale: string;
  configured: boolean;
  status:
    | "live"
    | "cached"
    | "empty"
    | "unconfigured"
    | "unavailable"
    | "authentication_failed";
  service_status: "unavailable" | null;
  fetched_at: string | null;
  expires_at: string | null;
  stale_until: string | null;
  last_success: string | null;
  attempted_at: string;
  items: TrendingTitle[];
}

export interface DigitalReleaseContext {
  source_id: string;
  release_date: string;
  release_type: "digital";
  region: string;
  fetched_at: string | null;
  window: { start: string; end: string };
}

export interface DigitalRelease extends Omit<
  TrendingTitle,
  "rank" | "media_type"
> {
  media_type: "movie";
  release_date: string;
  release_type: "digital";
  region: string;
  provenance: {
    source: "tmdb";
    path: string;
    region: string;
    type: 4;
    release_date: string;
  };
}

export interface DigitalReleaseFeed extends Omit<
  TrendingFeed,
  "period" | "scope" | "media_type" | "items"
> {
  region: string;
  release_type: "digital";
  window: { start: string; end: string };
  coverage: {
    complete: boolean;
    candidate_limit: number;
    candidates: number;
    checked: number;
    missing_region: number;
    failed: number;
    truncated: boolean;
  };
  items: DigitalRelease[];
}

export interface RecentEpisode extends MetadataSourceEpisode {
  air_date: string;
  show_title: string;
  show_year: number | null;
  poster_url: string | null;
  identity_status: "unverified" | "conflict";
  provenance: { source: "tmdb"; path: string; air_date: string };
}
export interface RecentEpisodeContext {
  item: RecentEpisode;
  fetched_at: string | null;
  window: { start: string; end: string };
}
export interface RecentEpisodeFeed extends Omit<
  TrendingFeed,
  "scope" | "media_type" | "items"
> {
  scope: "trending_shows";
  window: { start: string; end: string };
  coverage: {
    complete: boolean;
    show_limit: number;
    shows: number;
    shows_checked: number;
    season_limit: number;
    seasons: number;
    seasons_checked: number;
    episode_limit: number;
    output_limit: number;
    episodes_checked: number;
    missing_dates: number;
    failed: number;
    truncated: boolean;
  };
  items: RecentEpisode[];
}

/** Read-only summary of this Bazarr's own work, shown beside global discovery. */
export type DiscoverSummaryAvailability = "available" | "stale" | "unknown";

export type DiscoverSummaryState =
  | "busy"
  | "quiet"
  | "degraded"
  | "new_installation"
  | "unknown";

export interface DiscoverSummaryProgress {
  unit: "item" | "percent";
  value: number;
  total: number;
}

export interface DiscoverRemoteObservation {
  service_id: string;
  job_id: string;
  phase: string;
  observed_at: string | null;
  progress: number | null;
  total: number | null;
}

export interface DiscoverActivityItem {
  activity_id: string;
  operation: string;
  state: "running" | "queued";
  phase: "running" | "queued" | "waiting_for_service";
  name: string;
  scope_kind: string;
  arr_instance_id: number | null;
  instance_name: string | null;
  media_type: "episode" | "movie" | null;
  title: string | null;
  season: number | null;
  episode: number | null;
  episode_title: string | null;
  language: string | null;
  progress: DiscoverSummaryProgress | null;
  remote: DiscoverRemoteObservation | null;
  parent_activity_id: string | null;
  scheduler_run_id: string | null;
  observed_at: string | null;
}

export interface DiscoverScheduleItem {
  job_id: string;
  name: string;
  interval: string | null;
  next_run_in: string | null;
}

export interface DiscoverActivityComponent {
  availability: DiscoverSummaryAvailability;
  observed_at: string | null;
  complete: boolean;
  truncated: boolean;
  running_count: number | null;
  queued_count: number | null;
  scheduled_count: number | null;
  running: DiscoverActivityItem[];
  queued: DiscoverActivityItem[];
  scheduled: DiscoverScheduleItem[];
  unknown_sources: string[];
}

export interface DiscoverWantedComponent {
  availability: DiscoverSummaryAvailability;
  observed_at: string | null;
  complete: boolean;
  requirements: number | null;
  episode_requirements: number | null;
  movie_requirements: number | null;
  media_count: number | null;
  unknown_media_count: number | null;
  qualifications: string[];
  by_instance: {
    arr_instance_id: number | null;
    instance_name: string | null;
    requirements: number;
  }[];
}

export interface DiscoverArrival {
  library_id?: number | null;
  poster_url?: string | null;
  kind: "episode" | "movie" | "translation";
  event_id: string;
  status: "success";
  action: number | null;
  title: string | null;
  season: number | null;
  episode: number | null;
  episode_title: string | null;
  language: string | null;
  provider: string | null;
  arr_instance_id: number | null;
  instance_name: string | null;
  timestamp: string | null;
}

export interface DiscoverArrivalsStatus {
  availability: DiscoverSummaryAvailability;
  observed_at: string | null;
  complete: boolean;
  truncated: boolean;
  candidate_limit: number;
  display_limit: number;
  qualifications: string[];
}

export interface DiscoverAttentionItem {
  id: string;
  capability: "library_sync" | "library_paths" | "subtitle_providers" | string;
  severity: "warning" | "error";
  scope: {
    arr_instance_id?: number | null;
    instance_name?: string | null;
    kind?: string | null;
    folders?: number;
    example?: string | null;
    providers?: string[];
    alternatives?: boolean;
    enabled_count?: number;
  };
  summary: string;
  detail: string | null;
  freshness: "live" | "last_recorded_observation" | string;
  recovery: { label: string; target: string };
}

export interface DiscoverOnboardingItem {
  id: string;
  summary: string;
  target: string;
}

export interface DiscoverSummary {
  generated_at: string;
  state: DiscoverSummaryState;
  query_budget: number;
  activity: DiscoverActivityComponent;
  wanted: DiscoverWantedComponent;
  arrivals: DiscoverArrival[];
  arrivals_status: DiscoverArrivalsStatus;
  attention: {
    availability: DiscoverSummaryAvailability;
    observed_at: string | null;
    complete: boolean;
    unknown_sources: string[];
    items: DiscoverAttentionItem[];
  };
  onboarding: {
    availability: DiscoverSummaryAvailability;
    observed_at: string | null;
    complete: boolean;
    items: DiscoverOnboardingItem[];
  };
}
