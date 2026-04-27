import { useMutation, useQuery } from "@tanstack/react-query";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";

// Non-cryptographic FNV-1a 32-bit hash, returned as 8-char hex. Used as a
// fingerprint of the Jellyfin apikey for the React Query cache key: same
// key -> same fingerprint -> cache hit; changed key -> different
// fingerprint -> cache miss and refetch (different Jellyfin accounts can
// see different libraries, so stale cache would show wrong options).
// Crucially the fingerprint is NOT the apikey - the cache key never
// contains secret material that could be exposed via React Query devtools
// or other cache instrumentation.
export const apikeyFingerprint = (apikey: string | undefined): string => {
  if (!apikey) return "";
  let h = 0x811c9dc5;
  for (let i = 0; i < apikey.length; i++) {
    h ^= apikey.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(16).padStart(8, "0");
};

export const useJellyfinLibrariesQuery = (
  enabled: boolean = true,
  url?: string,
  apikey?: string,
  verifySsl?: boolean,
) => {
  return useQuery({
    queryKey: [
      QueryKeys.Jellyfin,
      "libraries",
      url,
      apikeyFingerprint(apikey),
      verifySsl,
    ],
    queryFn: () => api.jellyfin.libraries(url, apikey, verifySsl),
    enabled,
    staleTime: 1000 * 60 * 5,
    refetchOnWindowFocus: false,
    retry: 3,
    retryDelay: (attemptIndex: number) =>
      Math.min(1000 * 2 ** attemptIndex, 30000),
  });
};

export const useJellyfinTestConnectionMutation = () => {
  return useMutation({
    mutationFn: (params: {
      url: string;
      apikey: string;
      verifySsl?: boolean;
    }) =>
      api.jellyfin.testConnection(params.url, params.apikey, params.verifySsl),
  });
};

export const useJellyfinRefreshLibrariesMutation = () => {
  return useMutation({
    mutationFn: () => api.jellyfin.refreshLibraries(),
  });
};
