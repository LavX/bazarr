import { useCallback, useMemo } from "react";
import { showNotification } from "@mantine/notifications";
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { isAxiosError } from "axios";
import { mediaServerLinksKey } from "@/apis/hooks/mediaServerLinks";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import { inlineWhen } from "@/apis/raw/inlineWrites";
import { notification } from "@/modules/notification";
import { Environment } from "@/utilities";
import { setAuthenticated } from "@/utilities/event";
import { JobFailedError, JobStoppedError, waitForJob } from "@/utilities/jobs";

export function useBadges() {
  return useQuery({
    queryKey: [QueryKeys.System, QueryKeys.Badges],
    queryFn: () => api.badges.all(),
    refetchOnWindowFocus: "always",
    refetchInterval: 1000 * 60,
    staleTime: 1000 * 10,
  });
}

export function useFileSystem(
  type: "bazarr" | "sonarr" | "radarr" | "sportarr",
  path: string,
  enabled: boolean,
  // instanceId (#156) routes a sonarr/radarr/sportarr browse at the owning
  // instance's server. Undefined => default server (the legacy
  // single-instance behaviour, which for sportarr means the default instance).
  instanceId?: number,
) {
  return useQuery({
    queryKey: [QueryKeys.FileSystem, type, path, instanceId],

    queryFn: () => {
      if (type === "bazarr") {
        return api.files.bazarr(path);
      } else if (type === "radarr") {
        return api.files.radarr(path, instanceId);
      } else if (type === "sonarr") {
        return api.files.sonarr(path, instanceId);
      } else if (type === "sportarr") {
        return api.files.sportarr(path, instanceId);
      }

      return [];
    },

    enabled,
  });
}

export function useSystemSettings() {
  return useQuery({
    queryKey: [QueryKeys.System, QueryKeys.Settings],
    queryFn: () => api.system.settings(),
    staleTime: Infinity,
  });
}

export function useSystemJobs() {
  return useQuery({
    queryKey: [QueryKeys.System, QueryKeys.Jobs],
    queryFn: () => api.system.jobs(),
    staleTime: Infinity,
  });
}

export function isMetadataFollowupError(error: unknown): boolean {
  return (
    isAxiosError(error) &&
    error.response?.status === 503 &&
    error.response.data?.code === "discover_settings_refresh_failed"
  );
}

// The settings reached the disk, and only what the backend does after the write
// failed, such as resetting a provider login or queueing a library job.
export function isSettingsFollowupError(error: unknown): boolean {
  return (
    isAxiosError(error) &&
    error.response?.status === 503 &&
    error.response.data?.code === "settings_refresh_failed"
  );
}

interface SettingsMutationOptions {
  // The caller says a failed write next to the control that made it, so
  // neither this hook's "Save failed" toast nor the client's "Error <code>"
  // one is shown. Both are announced as alerts, and
  // the setup wizard read every failure out twice. A write that landed but
  // could not be applied still toasts: that is news the caller does not have.
  inlineErrors?: boolean;
}

