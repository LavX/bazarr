import { isArray, isEmpty, isNumber } from "lodash";
import queryClient from "@/apis/queries";
import { scheduleEpisodesHistoryRefresh } from "@/apis/queries/episodeHistory";
import { QueryKeys } from "@/apis/queries/keys";
import {
  scheduleSubtitleScoresRefresh,
  skipScoredRangeQueries,
} from "@/apis/queries/subtitleScores";
import api from "@/apis/raw";
import type { HiddenTrackMediaType } from "@/apis/raw/subtitles";
import { notifyJobOutcome, observeJobSession } from "@/modules/jobs";
import { LOG } from "@/utilities/console";
import { setOnlineStatus } from "@/utilities/event";

// A hide or unhide re-indexes the row, and the event that refreshes the row
// refreshes the hidden embedded tracks of that media type too. Another tab
// then shows the Hidden tracks disclosure together with the row it explains.
function invalidateHiddenTracks(mediaType: HiddenTrackMediaType) {
  void queryClient.invalidateQueries({
    queryKey: [QueryKeys.Subtitles, QueryKeys.EmbeddedHidden, mediaType],
  });
}

const JOBS_KEY = [QueryKeys.System, QueryKeys.Jobs];

// A job is its id within one backend run. A row from another run only shares
// the id by chance, so it is never the one an event is about.
function isSameJob(
  row: LooseObject,
  job: { job_id: number; session?: string },
) {
  return (
    row.job_id === job.job_id &&
    (row.session === undefined ||
      job.session === undefined ||
      row.session === job.session)
  );
}

function refreshJobsForSession(session: string | undefined) {
  if (session !== undefined && observeJobSession(session) === "changed") {
    void queryClient.resetQueries({ queryKey: JOBS_KEY, exact: true });
    return;
  }
  void queryClient.invalidateQueries({ queryKey: JOBS_KEY, exact: true });
}

