/* eslint-disable camelcase -- API query parameters retain their transport names. */
import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import type {
  DiscoverDownloadIdentity,
  DiscoverSelection,
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
    }: {
      context: DiscoverSelection;
      refresh: boolean;
    }) => api.discover.search(context, refresh),
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
    queryFn: ({ signal }) => api.discover.summary(signal),
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
    staleTime: 300_000,
    gcTime: 3_600_000,
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
    staleTime: 300_000,
    gcTime: 3_600_000,
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
    staleTime: 300_000,
    gcTime: 3_600_000,
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
