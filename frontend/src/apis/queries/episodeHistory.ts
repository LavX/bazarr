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
  // A refresh that runs now covers the events the pending socket refresh was
  // waiting out, so its timer does not fetch the same history again.
  const scheduled = pendingSocketRefreshes.get(client);
  const pendingSocket = scheduled?.get(seriesId);
  if (pendingSocket !== undefined) {
    clearTimeout(pendingSocket.timer);
    scheduled?.delete(seriesId);
  }

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

// A first scan or a landing batch writes many rows for one show in a few
// seconds, and every write emits its own event. A show's full history is the
// heaviest request on the page, so the socket path waits for the writes to
// stop and then asks once, a moment after the last one.
export const EPISODES_HISTORY_DEBOUNCE_MS = 1_000;

// The most a steady stream of writes can hold one show's refresh back. A scan
// or a landing batch can write rows for minutes on end; the page still shows
// the writes within this bound of the first one.
export const EPISODES_HISTORY_MAX_WAIT_MS = 5_000;

const pendingSocketRefreshes = new WeakMap<
  QueryClient,
  Map<number, { timer: ReturnType<typeof setTimeout>; firstAt: number }>
>();

// The socket's own history refresh: one trailing refresh per show, bounded by
// the first event's wait so a stream of writes cannot hold it back forever.
// Mutations refresh through invalidateEpisodeHistory instead, which does not
// make the user wait a second for an answer they just asked for.
export function scheduleEpisodesHistoryRefresh(
  client: QueryClient,
  seriesId: number,
) {
  let refreshes = pendingSocketRefreshes.get(client);
  if (refreshes === undefined) {
    refreshes = new Map();
    pendingSocketRefreshes.set(client, refreshes);
  }
  const now = Date.now();
  const firstAt = refreshes.get(seriesId)?.firstAt ?? now;
  const dueAt = Math.min(
    now + EPISODES_HISTORY_DEBOUNCE_MS,
    firstAt + EPISODES_HISTORY_MAX_WAIT_MS,
  );
  const pending = refreshes.get(seriesId);
  if (pending !== undefined) {
    clearTimeout(pending.timer);
  }
  const timer = setTimeout(
    () => {
      refreshes.delete(seriesId);
      void invalidateEpisodesHistory(client, seriesId);
    },
    Math.max(dueAt - now, 0),
  );
  refreshes.set(seriesId, { timer, firstAt });
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
