import { useRef } from "react";
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

export function useSeerrRequestMutation(identity: SeerrIdentity | null) {
  const client = useQueryClient();
  // The query to refresh is the title that was on screen when the reader
  // submitted, not whichever title the component happens to be rendering when
  // the answer lands. React Query reads onSettled off the latest render's
  // options, so on a navigation mid-request that closure names the new title:
  // it would refetch a title nothing happened to and leave the requested one
  // stale. mutationFn runs synchronously inside mutate(), while the
  // submitting render is still the current one, so it is the one place that
  // can pin the right identity.
  const submitted = useRef<SeerrIdentity | null>(null);
  return useMutation({
    mutationFn: (body: SeerrRequestBody) => {
      submitted.current = identity;
      return api.seerr.request(body);
    },
    onSettled: () => {
      const target = submitted.current;
      if (target)
        void client.invalidateQueries({ queryKey: seerrMediaKey(target) });
    },
  });
}

export function useSeerrTestConnectionMutation() {
  return useMutation({
    mutationFn: (params: {
      url: string;
      apikey: string;
      verifySsl?: boolean;
    }) => api.seerr.testConnection(params.url, params.apikey, params.verifySsl),
  });
}
