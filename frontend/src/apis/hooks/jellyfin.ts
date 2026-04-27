import { useMutation, useQuery } from "@tanstack/react-query";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";

export const useJellyfinLibrariesQuery = (
  enabled: boolean = true,
  url?: string,
  apikey?: string,
  verifySsl?: boolean,
) => {
  return useQuery({
    // The apikey deliberately does NOT enter the query key. Every keystroke
    // while editing credentials would otherwise spawn a fresh cache entry
    // containing partial secret material, visible to React Query devtools
    // and any instrumentation that walks the cache. The secret only rides
    // in the request body. If the user changes apikey for the same URL,
    // they'll Test Connection (separate mutation) and Save before the
    // libraries panel matters again - and a page rerender mints a fresh
    // query instance.
    queryKey: [QueryKeys.Jellyfin, "libraries", url, verifySsl],
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
