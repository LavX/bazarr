import { QueryClient } from "@tanstack/react-query";
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
        const filters = {
          queryKey: [media, QueryKeys.Range, { scores: true }],
        };
        // Invalidating alone reuses an initial request that may have read the
        // history before this write. Cancel it before requesting a fresh view.
        await client.cancelQueries(filters);
        await client.invalidateQueries(filters);
      },
      1_000,
      { maxWait: 5_000 },
    );
    scheduled.set(media, refresh);
  }
  void refresh();
}