export function createDefaultReducer(): SocketIO.Reducer[] {
  return [
    {
      key: "sports",
      any: () => {
        void queryClient.invalidateQueries({ queryKey: [QueryKeys.Sports] });
        void queryClient.invalidateQueries({ queryKey: [QueryKeys.Badges] });
        invalidateHiddenTracks("sports");
      },
    },
    {
      key: "connect",
      any: () => {
        // Whether this is the backend the page was talking to or a new run of
        // it is not known yet: the session event that follows says, and
        // decides what to keep.
        setOnlineStatus(true);
      },
    },
    {
      key: "session",
      // Sent after every connect with the backend run the socket reached. A
      // reconnect to the same run keeps what the page recorded about its jobs,
      // claims included, and reads the list again for what the gap missed.
      // Job ids restart with a new run, so a finished row cached before the
      // restart can carry the id of the next new job and settle its waiter
      // before that job has run: the list is dropped, and read again.
      // Without a session the answer is unknown, and the list is only
      // marked stale.
      update: (sessions) =>
        refreshJobsForSession(sessions[sessions.length - 1]),
    },
    {
      key: "connect_error",
      any: () => {
        setOnlineStatus(false);
      },
    },
    {
      key: "disconnect",
      any: () => setOnlineStatus(false),
    },
    {
      key: "series",
      // Multi-instance note (#156): the per-id invalidation below targets the
      // emitted payload id. The unconditional list-prefix invalidation
      // ([QueryKeys.Series]) is the cross-instance-safe refresh: it invalidates
      // every cached series detail regardless of id, so a non-default series'
      // detail page is refreshed even when the payload carries an upstream id.
      update: (ids) => {
        LOG("info", "Invalidating series", ids);
        ids.forEach((id) => {
          void queryClient.invalidateQueries({
            queryKey: [QueryKeys.Series, id],
          });
        });
        // Invalidate series list so Missing Subtitles column refreshes. The
        // scored range reads are left to the one debounced refresh, so a scan
        // emitting one event per row cannot restart the aggregation each time.
        scheduleSubtitleScoresRefresh(queryClient, QueryKeys.Series);
        void queryClient.invalidateQueries({
          queryKey: [QueryKeys.Series],
          predicate: skipScoredRangeQueries,
        });
        invalidateHiddenTracks("episode");
      },
      delete: (ids) => {
        LOG("info", "Invalidating series", ids);
        ids.forEach((id) => {
          void queryClient.invalidateQueries({
            queryKey: [QueryKeys.Series, id],
          });
        });
        void queryClient.invalidateQueries({
          queryKey: [QueryKeys.Series],
        });
        invalidateHiddenTracks("episode");
      },
    },
    {
      key: "movie",
      // Multi-instance note (#156): same as "series" - the list-prefix
      // invalidation ([QueryKeys.Movies]) is the cross-instance-safe refresh
      // that covers non-default movie detail pages regardless of the payload id.
      update: (ids) => {
        LOG("info", "Invalidating movies", ids);
        ids.forEach((id) => {
          void queryClient.invalidateQueries({
            queryKey: [QueryKeys.Movies, id],
          });
        });
        // Invalidate movies list so Missing Subtitles column refreshes. The
        // scored range reads refresh through the debounced scheduler instead.
        scheduleSubtitleScoresRefresh(queryClient, QueryKeys.Movies);
        void queryClient.invalidateQueries({
          queryKey: [QueryKeys.Movies],
          predicate: skipScoredRangeQueries,
        });
        invalidateHiddenTracks("movie");
      },
      delete: (ids) => {
        LOG("info", "Invalidating movies", ids);
        ids.forEach((id) => {
          void queryClient.invalidateQueries({
            queryKey: [QueryKeys.Movies, id],
          });
        });
        void queryClient.invalidateQueries({
          queryKey: [QueryKeys.Movies],
        });
        invalidateHiddenTracks("movie");
      },
    },
    {
      key: "episode",
      // Multi-instance note (#156): the backend now emits the LOCAL episode id
      // here (subtitles/indexer/series.py), which is the id the episode cache is
      // keyed by ([QueryKeys.Episodes, <local id>]). The getQueryData lookup
      // below therefore resolves the right series_id for non-default instances;
      // when the episode isn't cached we fall back to invalidating all series.
      // The history query is deliberately not touched here: episode rows change
      // without any history row changing, every history write emits its own
      // scoped event, and the indexer emits one event per episode, so a scan of
      // a large show would otherwise refetch the heaviest query on the page
      // hundreds of times over.
      update: (ids) => {
        // Current subtitle membership can change without a history write.
        scheduleSubtitleScoresRefresh(queryClient, QueryKeys.Series);
        // Currently invalidate episodes is impossible because we don't directly fetch episodes (we fetch episodes by series id)
        // So we need to invalidate series instead
        // TODO: Make a query for episodes and invalidate that instead
        LOG("info", "Invalidating episodes", ids);
        ids.forEach((id) => {
          const episode = queryClient.getQueryData<Item.Episode>([
            QueryKeys.Episodes,
            id,
          ]);
          if (episode !== undefined) {
            void queryClient.invalidateQueries({
              queryKey: [QueryKeys.Series, episode.series_id],
            });
          } else {
            void queryClient.invalidateQueries({
              queryKey: [QueryKeys.Series],
              predicate: skipScoredRangeQueries,
            });
          }
        });
      },
      delete: (ids) => {
        LOG("info", "Invalidating episodes", ids);
        scheduleSubtitleScoresRefresh(queryClient, QueryKeys.Series);
        ids.forEach((id) => {
          const episode = queryClient.getQueryData<Item.Episode>([
            QueryKeys.Episodes,
            id,
          ]);
          if (episode !== undefined) {
            void queryClient.invalidateQueries({
              queryKey: [QueryKeys.Series, episode.series_id],
            });
          } else {
            void queryClient.invalidateQueries({
              queryKey: [QueryKeys.Series],
              predicate: skipScoredRangeQueries,
            });
          }
        });
      },
    },
    {
      key: "episode-wanted",
      update: () => {
        // Find a better way to update wanted
        void queryClient.invalidateQueries({
          queryKey: [QueryKeys.Series, QueryKeys.Wanted],
        });
      },
      delete: () => {
        void queryClient.invalidateQueries({
          queryKey: [QueryKeys.Series, QueryKeys.Wanted],
        });
      },
    },
    {
      key: "movie-wanted",
      update: () => {
        // Find a better way to update wanted
        void queryClient.invalidateQueries({
          queryKey: [QueryKeys.Movies, QueryKeys.Wanted],
        });
      },
      delete: () => {
        void queryClient.invalidateQueries({
          queryKey: [QueryKeys.Movies, QueryKeys.Wanted],
        });
      },
    },
    {
      key: "settings",
      any: () => {
        void queryClient.invalidateQueries({ queryKey: [QueryKeys.System] });
      },
    },
    {
      key: "languages",
      any: () => {
        void queryClient.invalidateQueries({
          queryKey: [QueryKeys.System, QueryKeys.Languages],
        });
      },
    },
    {
      key: "badges",
      any: () => {
        void queryClient.invalidateQueries({
          queryKey: [QueryKeys.System, QueryKeys.Badges],
        });
      },
    },
    {
      key: "backup",
      any: () => {
        void queryClient.invalidateQueries({
          queryKey: [QueryKeys.System, QueryKeys.Backups],
        });
      },
    },
    {
      key: "movie-history",
      any: () => {
        scheduleSubtitleScoresRefresh(queryClient, QueryKeys.Movies);
        void queryClient.invalidateQueries({
          queryKey: [QueryKeys.Movies, QueryKeys.History],
        });
      },
    },
    {
      key: "movie-blacklist",
      any: () => {
        void queryClient.invalidateQueries({
          queryKey: [QueryKeys.Movies, QueryKeys.Blacklist],
        });
      },
    },
    {
      key: "episode-history",
      // New history writes carry the canonical local series id. Legacy events
      // without a payload still refresh the general history page via `any`.
      update: (seriesIds) => {
        seriesIds.forEach((seriesId) => {
          scheduleEpisodesHistoryRefresh(queryClient, seriesId);
        });
      },
      any: () => {
        scheduleSubtitleScoresRefresh(queryClient, QueryKeys.Series);
        void queryClient.invalidateQueries({
          queryKey: [QueryKeys.Series, QueryKeys.Episodes, QueryKeys.History],
        });
      },
    },
    {
      key: "episode-blacklist",
      any: () => {
        void queryClient.invalidateQueries({
          queryKey: [QueryKeys.Series, QueryKeys.Episodes, QueryKeys.Blacklist],
        });
      },
    },
    {
      key: "reset-episode-wanted",
      any: () => {
        void queryClient.invalidateQueries({
          queryKey: [QueryKeys.Series, QueryKeys.Wanted],
        });
      },
    },
    {
      key: "reset-movie-wanted",
      any: () => {
        void queryClient.invalidateQueries({
          queryKey: [QueryKeys.Movies, QueryKeys.Wanted],
        });
      },
    },
    {
      key: "task",
      any: () => {
        void queryClient.invalidateQueries({
          queryKey: [QueryKeys.System, QueryKeys.Tasks],
        });
      },
    },
    {
      key: "jobs",
      update: (items) => {
        const keys = [QueryKeys.System, QueryKeys.Jobs];
        const MAX_JOBS_IN_CACHE = 100;

        items.forEach((payload) => {
          // A job event names the backend run it came from. One from a run the
          // page has not seen means a restart the connect handshake missed.
          if (payload.session !== undefined) {
            if (observeJobSession(payload.session) === "changed") {
              void queryClient.resetQueries({ queryKey: keys, exact: true });
            }
          }
          // Payload is always a JSON string:
          // {"job_id": <number>, "progress_value": <number|null>, "progress_message": <string>, "status": <string>}
          // If progress_value is present (not null/undefined), apply (with progress_message and status) directly to
          // cache without an API call
          if (isNumber(payload.progress_value)) {
            const current = queryClient.getQueryData<LooseObject[]>(keys) || [];
            const idx = current.findIndex((j) => isSameJob(j, payload));

            const initialJob: LooseObject =
              // eslint-disable-next-line camelcase
              idx >= 0 ? { ...current[idx] } : { job_id: payload.job_id };

            const updatedJob = {
              ...initialJob,
              status: payload.status,
              /* eslint-disable camelcase */
              progress_value: payload.progress_value,
              progress_max: payload.progress_max,
              progress_message: payload.progress_message,
              session: payload.session ?? initialJob.session,
              /* eslint-enable camelcase */
            };

            const next =
              idx >= 0
                ? [
                    ...current.slice(0, idx),
                    updatedJob,
                    ...current.slice(idx + 1),
                  ]
                : [...current, updatedJob];

            // Prevent memory leak: keep only the most recent jobs
            const trimmed =
              next.length > MAX_JOBS_IN_CACHE
                ? next.slice(-MAX_JOBS_IN_CACHE)
                : next;

            queryClient.setQueryData(keys, trimmed);
            LOG(
              "info",
              "Applied inline payload content to cache",
              payload.job_id,
            );
            return;
          }

          // progress_value is null/undefined -> refresh this job via API
          LOG(
            "info",
            "progress_value missing; fetching job from API",
            payload.job_id,
          );
          void api.system
            .jobs(payload.job_id)
            .then((resp: LooseObject[] | undefined) => {
              const incomingJobs = isArray(resp) ? resp : [];
              if (isEmpty(incomingJobs)) {
                return;
              }
              const incoming = incomingJobs[0] as System.Jobs;

              const current =
                queryClient.getQueryData<LooseObject[]>(keys) || [];

              const idx = current.findIndex((j) => isSameJob(j, incoming));
              const next =
                idx >= 0
                  ? [
                      ...current.slice(0, idx),
                      { ...current[idx], ...incoming },
                      ...current.slice(idx + 1),
                    ]
                  : [...current, incoming];

              // Prevent memory leak: keep only the most recent jobs
              const trimmed =
                next.length > MAX_JOBS_IN_CACHE
                  ? next.slice(-MAX_JOBS_IN_CACHE)
                  : next;

              queryClient.setQueryData(keys, trimmed);
              // The terminal event is the one place a finish is announced.
              if (
                payload.status === "completed" ||
                payload.status === "failed"
              ) {
                notifyJobOutcome(incoming as System.Jobs);
              }
            })
            .catch((e: unknown) => {
              LOG("warning", "Failed to fetch job update", payload.job_id, e);
              // The cached row still says what it said before the event, and
              // everything following the job reads it. Mark the list stale
              // so its observers read it again.
              void queryClient.invalidateQueries({
                queryKey: keys,
                exact: true,
              });
            });
        });
      },
    },
  ];
}
