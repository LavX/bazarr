import {
  hideNotification,
  showNotification,
  updateNotification,
} from "@mantine/notifications";
import { isArray, isEmpty, isNumber } from "lodash";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import { notification } from "@/modules/task";
import { LOG } from "@/utilities/console";
import { setOnlineStatus } from "@/utilities/event";

export function createDefaultReducer(): SocketIO.Reducer[] {
  return [
    {
      key: "connect",
      any: () => setOnlineStatus(true),
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
      key: "message",
      update: (msg) => {
        msg
          .map((message) => notification.info("Notification", message))
          .forEach((data) => showNotification(data));
      },
    },
    {
      key: "progress",
      update: (items) => {
        items.forEach((item) => {
          // Ensure the notification exists before updating it. showNotification
          // is a no-op when a notification with this id is already displayed.
          showNotification(notification.progress.pending(item.id, item.header));

          if (item.value >= item.count) {
            updateNotification(notification.progress.end(item.id, item.header));
          } else {
            updateNotification(
              notification.progress.update(
                item.id,
                item.header,
                item.name,
                item.value,
                item.count,
              ),
            );
          }
        });
      },
      delete: (ids) => {
        // hide_progress fires when a job finishes or is cancelled. Give the
        // user a moment to read the final state before closing.
        setTimeout(
          () => ids.forEach((id) => hideNotification(id)),
          notification.PROGRESS_TIMEOUT,
        );
      },
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
        // Invalidate series list so Missing Subtitles column refreshes
        void queryClient.invalidateQueries({
          queryKey: [QueryKeys.Series],
        });
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
        // Invalidate movies list so Missing Subtitles column refreshes
        void queryClient.invalidateQueries({
          queryKey: [QueryKeys.Movies],
        });
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
      },
    },
    {
      key: "episode",
      // Multi-instance note (#156): the backend now emits the LOCAL episode id
      // here (subtitles/indexer/series.py), which is the id the episode cache is
      // keyed by ([QueryKeys.Episodes, <local id>]). The getQueryData lookup
      // below therefore resolves the right series_id for non-default instances;
      // when the episode isn't cached we fall back to invalidating all series.
      update: (ids) => {
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
            });
          }
        });
      },
      delete: (ids) => {
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
      any: () => {
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
          // Payload is always a JSON string:
          // {"job_id": <number>, "progress_value": <number|null>, "progress_message": <string>, "status": <string>}
          // If progress_value is present (not null/undefined), apply (with progress_message and status) directly to
          // cache without an API call
          if (isNumber(payload.progress_value)) {
            const current = queryClient.getQueryData<LooseObject[]>(keys) || [];
            const idx = current.findIndex((j) => j.job_id === payload.job_id);

            const initialJob =
              // eslint-disable-next-line camelcase
              idx >= 0 ? { ...current[idx] } : { job_id: payload.job_id };

            const updatedJob = {
              ...initialJob,
              status: payload.status,
              /* eslint-disable camelcase */
              progress_value: payload.progress_value,
              progress_max: payload.progress_max,
              progress_message: payload.progress_message,
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
              const incoming = incomingJobs[0];

              const current =
                queryClient.getQueryData<LooseObject[]>(keys) || [];

              const idx = current.findIndex(
                (j) => j.job_id === incoming.job_id,
              );
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
            })
            .catch((e: unknown) => {
              LOG("warning", "Failed to fetch job update", payload.job_id, e);
            });
        });
      },
    },
  ];
}
