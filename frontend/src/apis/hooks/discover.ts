/* eslint-disable camelcase -- API query parameters retain their transport names. */
import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import client from "@/apis/raw/client";
import type {
  DiscoverDownloadIdentity,
  DiscoverSelection,
  DiscoverSummary,
  MetadataSource,
  TrendingMediaType,
} from "@/types/discover";
import { useSystemSettings } from "./system";

export function useDiscoverSearch() {
  return useMutation({
    mutationKey: [QueryKeys.Discover, QueryKeys.Search],
    mutationFn: ({
      context,
      refresh,
      progressId,
    }: {
      context: DiscoverSelection;
      refresh: boolean;
      progressId?: string;
    }) => api.discover.search(context, refresh, progressId),
    retry: false,
    networkMode: "always",
    gcTime: 0,
  });
}

export function useDiscoverDownload() {
  return useMutation({
    mutationKey: [QueryKeys.Discover, "download"],
    mutationFn: (identity: DiscoverDownloadIdentity) =>
      api.discover.download(identity),
    retry: false,
    networkMode: "always",
    gcTime: 0,
  });
}

export function useDiscoverPreview() {
  return useMutation({
    mutationKey: [QueryKeys.Discover, "preview"],
    mutationFn: (identity: DiscoverDownloadIdentity) =>
      api.discover.preview(identity),
    retry: false,
    networkMode: "always",
    gcTime: 0,
  });
}

export const COPIES_QUERY_KEY = [QueryKeys.Discover, "copies"] as const;

/**
 * The exact library copies of one confirmed target.
 *
 * Reading this list adopts nothing: the default stays title-only until the
 * reader picks a copy. The cached result is kept long enough that returning
 * from a library page re-renders the picker on the first paint, so the
 * selected-title restorer finds the control it saved rather than an empty gap.
 */
export function useDiscoverCopies(
  target: {
    mediaType: "movie" | "episode";
    imdbId: string;
    season?: number;
    episode?: number;
  } | null,
) {
  const params = target
    ? {
        media_type: target.mediaType,
        imdb_id: target.imdbId,
        ...(target.mediaType === "episode"
          ? { season: target.season, episode: target.episode }
          : {}),
      }
    : {};
  return useQuery({
    queryKey: [...COPIES_QUERY_KEY, params],
    queryFn: ({ signal }) => api.discover.copies(params, signal),
    enabled: target !== null,
    staleTime: 30_000,
    gcTime: 300_000,
    retry: false,
    networkMode: "always",
  });
}

export const SUMMARY_QUERY_KEY = [QueryKeys.Discover, "summary"] as const;

/**
 * Whether a body is the summary rather than something else a 200 can carry.
 *
 * Only a non-2xx answer arrives as an error. A sign-in page from a reverse
 * proxy, an authentication message and the app's own index.html are all 200s
 * carrying a truthy body with none of the summary's components in it, and the
 * first panel to read one of them took the whole page down.
 */
export function isDiscoverSummary(body: unknown): body is DiscoverSummary {
  if (typeof body !== "object" || body === null) return false;
  const component = (value: unknown) =>
    typeof value === "object" && value !== null;
  const candidate = body as Record<string, unknown>;
  return (
    component(candidate.activity) &&
    component(candidate.attention) &&
    component(candidate.wanted) &&
    Array.isArray(candidate.arrivals)
  );
}

/**
 * Whether the summary has been read and could not be understood.
 *
 * Deliberately not `isError`: a query holding no data goes back to pending
 * while it refetches, so anything that watched isError would flip in and out
 * of existence, and a panel mounting is itself what starts the next read. This
 * only stops being true when a read actually succeeds.
 */
export function summaryUnreadable(summary: {
  isFetched: boolean;
  data: DiscoverSummary | undefined;
}): boolean {
  return summary.isFetched && summary.data === undefined;
}

/**
 * The read-only local work summary.
 *
 * It is deliberately independent of TMDB configuration and of the metadata
 * feeds: what this Bazarr is doing is knowable whether or not global metadata
 * is set up. There is no retry chain here, so a failed read stays visibly
 * unavailable until the reader asks again, rather than silently reappearing as
 * zero work.
 */
