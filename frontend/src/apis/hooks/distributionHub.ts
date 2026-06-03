import { showNotification } from "@mantine/notifications";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import type {
  DistKeyCreateRequest,
  DistKeyUpdateRequest,
  DistSettings,
  DistTier,
} from "@/apis/raw/distributionHub";

const distKey = [QueryKeys.DistributionHub];

export function useDistKeys() {
  return useQuery({
    queryKey: [...distKey, "keys"],
    queryFn: () => api.distributionHub.keys(),
  });
}

export function useDistCreateKey() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [...distKey, QueryKeys.Actions, "create-key"],
    mutationFn: (body: DistKeyCreateRequest) =>
      api.distributionHub.createKey(body),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: distKey });
    },
    onError: () =>
      showNotification({ color: "red", message: "Failed to create API key" }),
  });
}

export function useDistUpdateKey() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [...distKey, QueryKeys.Actions, "update-key"],
    mutationFn: ({ id, body }: { id: number; body: DistKeyUpdateRequest }) =>
      api.distributionHub.updateKey(id, body),
    onSettled: () => {
      client.invalidateQueries({ queryKey: distKey });
    },
  });
}

export function useDistDeleteKey() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [...distKey, QueryKeys.Actions, "delete-key"],
    mutationFn: (id: number) => api.distributionHub.deleteKey(id),
    onSettled: () => {
      client.invalidateQueries({ queryKey: distKey });
    },
  });
}

export function useDistRotateKey() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [...distKey, QueryKeys.Actions, "rotate-key"],
    mutationFn: (id: number) => api.distributionHub.rotateKey(id),
    onSettled: () => {
      client.invalidateQueries({ queryKey: distKey });
    },
  });
}

export function useDistTiers() {
  return useQuery({
    queryKey: [...distKey, "tiers"],
    queryFn: () => api.distributionHub.tiers(),
  });
}

export function useDistSaveTiers() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [...distKey, QueryKeys.Actions, "save-tiers"],
    mutationFn: (body: {
      default_tier: string;
      tiers: Record<string, DistTier>;
    }) => api.distributionHub.saveTiers(body),
    onSuccess: () => {
      showNotification({ color: "green", message: "Tiers saved" });
      client.invalidateQueries({ queryKey: distKey });
    },
    onError: () =>
      showNotification({ color: "red", message: "Failed to save tiers" }),
  });
}

export function useDistStatsOverview() {
  return useQuery({
    queryKey: [...distKey, "stats", "overview"],
    queryFn: () => api.distributionHub.statsOverview(),
  });
}

export function useDistStatsTimeseries(params: {
  range_days?: number;
  key_id?: number;
}) {
  return useQuery({
    queryKey: [...distKey, "stats", "timeseries", params],
    queryFn: () => api.distributionHub.statsTimeseries(params),
  });
}

export function useDistProviders() {
  return useQuery({
    queryKey: [...distKey, "providers"],
    queryFn: () => api.distributionHub.providers(),
  });
}

export function useDistSettings() {
  return useQuery({
    queryKey: [...distKey, "settings"],
    queryFn: () => api.distributionHub.settings(),
  });
}

export function useDistSaveSettings() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [...distKey, QueryKeys.Actions, "save-settings"],
    mutationFn: (body: Partial<DistSettings>) =>
      api.distributionHub.saveSettings(body),
    onSuccess: () => {
      showNotification({ color: "green", message: "Settings saved" });
      client.invalidateQueries({ queryKey: distKey });
    },
    onError: () =>
      showNotification({ color: "red", message: "Failed to save settings" }),
  });
}

export function useDistRegenerate() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [...distKey, QueryKeys.Actions, "regenerate"],
    mutationFn: () => api.distributionHub.regenerate(),
    onSuccess: () => {
      showNotification({
        color: "green",
        message: "Secrets regenerated. The Default key now uses the new token.",
      });
      client.invalidateQueries({ queryKey: distKey });
    },
    onError: () =>
      showNotification({
        color: "red",
        message: "Failed to regenerate secrets",
      }),
  });
}
