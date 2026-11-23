import { QueryClient } from "@tanstack/react-query";
import { debounce, DebouncedFunc } from "lodash";
import { QueryKeys } from "./keys";

const historyKeys = {
  movie: [QueryKeys.Movies, QueryKeys.History],
  episode: [QueryKeys.Series, QueryKeys.Episodes, QueryKeys.History],
} as const;

const refreshes = new WeakMap<
  QueryClient,
  DebouncedFunc<() => Promise<void>>
>();

export const SOURCE_HISTORY_DEBOUNCE_MS = 1_000;
export const SOURCE_HISTORY_MAX_WAIT_MS = 5_000;

export async function invalidateSourceHistory(
  client: QueryClient,
  kinds?: readonly string[],
) {
  if (kinds === undefined) refreshes.get(client)?.cancel();
  const keys = Object.entries(historyKeys)
    .filter(([kind]) => kinds === undefined || kinds.includes(kind))
    .map(([, key]) => key);
  await Promise.all(
    keys.map(async (queryKey) => {
      const filters = {
        queryKey,
        predicate: ({ queryKey: key }: { queryKey: readonly unknown[] }) => {
          const scope = key[queryKey.length + 1];
          return (
            typeof scope === "object" &&
            scope !== null &&
            "includeSources" in scope &&
            scope.includeSources === true
          );
        },
      };
      // An initial request can still contain the pre-write history snapshot.
      // Cancel it before invalidation so React Query requests fresh data.
      await client.cancelQueries(filters);
      await client.invalidateQueries(filters);
    }),
  );
}

// Source indexing can emit before the history write commits. Terminal job
// events provide another refresh after queued downloads and upgrades finish.
// One bounded timer covers bursts of either event without tracking each job.
export function scheduleSourceHistoryRefresh(client: QueryClient) {
  let refresh = refreshes.get(client);
  if (refresh === undefined) {
    refresh = debounce(
      () => invalidateSourceHistory(client),
      SOURCE_HISTORY_DEBOUNCE_MS,
      { maxWait: SOURCE_HISTORY_MAX_WAIT_MS },
    );
    refreshes.set(client, refresh);
  }
  void refresh();
}
