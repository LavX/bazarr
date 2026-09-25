import { showNotification } from "@mantine/notifications";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import type { ProviderHubInstallRequest } from "@/apis/raw/providerHub";
import { notification } from "@/modules/notification";
import { waitForJob } from "@/utilities/jobs";

const providerHubKey = [QueryKeys.ProviderHub];

export function useProviderHubCatalog() {
  return useQuery({
    queryKey: [...providerHubKey, QueryKeys.All],
    queryFn: () => api.providerHub.catalog(),
  });
}

export function useProviderHubProviders() {
  return useQuery({
    queryKey: providerHubKey,
    queryFn: () => api.providerHub.providers(),
    refetchOnMount: "always",
    refetchInterval: 10_000,
  });
}

export function useProviderHubJobs() {
  return useQuery({
    queryKey: [...providerHubKey, QueryKeys.Jobs],
    queryFn: () => api.providerHub.jobs(),
  });
}

export function useProviderHubRefreshCatalog() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [...providerHubKey, QueryKeys.Actions, "refresh-catalog"],
    mutationFn: async () => {
      const { job_id: jobId } = await api.providerHub.refreshCatalog();
      await waitForJob(client, jobId);
    },
    // Settled, not success: a refresh that failed for one source still
    // refreshed the others and recorded the failure on the source.
    onSettled: () => {
      client.invalidateQueries({ queryKey: providerHubKey });
    },
  });
}

export function useProviderHubAddCatalogSource() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [...providerHubKey, QueryKeys.Actions, "add-source"],
    mutationFn: (param: { name: string; url: string }) =>
      api.providerHub.addCatalogSource(param.name, param.url),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: providerHubKey });
    },
  });
}

export function useProviderHubRemoveCatalogSource() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [...providerHubKey, QueryKeys.Actions, "remove-source"],
    mutationFn: (name: string) => api.providerHub.removeCatalogSource(name),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: providerHubKey });
    },
  });
}

export function useProviderHubPatchCatalogSource() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [...providerHubKey, QueryKeys.Actions, "patch-source"],
    mutationFn: (param: { name: string; dev_ref?: string | null }) =>
      api.providerHub.patchCatalogSource(param.name, {
        dev_ref: param.dev_ref,
      }),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: providerHubKey });
    },
  });
}

export function useProviderHubInstall() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [...providerHubKey, QueryKeys.Actions, "install"],
    // Resolves when the install job has finished, not when it was queued, and
    // rejects with the job's reason when it failed.
    mutationFn: async ({ manifest }: ProviderHubInstallRequest) => {
      const { job_id: jobId } = await api.providerHub.install(manifest);
      await waitForJob(client, jobId);
    },
    onSettled: () => {
      client.invalidateQueries({ queryKey: providerHubKey });
    },
  });
}

export function useProviderHubInstallLocal() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [...providerHubKey, QueryKeys.Actions, "install-local"],
    mutationFn: async (file: File) => {
      const { job_id: jobId } = await api.providerHub.installLocal(file);
      await waitForJob(client, jobId);
    },
    onSettled: () => {
      client.invalidateQueries({ queryKey: providerHubKey });
    },
  });
}

export function useProviderHubUninstall() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [...providerHubKey, QueryKeys.Actions, "uninstall"],
    mutationFn: async (providerId: string) => {
      const { job_id: jobId } = await api.providerHub.uninstall(providerId);
      await waitForJob(client, jobId);
    },
    onSettled: () => {
      client.invalidateQueries({ queryKey: providerHubKey });
    },
  });
}

export function useProviderHubTest() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [...providerHubKey, QueryKeys.Actions, "test"],
    mutationFn: (providerId: string) => api.providerHub.test(providerId),
    onSuccess: (result) => {
      showNotification(
        result.ok
          ? notification.info("Provider test passed", result.message)
          : notification.warn("Provider test needs attention", result.message),
      );
      client.invalidateQueries({ queryKey: providerHubKey });
    },
    onError: (error) => {
      const message =
        error instanceof Error
          ? error.message
          : "Provider connection test failed";
      showNotification(notification.error("Provider test failed", message));
    },
  });
}

export function useProviderHubCheckUpdates() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [...providerHubKey, QueryKeys.Actions, "check-updates"],
    mutationFn: () => api.providerHub.checkUpdates(),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: providerHubKey });
    },
  });
}

export function useProviderHubApplyUpdate() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [...providerHubKey, QueryKeys.Actions, "apply-update"],
    mutationFn: async (providerId: string) => {
      const { job_id: jobId } = await api.providerHub.applyUpdate(providerId);
      await waitForJob(client, jobId);
    },
    // A failed update is recorded on the installation, so refresh either way.
    onSettled: () => {
      client.invalidateQueries({ queryKey: providerHubKey });
    },
  });
}
