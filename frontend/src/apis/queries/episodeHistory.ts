import { QueryClient } from "@tanstack/react-query";
import { episodesHistoryKey, QueryKeys } from "./keys";

const pendingRefreshes = new WeakMap<
  QueryClient,
  { seriesIds: Set<number>; finished: Promise<void> }
>();

// Socket.IO reduces a batch synchronously. Combine episode and history events
// before refetching, so each show's full history is requested once per batch.
export function invalidateEpisodesHistory(
  client: QueryClient,
  seriesId: number,
) {
  const pending = pendingRefreshes.get(client);
  if (pending) {
    pending.seriesIds.add(seriesId);
    return pending.finished;
  }

  const seriesIds = new Set([seriesId]);
  const finished = Promise.resolve().then(async () => {
    pendingRefreshes.delete(client);
    await Promise.all(
      [...seriesIds].map(async (id) => {
        const filters = { queryKey: episodesHistoryKey(id), exact: true };
        const state = client.getQueryState(filters.queryKey);
        // React Query reuses an in-flight initial fetch even when invalidated.
        // Cancel it explicitly so its pre-mutation snapshot cannot become fresh.
        if (state && state.data === undefined && state.fetchStatus !== "idle") {
          await client.cancelQueries(filters);
        }
        await client.invalidateQueries(filters);
      }),
    );
  });
  pendingRefreshes.set(client, { seriesIds, finished });
  return finished;
}

// Mutations receive upstream ids. Resolve their local owners from cached rows
// rather than using an upstream number as a local query key. The series list
// remains available even after the individually primed episode cache expires.
export function invalidateEpisodeHistory(
  client: QueryClient,
  owner: { episodeId?: number; seriesId?: number; arrInstanceId?: number },
) {
  if (owner.episodeId === undefined && owner.seriesId === undefined) {
    return Promise.resolve();
  }
  const individualEpisodes = client.getQueriesData<Item.Episode>({
    queryKey: [QueryKeys.Episodes],
    predicate: ({ queryKey }) =>
      queryKey.length === 2 && typeof queryKey[1] === "number",
  });
  const seriesEpisodes = client.getQueriesData<Item.Episode[]>({
    queryKey: [QueryKeys.Series],
    predicate: ({ queryKey }) =>
      queryKey.length === 4 &&
      queryKey[2] === QueryKeys.Episodes &&
      queryKey[3] === QueryKeys.All,
  });
  const episodes = [
    ...individualEpisodes.map(([, episode]) => episode),
    ...seriesEpisodes.flatMap(([, rows]) => rows ?? []),
  ];
  const seriesIds = new Set<number>();
  episodes.forEach((episode) => {
    if (
      episode &&
      (owner.episodeId === undefined ||
        episode.sonarrEpisodeId === owner.episodeId) &&
      (owner.seriesId === undefined ||
        episode.sonarrSeriesId === owner.seriesId) &&
      (owner.arrInstanceId === undefined ||
        episode.arr_instance_id === owner.arrInstanceId)
    ) {
      seriesIds.add(episode.series_id);
    }
  });
  return Promise.all(
    [...seriesIds].map((id) => invalidateEpisodesHistory(client, id)),
  ).then(() => undefined);
}
