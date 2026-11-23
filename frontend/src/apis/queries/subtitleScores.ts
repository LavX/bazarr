import { QueryClient, QueryFilters } from "@tanstack/react-query";
import { debounce, DebouncedFunc } from "lodash";
import { QueryKeys } from "./keys";

type Media = QueryKeys.Movies | QueryKeys.Series;

const refreshes = new WeakMap<
  QueryClient,
  Map<Media, DebouncedFunc<() => Promise<void>>>
>();

// Current subtitle membership and history writes both change list scores.
// Coalesce changes across the whole library, with a bounded wait for
// scans/downloads that keep producing events. Unscored lists stay untouched.
export function scheduleSubtitleScoresRefresh(
  client: QueryClient,
  media: Media,
) {
  let scheduled = refreshes.get(client);
  if (scheduled === undefined) {
    scheduled = new Map();
    refreshes.set(client, scheduled);
  }
  let refresh = scheduled.get(media);
  if (refresh === undefined) {
    refresh = debounce(
      async () => {
        const filters: QueryFilters[] = [
          { queryKey: [media, QueryKeys.Range, { scores: true }] },
          {
            queryKey: [media, "library"],
            predicate: ({ queryKey }) => isScoredRangeQuery(queryKey),
          },
        ];
        // Invalidating alone reuses an initial read from before this write.
        await Promise.all(
          filters.map((filter) => client.cancelQueries(filter)),
        );
        await Promise.all(
          filters.map((filter) => client.invalidateQueries(filter)),
        );
      },
      1_000,
      { maxWait: 5_000 },
    );
    scheduled.set(media, refresh);
  }
  void refresh();
}

/** Whether a query key is a scored range read (the whole-list aggregation). */
export function isScoredRangeQuery(queryKey: readonly unknown[]): boolean {
  const range = queryKey[1] === "library" ? 3 : 1;
  if (queryKey[range] !== QueryKeys.Range) return false;
  const params = queryKey[range + 1];
  return (
    typeof params === "object" &&
    params !== null &&
    (params as { scores?: unknown }).scores === true
  );
}

/**
 * Predicate for a broad list invalidation: the scored range reads are left to
 * the debounced scheduler below, so one scan event per row cannot restart the
 * expensive aggregation.
 */
export function skipScoredRangeQueries(query: {
  queryKey: readonly unknown[];
}): boolean {
  return !isScoredRangeQuery(query.queryKey);
}
