import { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import type { SeerrIdentity, SeerrRequestBody } from "@/types/seerr";

export const seerrMediaKey = (identity: SeerrIdentity) =>
  "tvdbId" in identity
    ? [QueryKeys.Seerr, "media", identity.kind, "tvdb", identity.tvdbId]
    : [QueryKeys.Seerr, "media", identity.kind, "tmdb", identity.tmdbId];

// Fires only while the title page is open and Seerr is enabled: the caller
// passes `enabled`, and nothing here prefetches.
export function useSeerrMedia(
  identity: SeerrIdentity | null,
  enabled: boolean,
) {
  return useQuery({
    queryKey: identity
      ? seerrMediaKey(identity)
      : [QueryKeys.Seerr, "media", "none"],
    queryFn: ({ signal }) =>
      identity && "tvdbId" in identity
        ? api.seerr.mediaByTvdb(identity.tvdbId, signal)
        : api.seerr.media(identity!.kind, identity!.tmdbId, signal),
    enabled: enabled && identity !== null,
    staleTime: 60_000,
    gcTime: 30 * 60_000,
    retry: false,
    refetchOnWindowFocus: false,
  });
}

/** A request, with the title it was made for, so the answer cannot be misfiled. */
export interface SeerrRequestVariables {
  body: SeerrRequestBody;
  identity: SeerrIdentity;
}

export function useSeerrRequestMutation() {
  const client = useQueryClient();
  // The identity travels with the request rather than being read off the
  // component. React Query hands onSettled the latest render's options, so a
  // closure over the selected title names whatever the reader has navigated
  // to by the time the answer lands, and a ref holds only the most recent
  // submit, which misfiles the first of two overlapping ones. Variables are
  // per-mutation, so each answer refreshes exactly the title it belongs to.
  return useMutation({
    mutationFn: ({ body }: SeerrRequestVariables) => api.seerr.request(body),
    onSettled: (_data, _error, { identity }) => {
      void client.invalidateQueries({ queryKey: seerrMediaKey(identity) });
    },
  });
}

/**
 * The connection a test verdict belongs to. Same contract as the arr test
 * hook: editing any of these drops the previous verdict, so a green result
 * never survives the address or the key it was measured against.
 */
export interface SeerrTestConnection {
  url?: string;
  apikey?: string;
  verifySsl?: boolean;
}

export function useSeerrTestConnectionMutation(
  connection: SeerrTestConnection = {},
) {
  const mutation = useMutation({
    mutationFn: (params: {
      url: string;
      apikey: string;
      verifySsl?: boolean;
    }) => api.seerr.testConnection(params.url, params.apikey, params.verifySsl),
  });
  const { reset } = mutation;
  const { url, apikey, verifySsl } = connection;
  useEffect(() => reset(), [url, apikey, verifySsl, reset]);
  return mutation;
}
