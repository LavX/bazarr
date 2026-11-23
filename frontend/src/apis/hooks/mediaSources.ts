import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { isAxiosError } from "axios";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import { withInlineErrors } from "@/apis/raw/inlineWrites";
import type { SourceContentKind } from "@/apis/raw/mediaSources";
import { refreshWhenJobFinishes } from "./jobWatch";
import { useSystemSettings } from "./system";

export const mediaSourcesKey = [QueryKeys.System, "media-sources"] as const;

export function useMediaSourcesVisible() {
  const { data } = useSystemSettings();
  return data?.media_sources?.visible === true;
}

export function useMediaSources() {
  const visible = useMediaSourcesVisible();
  return useQuery({
    queryKey: mediaSourcesKey,
    queryFn: ({ signal }) =>
      withInlineErrors(() => api.mediaSources.list(signal)),
    enabled: visible,
  });
}

export function useSourceAvailability() {
  const { data, isEnabled, isLoading } = useMediaSources();
  return {
    availability: isEnabled ? data?.availability : undefined,
    isLoading,
  };
}

export function useValidateLibraryPath(
  path: string,
  contentKind: SourceContentKind,
  enabled: boolean,
) {
  return useQuery({
    queryKey: [...mediaSourcesKey, "validate-path", path, contentKind],
    queryFn: ({ signal }) =>
      withInlineErrors(() =>
        api.mediaSources.validatePath(path, contentKind, signal),
      ),
    enabled,
    placeholderData: undefined,
    staleTime: 0,
  });
}

export function useWriteMediaSource() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (write: () => Promise<unknown>) => withInlineErrors(write),
    onSuccess: () =>
      client.invalidateQueries({ queryKey: mediaSourcesKey, exact: true }),
  });
}

export function useScanMediaSource() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: number) =>
      withInlineErrors(() => api.mediaSources.scan(id)),
    onSuccess: (jobId) =>
      refreshWhenJobFinishes(client, jobId, () =>
        client.invalidateQueries({ queryKey: mediaSourcesKey, exact: true }),
      ),
  });
}

export function mediaSourceErrorMessage(error: unknown, fallback: string) {
  if (isAxiosError<{ message?: string }>(error)) {
    return error.response?.data?.message || fallback;
  }
  return fallback;
}