export function useSettingsMutation(
  retryingMetadataRefresh = false,
  { inlineErrors = false }: SettingsMutationOptions = {},
) {
  const client = useQueryClient();
  const retireMetadata = (changes: LooseObject, retrying = false) => {
    const names = Object.keys(changes);
    const tmdbChanged =
      retrying ||
      names.some(
        (key) =>
          key.startsWith("settings-discover-") ||
          key === "settings-general-metadata_language",
      );
    const omdbChanged = names.includes("settings-omdb-apikey");
    if (!tmdbChanged && !omdbChanged) return;
    const filters = {
      queryKey: [QueryKeys.Discover, "metadata"],
      predicate: (query: { queryKey: readonly unknown[] }) => {
        const source = query.queryKey[6];
        if (source === "local") return false;
        if (source === "omdb") return omdbChanged;
        if (source === "all" || query.queryKey[2] === "fallback-configuration")
          return true;
        return tmdbChanged;
      },
    };
    void client.cancelQueries(filters);
    client.removeQueries(filters);
  };
  // Everything a written save can change, reloaded.
  const reloadSaved = (changes: LooseObject) => {
    retireMetadata(changes, retryingMetadataRefresh);
    void client.invalidateQueries({
      queryKey: [QueryKeys.System],
    });
    void client.invalidateQueries({
      queryKey: [QueryKeys.ProviderHub],
    });

    void client.invalidateQueries({
      queryKey: [QueryKeys.Series],
    });

    void client.invalidateQueries({
      queryKey: [QueryKeys.Episodes],
    });

    void client.invalidateQueries({
      queryKey: [QueryKeys.Movies],
    });

    void client.invalidateQueries({
      queryKey: [QueryKeys.Wanted],
    });

    // The sports wanted rows are computed live against the exclusion and
    // monitoring settings, so saving them refreshes the cached sports
    // queries (wanted included) the same way the ones above do for series
    // and movies.
    void client.invalidateQueries({
      queryKey: [QueryKeys.Sports],
    });

    void client.invalidateQueries({
      queryKey: [QueryKeys.Badges],
    });

    // Invalidate Plex libraries when settings change (e.g., server configuration)
    void client.invalidateQueries({
      queryKey: [QueryKeys.Plex, "libraries"],
    });

    // A media server kind's master switch decides whether a Play on menu
    // offers its destinations, and the path mappings which of them cover a
    // file. Settings are saved away from the library pages, so this only
    // has the next menu opened ask again.
    void client.invalidateQueries({ queryKey: mediaServerLinksKey });
  };
  return useMutation({
    mutationKey: [QueryKeys.System, QueryKeys.Settings],
    mutationFn: inlineWhen(inlineErrors, (data: LooseObject) =>
      api.system.updateSettings(data),
    ),

    onSuccess: (_, changes) => {
      reloadSaved(changes);
      showNotification(
        notification.info("Settings saved", "Your changes have been saved"),
      );
    },

    onError: (error, changes) => {
      if (isMetadataFollowupError(error) || retryingMetadataRefresh) {
        // Written as well, other settings in the same request included.
        reloadSaved(changes);
        showNotification(
          notification.error(
            "Settings saved; application refresh failed",
            "Your saved settings are kept. Retry application refresh or leave with the saved settings.",
          ),
        );
        return;
      }
      if (isSettingsFollowupError(error)) {
        reloadSaved(changes);
        showNotification(
          notification.error(
            "Settings saved; applying them failed",
            "Your changes are saved, but applying some of them failed. The system log has the details.",
          ),
        );
        return;
      }
      if (inlineErrors) {
        return;
      }
      showNotification(
        notification.error(
          "Save failed",
          "An error occurred while saving settings",
        ),
      );
    },
  });
}

export function useServerSearch(query: string, enabled: boolean) {
  return useQuery({
    queryKey: [QueryKeys.System, QueryKeys.Search, query],
    queryFn: () => api.system.search(query),
    enabled,
  });
}

export interface SystemLogsPage {
  page: number;
  pageSize: number;
  level?: System.LogLevel;
  contains?: string;
  baselineTotal?: number;
}

// One page of the log at a time. Pages count back from the newest entry, so
// only the newest page refreshes itself: it is a small read of the end of the
// file. An older page is read against the total paging started from, so the
// lines that arrive meanwhile do not shift its rows under the reader. The
// Refresh button still refreshes whatever is shown.
export function useSystemLogs({
  page,
  pageSize,
  level,
  contains,
  baselineTotal,
}: SystemLogsPage) {
  const newest = page === 0;
  return useQuery({
    queryKey: [
      QueryKeys.System,
      QueryKeys.Logs,
      { page, pageSize, level, contains, baselineTotal },
    ],
    queryFn: () =>
      api.system.logs({
        limit: pageSize,
        offset: page * pageSize,
        level,
        contains,
        baselineTotal,
      }),
    placeholderData: keepPreviousData,
    refetchOnWindowFocus: newest ? "always" : false,
    refetchInterval: newest ? 1000 * 60 : false,
    staleTime: 1000 * 10,
  });
}

export function useDeleteLogs() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [QueryKeys.System, QueryKeys.Logs],
    mutationFn: () => api.system.deleteLogs(),

    onSuccess: () => {
      void client.invalidateQueries({
        queryKey: [QueryKeys.System, QueryKeys.Logs],
      });
    },
  });
}

export function useSystemAnnouncements() {
  return useQuery({
    queryKey: [QueryKeys.System, QueryKeys.Announcements],
    queryFn: () => api.system.announcements(),
    refetchOnWindowFocus: "always",
    refetchInterval: 1000 * 60,
    staleTime: 1000 * 10,
  });
}

