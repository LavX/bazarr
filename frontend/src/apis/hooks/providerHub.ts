import { showNotification } from "@mantine/notifications";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import type { ProviderHubInstallRequest } from "@/apis/raw/providerHub";
import { notification } from "@/modules/task";

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
    mutationFn: () => api.providerHub.refreshCatalog(),
    onSuccess: () => {
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
    mutationFn: ({ manifest }: ProviderHubInstallRequest) =>
      api.providerHub.install(manifest),
    onSettled: () => {
      client.invalidateQueries({ queryKey: providerHubKey });
    },
  });
}

export function useProviderHubInstallLocal() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [...providerHubKey, QueryKeys.Actions, "install-local"],
    mutationFn: (file: File) => api.providerHub.installLocal(file),
    onSettled: () => {
      client.invalidateQueries({ queryKey: providerHubKey });
    },
  });
}

export function useProviderHubUninstall() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [...providerHubKey, QueryKeys.Actions, "uninstall"],
    mutationFn: (providerId: string) => api.providerHub.uninstall(providerId),
    onSuccess: () => {
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
    mutationFn: (providerId: string) => api.providerHub.applyUpdate(providerId),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: providerHubKey });
    },
  });
}