export function useDiscoverSummary() {
  return useQuery({
    queryKey: SUMMARY_QUERY_KEY,
    queryFn: async ({ signal }) => {
      const body = await api.discover.summary(signal);
      // A read that answers with something else is a failed read, not a
      // summary with holes in it. Handed on as data it would be dereferenced
      // by every panel on the page, and the panels would report its counts as
      // absent rather than as unknown.
      if (!isDiscoverSummary(body)) {
        throw new Error("The local summary could not be read.");
      }
      return body;
    },
    staleTime: 15_000,
    gcTime: 60_000,
    retry: false,
    networkMode: "always",
  });
}

export const METADATA_QUERY_KEY = [QueryKeys.Discover, "metadata"] as const;

export function normalizeTitleQuery(query: string) {
  return query
    .normalize("NFKD")
    .toLowerCase()
    .replace(/\p{M}/gu, "")
    .replace(/['’ʼ]/gu, "")
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim();
}

export function useDiscoverMetadata(
  path: string,
  query?: string,
  enabled = true,
  mediaType: "movie" | "show" = "movie",
  source: MetadataSource | "all" = "tmdb",
  localQuery?: string,
) {
  const settings = useSystemSettings();
  const revision = settings.data?.discover?.metadata_revision;
  const configured = settings.data?.discover?.tmdb_configured ?? false;
  const scope = useQuery({
    queryKey: [
      ...METADATA_QUERY_KEY,
      "fallback-configuration",
      settings.dataUpdatedAt,
    ],
    queryFn: ({ signal }) => api.discover.metadata("status", signal),
    enabled: enabled && source !== "tmdb" && Boolean(revision),
    staleTime: 300_000,
    gcTime: 60_000,
    retry: false,
  });
  const sourceRevision =
    source === "local" ? "local" : scope.data?.fallback_revision;
  const result = useQuery({
    queryKey: [
      ...METADATA_QUERY_KEY,
      source === "omdb" || source === "local" ? sourceRevision : revision,
      path,
      query,
      mediaType,
      source,
      source === "all" ? sourceRevision : null,
      source === "all" ? localQuery : null,
    ],
    queryFn: ({ signal }) =>
      api.discover.metadata(path, signal, {
        ...(query === undefined ? {} : { q: query, type: mediaType }),
        ...(source === "all" && localQuery !== undefined
          ? { local_q: localQuery }
          : {}),
        ...(source !== "tmdb" ||
        path.startsWith("movies/") ||
        (path.startsWith("shows/") && !path.includes("/seasons/"))
          ? { source }
          : {}),
      }),
    enabled:
      enabled &&
      Boolean(revision) &&
      (source === "local" ||
        (source === "tmdb" ? configured : !scope.isPending)),
    staleTime: source === "local" ? 0 : 300_000,
    gcTime: 60_000,
    retry: false,
  });
  return {
    ...result,
    primaryMetadata: scope.data,
    data:
      source === "omdb" ||
      source === "local" ||
      result.data?.revision === revision
        ? result.data
        : undefined,
    configured,
    revision,
    settingsLoading: settings.isLoading,
    settingsError: settings.isError,
  };
}

class FeedBusy extends Error {}
async function admittedFeed<
  T extends { retry_after_ms?: number; items: unknown[] },
>(request: Promise<T>): Promise<T> {
  const data = await request;
  if (data.retry_after_ms && !data.items.length)
    throw new FeedBusy("Metadata feeds are busy. Retry shortly.");
  return data;
}
/**
 * How long a global metadata feed is worth re-asking for.
 *
 * It mirrors FRESH_SECONDS on the server, which is the only thing that decides
 * when these feeds are actually re-fetched from TMDB. Asking more often than
 * that cannot produce newer titles, it just spends a round trip to be handed
 * the same payload back, and the responses now carry a matching max-age so the
 * browser holds them across a page load too. The two have to be changed
 * together: a shorter window here is wasted work, a longer one shows titles the
 * server has already replaced.
 */
const FEED_FRESH_MS = 3_600_000;

const feedRetry = (count: number, error: Error) =>
  error instanceof FeedBusy && count < 2;
const feedRetryDelay = (attempt: number) => (attempt === 0 ? 1000 : 12000);

export function useDiscoverTrending(mediaType: TrendingMediaType) {
  const settings = useSystemSettings();
  const revision = settings.data?.discover?.metadata_revision;
  const configured = settings.data?.discover?.tmdb_configured ?? false;
  const result = useQuery({
    queryKey: [...METADATA_QUERY_KEY, revision, "trending", "week", mediaType],
    queryFn: ({ signal }) =>
      admittedFeed(api.discover.trending(mediaType, signal)),
    enabled: Boolean(revision) && configured,
    staleTime: FEED_FRESH_MS,
    gcTime: FEED_FRESH_MS * 2,
    networkMode: "always",
    retry: feedRetry,
    retryDelay: feedRetryDelay,
  });
  return {
    ...result,
    data:
      configured &&
      result.data?.revision === revision &&
      result.data?.media_type === mediaType
        ? result.data
        : undefined,
    configured,
    settingsLoading: settings.isLoading,
    settingsError: settings.isError,
  };
}

export function useDiscoverDigitalReleases(region: string) {
  const settings = useSystemSettings();
  const revision = settings.data?.discover?.metadata_revision;
  const configured = settings.data?.discover?.tmdb_configured ?? false;
  const [today, setToday] = useState(() =>
    new Date().toISOString().slice(0, 10),
  );
  useEffect(() => {
    const midnight = Date.parse(today + "T00:00:00Z") + 86_400_000;
    const timer = window.setTimeout(
      () => setToday(new Date().toISOString().slice(0, 10)),
      Math.max(0, midnight - Date.now()) + 1,
    );
    return () => window.clearTimeout(timer);
  }, [today]);
  const start = new Date(Date.parse(today) - 29 * 86_400_000)
    .toISOString()
    .slice(0, 10);
  const result = useQuery({
    queryKey: [
      ...METADATA_QUERY_KEY,
      revision,
      "digital",
      region,
      start,
      today,
      4,
    ],
    queryFn: ({ signal }) =>
      admittedFeed(api.discover.digitalReleases(region, signal)),
    enabled: Boolean(revision) && configured,
    staleTime: FEED_FRESH_MS,
    gcTime: FEED_FRESH_MS * 2,
    networkMode: "always",
    retry: feedRetry,
    retryDelay: feedRetryDelay,
  });
  return {
    ...result,
    data:
      configured &&
      result.data?.revision === revision &&
      result.data?.region === region &&
      result.data?.release_type === "digital" &&
      result.data?.window.start === start &&
      result.data?.window.end === today
        ? result.data
        : undefined,
    configured,
    settingsLoading: settings.isLoading,
    settingsError: settings.isError,
  };
}

export function useDiscoverRecentEpisodes() {
  const settings = useSystemSettings();
  const revision = settings.data?.discover?.metadata_revision;
  const configured = settings.data?.discover?.tmdb_configured ?? false;
  const [today, setToday] = useState(() =>
    new Date().toISOString().slice(0, 10),
  );
  useEffect(() => {
    const midnight = Date.parse(today + "T00:00:00Z") + 86_400_000;
    const timer = window.setTimeout(
      () => setToday(new Date().toISOString().slice(0, 10)),
      Math.max(0, midnight - Date.now()) + 1,
    );
    return () => window.clearTimeout(timer);
  }, [today]);
  const start = new Date(Date.parse(today) - 29 * 86_400_000)
    .toISOString()
    .slice(0, 10);
  const result = useQuery({
    queryKey: [
      ...METADATA_QUERY_KEY,
      revision,
      "recent-episodes",
      "week",
      start,
      today,
    ],
    queryFn: ({ signal }) => admittedFeed(api.discover.recentEpisodes(signal)),
    enabled: Boolean(revision) && configured,
    staleTime: FEED_FRESH_MS,
    gcTime: FEED_FRESH_MS * 2,
    networkMode: "always",
    retry: feedRetry,
    retryDelay: feedRetryDelay,
  });
  return {
    ...result,
    data:
      configured &&
      result.data?.revision === revision &&
      result.data?.source === "tmdb" &&
      result.data?.scope === "trending_shows" &&
      result.data?.period === "week" &&
      result.data?.window.start === start &&
      result.data?.window.end === today
        ? result.data
        : undefined,
    configured,
    settingsLoading: settings.isLoading,
    settingsError: settings.isError,
  };
}

/**
 * The next few titles still missing subtitles, for the Discover homepage.
 *
 * The Wanted page owns the full list and pages through it; this is the short
 * head of that same queue, so it asks for a bounded slice rather than reusing
 * the table's pagination. It shares the Wanted query key prefix so acting on an
 * item from here invalidates both surfaces together.
 *
 * Sonarr and Radarr are asked separately because a reader may run only one of
 * them, and an install with no Radarr must not be shown a failed movie query.
 */
/**
 * One sports event still missing subtitles.
 *
 * Sportarr reports missing languages as bare codes rather than the objects the
 * series and movie endpoints return, so the shapes cannot be shared.
 */
export interface SportsWantedEvent {
  id: number;
  title: string;
  league_id: number | null;
  partName: string | null;
  missing_subtitles: string[];
  arr_instance_id: number | null;
}

/**
 * Sportarr is optional and is not present in every build, so it is reached
 * through the shared client by path rather than through a typed api module
 * that may not be there, and the setting is read defensively for the same
 * reason. Where it is off, the query never runs and the group never appears.
 */
function useSportarrEnabled() {
  const settings = useSystemSettings();
  const general = settings.data?.general as
    | { use_sportarr?: boolean }
    | undefined;
  return general?.use_sportarr ?? false;
}

/**
 * How many sports events still need subtitles.
 *
 * Read from Sportarr's own wanted endpoint rather than counted here, so the
 * figure is the one its Wanted page shows: that query also applies monitoring,
 * language profiles and league exclusions, and a count assembled separately
 * would drift from it. One row is asked for because only the total is wanted.
 */
export function useSportsWantedCount() {
  const sportarr = useSportarrEnabled();
  return useQuery({
    queryKey: [QueryKeys.Discover, "sports", QueryKeys.Wanted, "count"],
    queryFn: async () => {
      const response = await client.axios.get<{ total: number }>(
        "/sports/wanted",
        { params: { start: 0, length: 1 } },
      );
      return response.data?.total ?? 0;
    },
    enabled: sportarr,
    staleTime: 30_000,
    retry: false,
  });
}

export function useWantedPreview(limit: number) {
  const settings = useSystemSettings();
  const sportarr = useSportarrEnabled();
  const sports = useQuery({
    queryKey: [
      QueryKeys.Discover,
      "sports",
      QueryKeys.Wanted,
      "preview",
      limit,
    ],
    queryFn: async () => {
      const response = await client.axios.get<{ data: SportsWantedEvent[] }>(
        "/sports/wanted",
        { params: { start: 0, length: limit } },
      );
      return response.data?.data ?? [];
    },
    enabled: sportarr,
    staleTime: 30_000,
    retry: false,
  });
  const series = useQuery({
    queryKey: [QueryKeys.Series, QueryKeys.Wanted, "preview", limit],
    queryFn: () => api.episodes.wanted({ start: 0, length: limit }),
    enabled: settings.data?.general.use_sonarr ?? false,
    staleTime: 30_000,
    retry: false,
  });
  const movies = useQuery({
    queryKey: [QueryKeys.Movies, QueryKeys.Wanted, "preview", limit],
    queryFn: () => api.movies.wanted({ start: 0, length: limit }),
    enabled: settings.data?.general.use_radarr ?? false,
    staleTime: 30_000,
    retry: false,
  });
  // Reported per source. Combining them meant a failing or slow Radarr, or the
  // optional Sportarr call, blanked the whole section including the kinds that
  // had already answered.
  const connected = [series, movies, sports].filter((query) => query.isEnabled);
  // Switching an integration off stops its query but keeps its last answer in
  // the cache, so each source is read only while it is still enabled.
  return {
    episodes: series.isEnabled ? (series.data?.data ?? []) : [],
    movies: movies.isEnabled ? (movies.data?.data ?? []) : [],
    sports: sports.isEnabled ? (sports.data ?? []) : [],
    failed: {
      episodes: series.isEnabled && series.isError,
      movies: movies.isEnabled && movies.isError,
      sports: sports.isEnabled && sports.isError,
    },
    // Judged over the sources that are actually configured, and only when all
    // of them agree. A disabled source is not evidence of anything: counting
    // its false "not pending" or "not failing" would report the section ready,
    // or healthy, on the strength of a query that never ran. A reader with
    // none connected is not loading and not failing, they simply have no
    // library to be missing anything from.
    isPending:
      connected.length > 0 && connected.every((query) => query.isPending),
    isError: connected.length > 0 && connected.every((query) => query.isError),
    connected: connected.length > 0,
  };
}
