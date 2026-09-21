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

/**
 * What one save writes.
 *
 * The payload is passed at mutate() time so one hook can write several rows:
 * `mutateAsync(() => payload)` per row, which is what the onboarding wizard
 * needs to create the media servers a reader ticked in one run. A caller that
 * has its payload at hook-call time still passes it as `input` and calls
 * `mutate(undefined)`, which is what the Connections editors do.
 *
 * It is handed over as a thunk rather than as the object itself because
 * whatever is passed to mutate() becomes the mutation's stored `variables`, and
 * this payload carries a write-only API key. A function is not serialised into
 * a state dump, so the credential stays where apis/raw/mediaServers.ts already
 * keeps it: out of every cache and every error.
 *
 * Test and libraries deliberately keep their payload at hook-call time: they
 * reset their result when a connection value changes, which is the behaviour
 * that stops a stale "Connected" sitting under an edited URL.
 */
export type SaveMediaServerInput = MediaServerUpdate & {
  kind?: MediaServerKind;
};

export type SaveMediaServerPayload = () => SaveMediaServerInput;

export function useSaveMediaServerInstance(
  kind?: MediaServerKind,
  input?: MediaServerUpdate,
  id?: string,
) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (variables: SaveMediaServerPayload | void) => {
      const source: SaveMediaServerInput = variables
        ? variables()
        : (input ?? {});
      const { kind: payloadKind, ...payload } = source;
      const target = payloadKind ?? kind;
      if (id) {
        return api.mediaServers.update(id, payload);
      }
      if (!target) {
        return Promise.reject(
          new Error("A new media server instance needs a kind"),
        );
      }
      return api.mediaServers.create({
        ...payload,
        kind: target,
        name: payload.name ?? "",
        url: payload.url ?? "",
      });
    },
    onSuccess: (_data, variables) => {
      const target = (variables ? variables().kind : undefined) ?? kind;
      if (!target) return;
      void client.invalidateQueries({ queryKey: instancesKey(target) });
      if (id) void client.invalidateQueries({ queryKey: itemKey(target, id) });
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

export function useMediaServerLibraries(
  kind: MediaServerKind,
  input: ConnectionOverrides,
  id?: string,
) {
  const mutation = useMutation({
    mutationFn: () =>
      id
        ? api.mediaServers.librariesExisting(id, input)
        : api.mediaServers.libraries(kind, {
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

export function useRefreshMediaServerLibraries(
  kind: MediaServerKind,
  id: string,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => api.mediaServers.refreshLibraries(id),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: statusKey(kind, id) }),
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