export function useSystemAnnouncementsAddDismiss() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [QueryKeys.System, QueryKeys.Announcements],

    mutationFn: (param: { hash: string }) => {
      const { hash } = param;
      return api.system.addAnnouncementsDismiss(hash);
    },

    onSuccess: () => {
      void client.invalidateQueries({
        queryKey: [QueryKeys.System, QueryKeys.Announcements],
      });

      void client.invalidateQueries({
        queryKey: [QueryKeys.System, QueryKeys.Badges],
      });
    },
  });
}

export function useSystemTasks() {
  return useQuery({
    queryKey: [QueryKeys.System, QueryKeys.Tasks],
    queryFn: () => api.system.tasks(),
    refetchOnWindowFocus: "always",
    refetchInterval: 1000 * 60,
    staleTime: 1000 * 10,
  });
}

export function useRunTask() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [QueryKeys.System, QueryKeys.Tasks],
    mutationFn: (id: string) => api.system.runTask(id),

    onSuccess: () => {
      void client.invalidateQueries({
        queryKey: [QueryKeys.System, QueryKeys.Tasks],
      });

      void client.invalidateQueries({
        queryKey: [QueryKeys.System, QueryKeys.Backups],
      });
    },
  });
}

export function useSystemBackups() {
  return useQuery({
    queryKey: [QueryKeys.System, "backups"],
    queryFn: () => api.system.backups(),
  });
}

export function useCreateBackups() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [QueryKeys.System, QueryKeys.Backups],
    mutationFn: () => api.system.createBackups(),

    onSuccess: () => {
      void client.invalidateQueries({
        queryKey: [QueryKeys.System, QueryKeys.Backups],
      });
    },
  });
}

export function useRestoreBackups() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [QueryKeys.System, QueryKeys.Backups],
    mutationFn: (filename: string) => api.system.restoreBackups(filename),

    onSuccess: (data) => {
      const message =
        data && typeof data === "object" && typeof data.message === "string"
          ? data.message
          : "Restore staged; Bazarr will restart to apply it";
      showNotification(notification.info("Backup restored", message));
      void client.invalidateQueries({
        queryKey: [QueryKeys.System, QueryKeys.Backups],
      });
    },
  });
}

export function useDeleteBackups() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [QueryKeys.System, QueryKeys.Backups],
    mutationFn: (filename: string) => api.system.deleteBackups(filename),

    onSuccess: () => {
      void client.invalidateQueries({
        queryKey: [QueryKeys.System, QueryKeys.Backups],
      });
    },
  });
}

export function useSystemStatus() {
  return useQuery({
    queryKey: [QueryKeys.System, "status"],
    queryFn: () => api.system.status(),
  });
}

/**
 * The configured translator's answer to "can a translation run", from the
 * system status. Treated as available while the status has not answered yet or
 * on responses that predate the field, so a loading page never disables a
 * working action.
 */
export function useTranslationAvailability(): System.TranslationAvailability {
  const { data } = useSystemStatus();
  return data?.translation_availability ?? { available: true, reason: "" };
}

/**
 * The editor's own gate answer, from the same status: the editor translates
 * through the AI Subtitle Translator service whatever the settings page has
 * selected, so this field answers for that engine and the editor's controls
 * match the 409 its endpoint gives. Treated as available while the status has
 * not answered yet or on responses that predate the field, so a loading page
 * never disables a working action.
 */
export function useEditorTranslationAvailability(): System.TranslationAvailability {
  const { data } = useSystemStatus();
  return (
    data?.editor_translation_availability ?? { available: true, reason: "" }
  );
}

/**
 * What a 409 on a translate endpoint owes the status its controls read: the
 * answer can be stale, so the next one is fetched before the user can try
 * again.
 */
export function useTranslationStatusRefetch(): () => void {
  const queryClient = useQueryClient();
  return useCallback(() => {
    void queryClient.invalidateQueries({
      queryKey: [QueryKeys.System, "status"],
    });
  }, [queryClient]);
}

/**
 * What a translate control learns from a 409 refusal. The status answer those
 * controls read is cached for a minute, and the gate can close elsewhere in
 * the meantime: another tab, another user, the config file. When that happens
 * the server answers 409 with its reason, and the shared client leaves that
 * status to the caller, so without this the refusal was invisible (the tools
 * menu's rejection was uncaught) or replaced with a generic sentence. The
 * same endpoints also answer 409 for refusals of their own, from a sports
 * target that is already satisfied to a translation that could not be
 * queued, so the title says only that nothing was queued and the reason
 * carries the server's answer. Show the reason, then refetch the status so
 * every gate-reading control flips to its disabled state. Returns whether the
 * error was a 409, so a caller can keep its own message for the rest.
 */
