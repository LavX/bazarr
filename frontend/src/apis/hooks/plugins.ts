import {
  QueryClient,
  useMutation,
  useQueries,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import { inlineWhen, withInlineErrors } from "@/apis/raw/inlineWrites";
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

/**
 * Waits for a job the way every plugin action does, after the list of jobs has been read again, so the
 * cards say what the job is doing from its first moment and not only once it is over.
 */
function waitForPluginJob(
  client: QueryClient,
  jobId: Parameters<typeof waitForJob>[1],
) {
  void client.invalidateQueries({ queryKey: [...pluginsKey, QueryKeys.Jobs] });
  return waitForJob(client, jobId);
}

/** The catalog, all of it, or one kind of it. */
export function usePluginCatalog(kind?: string) {
  return useQuery({
    queryKey: [...pluginsKey, "catalog", kind ?? QueryKeys.All],
    queryFn: () => api.plugins.catalog(kind),
  });
}

export function usePluginInstallations(kind?: string, enabled = true) {
  return useQuery({
    queryKey: [...pluginsKey, "installations", kind ?? QueryKeys.All],
    queryFn: () => api.plugins.installations(kind),
    enabled,
    refetchOnMount: "always",
    refetchInterval: 10_000,
  });
}

/** One installation, for the dialog that shows what an update changes in it. Null asks for nothing. */
export function usePluginInstallation(id: string | null) {
  return useQuery({
    queryKey: [...pluginsKey, "installation", id],
    queryFn: () => withInlineErrors(() => api.plugins.installation(id!)),
    enabled: id !== null,
    retry: false,
    refetchOnWindowFocus: false,
  });
}

/** A job that has not finished. */
export const jobIsOpen = (job: { state?: string }) =>
  job.state === "pending" || job.state === "running";

export function usePluginJobs() {
  return useQuery({
    queryKey: [...pluginsKey, QueryKeys.Jobs],
    queryFn: () => api.plugins.jobs(),
    // Cards say "Installing" or "Rebuilding" while a job runs, so the list is read again until it ends.
    // A slow read while nothing is open finds a job somebody else started.
    refetchInterval: (query) =>
      query.state.data?.some(jobIsOpen) ? 3_000 : 15_000,
  });
}

/** The recent output of one plugin. Asked only while a person has the log open. */
export function usePluginLog(id: string | null) {
  return useQuery({
    queryKey: [...pluginsKey, "log", id],
    queryFn: () => api.plugins.log(id!),
    enabled: id !== null,
    gcTime: 0,
    refetchInterval: 5_000,
  });
}

/** What installing the entry and everything it needs would add. Asked once per set, never refetched behind the dialog. */
export function usePluginInstallConsent(
  request: { source: string; id: string; version: string } | null,
) {
  return useQuery({
    queryKey: [...pluginsKey, CONSENT, "install", request],
    // The dialog says why a read was refused, so the client's own toast would say it twice.
    queryFn: () =>
      withInlineErrors(() =>
        api.plugins.installConsent(
          request!.source,
          request!.id,
          request!.version,
        ),
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
    queryFn: () => withInlineErrors(() => api.plugins.updateConsent(id!)),
    enabled: id !== null,
    staleTime: Infinity,
    gcTime: 0,
    retry: false,
    refetchOnWindowFocus: false,
  });
}

/**
 * Which of these plugins have an update that is held for the person's agreement: the server's own
 * summary of what the update adds says it needs consent. Read once per plugin and read again after any
 * install or update, because it is kept outside the keys a dialog reads under. A plugin whose summary
 * could not be read is left out, never guessed at.
 */
export function usePluginUpdatesNeedingConsent(ids: string[]): Set<string> {
  const results = useQueries({
    queries: ids.map((id) => ({
      queryKey: [...pluginsKey, "update-needs-consent", id],
      queryFn: () => withInlineErrors(() => api.plugins.updateConsent(id)),
      select: (delta: { needs_consent: boolean }) => delta.needs_consent,
      staleTime: 60_000,
      retry: false,
      refetchOnWindowFocus: false,
    })),
  });
  const needing = new Set<string>();
  ids.forEach((id, index) => {
    if (results[index]?.data === true) {
      needing.add(id);
    }
  });
  return needing;
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
        await waitForPluginJob(client, jobId);
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
        await waitForPluginJob(client, jobId);
      },
    ),
    // A failed update is recorded on the installation, so the lists are read again either way.
    onSettled: refresh,
  });
}

/** Turn a plugin on or off. A refusal (a plugin that needs it is on) is reported by the caller. */
export function usePluginSetEnabled() {
  const refresh = useRefreshAfterChange();
  return useMutation({
    mutationKey: [...pluginsKey, QueryKeys.Actions, "set-enabled"],
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      withInlineErrors(() => api.plugins.setEnabled(id, enabled)),
    onSettled: refresh,
  });
}

/** Rebuild the plugin's environment. Resolves when the job has finished and rejects with its reason. */
export function usePluginRepair() {
  const client = useQueryClient();
  const refresh = useRefreshAfterChange();
  return useMutation({
    mutationKey: [...pluginsKey, QueryKeys.Actions, "repair"],
    mutationFn: async (id: string) => {
      const { job_id: jobId } = await withInlineErrors(() =>
        api.plugins.repair(id),
      );
      await waitForPluginJob(client, jobId);
    },
    onSettled: refresh,
  });
}

export function usePluginRemove() {
  const client = useQueryClient();
  const refresh = useRefreshAfterChange();
  return useMutation({
    mutationKey: [...pluginsKey, QueryKeys.Actions, "remove"],
    mutationFn: async ({ id, keepData }: { id: string; keepData: boolean }) => {
      const { job_id: jobId } = await withInlineErrors(() =>
        api.plugins.remove(id, keepData),
      );
      await waitForPluginJob(client, jobId);
    },
    onSettled: refresh,
  });
}

export function usePluginDismissReview() {
  const refresh = useRefreshAfterChange();
  return useMutation({
    mutationKey: [...pluginsKey, QueryKeys.Actions, "dismiss-review"],
    mutationFn: (id: string) => api.plugins.dismissReview(id),
    onSettled: refresh,
  });
}
