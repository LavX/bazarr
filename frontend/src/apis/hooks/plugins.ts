import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import { inlineWhen } from "@/apis/raw/inlineWrites";
import type {
  PluginInstallRequest,
  PluginUpdateApplyRequest,
} from "@/apis/raw/plugins";
import { waitForJob } from "@/utilities/jobs";

const pluginsKey = [QueryKeys.Plugins];
/** The second part of the key of every consent read. */
export const CONSENT = "consent";

/**
 * An install or an update changes what the plugin routes list, what the Subtitle Hub lists, and the
 * engines the Translation page offers, so all three are asked again once one has settled.
 */
function useRefreshAfterChange() {
  const client = useQueryClient();
  return () => {
    // What a dialog is showing is not asked again behind it: it was read for one set, and the dialog
    // asks again itself when the server says that set changed.
    void client.invalidateQueries({
      queryKey: pluginsKey,
      predicate: (query) => query.queryKey[1] !== CONSENT,
    });
    void client.invalidateQueries({ queryKey: [QueryKeys.ProviderHub] });
    void client.invalidateQueries({ queryKey: [QueryKeys.Translation] });
  };
}

/** The catalog, all of it, or one kind of it. */
export function usePluginCatalog(kind?: string) {
  return useQuery({
    queryKey: [...pluginsKey, "catalog", kind ?? QueryKeys.All],
    queryFn: () => api.plugins.catalog(kind),
  });
}

export function usePluginInstallations(kind?: string) {
  return useQuery({
    queryKey: [...pluginsKey, "installations", kind ?? QueryKeys.All],
    queryFn: () => api.plugins.installations(kind),
    refetchOnMount: "always",
    refetchInterval: 10_000,
  });
}

/** One installation, for the dialog that shows what an update changes in it. Null asks for nothing. */
export function usePluginInstallation(id: string | null) {
  return useQuery({
    queryKey: [...pluginsKey, "installation", id],
    queryFn: () => api.plugins.installation(id!),
    enabled: id !== null,
    retry: false,
    refetchOnWindowFocus: false,
  });
}

export function usePluginJobs() {
  return useQuery({
    queryKey: [...pluginsKey, QueryKeys.Jobs],
    queryFn: () => api.plugins.jobs(),
  });
}

/** What installing the entry and everything it needs would add. Asked once per set, never refetched behind the dialog. */
export function usePluginInstallConsent(
  request: { source: string; id: string; version: string } | null,
) {
  return useQuery({
    queryKey: [...pluginsKey, CONSENT, "install", request],
    queryFn: () =>
      api.plugins.installConsent(
        request!.source,
        request!.id,
        request!.version,
      ),
    enabled: request !== null,
    staleTime: Infinity,
    gcTime: 0,
    retry: false,
    refetchOnWindowFocus: false,
  });
}

export function usePluginUpdateConsent(id: string | null) {
  return useQuery({
    queryKey: [...pluginsKey, CONSENT, "update", id],
    queryFn: () => api.plugins.updateConsent(id!),
    enabled: id !== null,
    staleTime: Infinity,
    gcTime: 0,
    retry: false,
    refetchOnWindowFocus: false,
  });
}

export function usePluginRefreshCatalog() {
  const refresh = useRefreshAfterChange();
  const client = useQueryClient();
  return useMutation({
    mutationKey: [...pluginsKey, QueryKeys.Actions, "refresh-catalog"],
    mutationFn: async () => {
      const { job_id: jobId } = await api.plugins.refreshCatalog();
      await waitForJob(client, jobId);
    },
    onSettled: refresh,
  });
}

export function usePluginCheckUpdates() {
  const refresh = useRefreshAfterChange();
  return useMutation({
    mutationKey: [...pluginsKey, QueryKeys.Actions, "check-updates"],
    mutationFn: () => api.plugins.checkUpdates(),
    onSuccess: refresh,
  });
}

/**
 * Resolves when the install job has finished and rejects with the request's refusal or the job's
 * reason. The caller reports it next to its own control.
 */
export function usePluginInstall({
  inlineErrors = false,
}: { inlineErrors?: boolean } = {}) {
  const client = useQueryClient();
  const refresh = useRefreshAfterChange();
  return useMutation({
    mutationKey: [...pluginsKey, QueryKeys.Actions, "install"],
    mutationFn: inlineWhen(
      inlineErrors,
      async (request: PluginInstallRequest) => {
        const { job_id: jobId } = await api.plugins.install(request);
        await waitForJob(client, jobId);
      },
    ),
    onSettled: refresh,
  });
}

export function usePluginApplyUpdate({
  inlineErrors = false,
}: { inlineErrors?: boolean } = {}) {
  const client = useQueryClient();
  const refresh = useRefreshAfterChange();
  return useMutation({
    mutationKey: [...pluginsKey, QueryKeys.Actions, "apply-update"],
    mutationFn: inlineWhen(
      inlineErrors,
      async (request: PluginUpdateApplyRequest) => {
        const { job_id: jobId } = await api.plugins.applyUpdate(request);
        await waitForJob(client, jobId);
      },
    ),
    // A failed update is recorded on the installation, so the lists are read again either way.
    onSettled: refresh,
  });
}