export function useTranslationRefusal(): (error: unknown) => boolean {
  const refetchStatus = useTranslationStatusRefetch();
  return useCallback(
    (error: unknown) => {
      if (!isAxiosError(error) || error.response?.status !== 409) {
        return false;
      }
      // The refusal's reason is the response's error field when the endpoint
      // answers with one, and the whole body when it answers with a bare
      // sentence, which is what the subtitles endpoint's (reason, 409)
      // arrives as.
      const data: unknown = error.response.data;
      const field =
        typeof data === "object" && data !== null
          ? (data as { error?: unknown }).error
          : undefined;
      const reason =
        typeof field === "string" && field
          ? field
          : typeof data === "string" && data
            ? data
            : error.message;
      showNotification(notification.error("Translation not queued", reason));
      refetchStatus();
      return true;
    },
    [refetchStatus],
  );
}

export function useSystemHealth() {
  return useQuery({
    queryKey: [QueryKeys.System, "health"],
    queryFn: () => api.system.health(),
  });
}

/**
 * Re-check health as a standard job. The request answers at once, so the list
 * is read again when the job has finished, which is what the button's spinner
 * waits for. A failed check is announced by the standard outcome, so only the
 * wait ends here.
 */
export function useRecheckHealth() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [QueryKeys.System, "health", "recheck"],
    mutationFn: async () => {
      const queued = await api.system.recheckHealth();
      try {
        await waitForJob(client, queued.job_id, queued.session);
      } catch (error) {
        if (
          !(error instanceof JobFailedError || error instanceof JobStoppedError)
        )
          throw error;
      } finally {
        await client.invalidateQueries({
          queryKey: [QueryKeys.System, "health"],
        });
      }
    },
  });
}

export function useSystemReleases() {
  return useQuery({
    queryKey: [QueryKeys.System, "releases"],
    queryFn: () => api.system.releases(),
  });
}

export function getPostLoginRedirectTarget(baseUrl = Environment.baseUrl) {
  return baseUrl || "/";
}

export function useSystem({
  inlineErrors = false,
}: {
  // The caller reports a failed action itself, so the client's toast for
  // that request stays away.
  inlineErrors?: boolean;
} = {}) {
  const client = useQueryClient();
  const { mutate: logout, isPending: isLoggingOut } = useMutation({
    mutationKey: [QueryKeys.System, QueryKeys.Actions],
    mutationFn: () => api.system.logout(),

    onSuccess: () => {
      setAuthenticated(false);
      // The cache is dropped one task later, on purpose. A clear that runs
      // here is observed by this mutation's own completion re-render, which
      // rebuilds every still-mounted query over the emptied cache and sends
      // its reads out again while the signed-in session is still in the
      // jar, and each answer re-issues a fresh login for the next page.
      // One task later the navigation above has already unmounted the app,
      // so nothing is left mounted to restart and the cache only refills
      // on the next sign-in.
      window.setTimeout(() => client.removeQueries(), 0);
    },
  });

  const { mutate: login, isPending: isLoggingIn } = useMutation({
    mutationKey: [QueryKeys.System, QueryKeys.Actions],

    mutationFn: (param: { username: string; password: string }) =>
      api.system.login(param.username, param.password),

    onSuccess: () => {
      // TODO: Hard-coded value
      window.location.replace(getPostLoginRedirectTarget());
    },
  });

  const { mutate: shutdown, isPending: isShuttingDown } = useMutation({
    mutationKey: [QueryKeys.System, QueryKeys.Actions],
    mutationFn: () => api.system.shutdown(),

    onSuccess: () => {
      client.clear();
    },
  });

  const { mutate: restart, isPending: isRestarting } = useMutation({
    mutationKey: [QueryKeys.System, QueryKeys.Actions],
    mutationFn: inlineWhen(inlineErrors, () => api.system.restart()),

    onSuccess: () => {
      client.clear();
    },
  });

  return useMemo(
    () => ({
      logout,
      shutdown,
      restart,
      login,
      isMutating: isLoggingOut || isShuttingDown || isRestarting || isLoggingIn,
    }),
    [
      isLoggingIn,
      isLoggingOut,
      isRestarting,
      isShuttingDown,
      login,
      logout,
      restart,
      shutdown,
    ],
  );
}

export function useSystemWebhookTestMutation() {
  return useMutation({
    mutationFn: () => api.system.testWebhook(),
  });
}
