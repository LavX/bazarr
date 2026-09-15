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
  return useMutation({
    mutationFn: (body: SeerrRequestBody) => api.seerr.request(body),
    onSettled: () => {
      if (identity)
        void client.invalidateQueries({ queryKey: seerrMediaKey(identity) });
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
