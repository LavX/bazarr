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
}

export type DiscoverSelection =
  | DiscoverIdentifiedSelection
  | DiscoverReleaseSelection;

export type DiscoverContext =
  | (DiscoverIdentifiedSelection & { matching_mode: "title" })
  | (DiscoverReleaseSelection & { matching_mode: "release" });

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
  provider: string;
  status: DiscoverProviderStatus;
  reason: string | null;
  result_count: number;
  elapsed_ms: number;
  retry_at: string | null;
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
