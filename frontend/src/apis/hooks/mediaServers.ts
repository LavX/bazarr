/* eslint-disable camelcase */

import { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import api from "@/apis/raw";
import type {
  ConnectionOverrides,
  MediaServerKind,
  MediaServerUpdate,
} from "@/apis/raw/mediaServers";

const instancesKey = (kind: MediaServerKind) => [
  "media-servers",
  kind,
  "instances",
];
const itemKey = (kind: MediaServerKind, id: string) => [
  "media-servers",
  kind,
  id,
];
const statusKey = (kind: MediaServerKind, id: string) => [
  ...itemKey(kind, id),
  "status",
];

export function useMediaServerInstances(kind: MediaServerKind) {
  return useQuery({
    queryKey: instancesKey(kind),
    queryFn: () => api.mediaServers.list(kind),
    placeholderData: undefined,
  });
}

export function useSaveMediaServerInstance(
  kind: MediaServerKind,
  input: MediaServerUpdate,
  id?: string,
) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () =>
      id
        ? api.mediaServers.update(id, input)
        : api.mediaServers.create({
            ...input,
            kind,
            name: input.name ?? "",
            url: input.url ?? "",
          }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: instancesKey(kind) });
      if (id) void client.invalidateQueries({ queryKey: itemKey(kind, id) });
    },
  });
}

export function useDeleteMediaServerInstance(
  kind: MediaServerKind,
  id: string,
) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api.mediaServers.remove(id),
    onSuccess: async () => {
      await client.cancelQueries({ queryKey: itemKey(kind, id) });
      client.removeQueries({ queryKey: itemKey(kind, id) });
      void client.invalidateQueries({ queryKey: instancesKey(kind) });
    },
  });
}

export function useMediaServerTest(
  kind: MediaServerKind,
  input: ConnectionOverrides = {},
  id?: string,
) {
  const mutation = useMutation({
    mutationFn: () =>
      id
        ? api.mediaServers.testExisting(id, input)
        : api.mediaServers.testConnection(kind, {
            url: input.url ?? "",
            apikey: input.api_key ?? "",
            verify_ssl: input.verify_ssl ?? true,
          }),
  });
  const { reset } = mutation;
  useEffect(
    () => reset(),
    [
      kind,
      id,
      input.url,
      input.api_key,
      input.clear_api_key,
      input.verify_ssl,
      reset,
    ],
  );
  return mutation;
}

export function useSiloLibraries(input: ConnectionOverrides, id?: string) {
  const mutation = useMutation({
    mutationFn: () =>
      id
        ? api.mediaServers.librariesExisting(id, input)
        : api.mediaServers.libraries({
            url: input.url ?? "",
            apikey: input.api_key ?? "",
            verify_ssl: input.verify_ssl ?? true,
          }),
  });
  const { reset } = mutation;
  useEffect(
    () => reset(),
    [
      id,
      input.url,
      input.api_key,
      input.clear_api_key,
      input.verify_ssl,
      reset,
    ],
  );
  return mutation;
}

export function useMediaServerStatus(kind: MediaServerKind, id: string) {
  return useQuery({
    queryKey: statusKey(kind, id),
    queryFn: () => api.mediaServers.status(id),
    retry: false,
    placeholderData: undefined,
    staleTime: 0,
    refetchInterval: 10000,
  });
}

export function useRetryPendingMediaServer(kind: MediaServerKind, id: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => api.mediaServers.retryPending(id),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: statusKey(kind, id) }),
  });
}
