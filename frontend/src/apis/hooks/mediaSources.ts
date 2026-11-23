import { useMemo } from "react";
import { useSearchParams } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { isAxiosError } from "axios";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import { withInlineErrors } from "@/apis/raw/inlineWrites";
import type { SourceContentKind } from "@/apis/raw/mediaSources";
import { useArrInstanceLabels } from "./arrInstances";
import { refreshWhenJobFinishes } from "./jobWatch";
import { useSystemSettings } from "./system";

export const mediaSourcesKey = [QueryKeys.System, "media-sources"] as const;

export function useMediaSources() {
  return useQuery({
    queryKey: mediaSourcesKey,
    queryFn: ({ signal }) =>
      withInlineErrors(() => api.mediaSources.list(signal)),
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

function isSourceKey(value: string | null): value is MediaSourceKey {
  return (
    value === "source:removed" ||
    (value !== null &&
      /^(arr|source):[1-9][0-9]{0,9}$/.test(value) &&
      Number(value.split(":")[1]) <= 2_147_483_647)
  );
}

export function useSourceFilter(
  kind: SourceContentKind,
  arr: "sonarr" | "radarr" | "sportarr",
  {
    includeDisabledSources = false,
    includeRemovedSources = false,
    clearOnChange = [] as string[],
  } = {},
) {
  const [params, setParams] = useSearchParams();
  const arrLabels = useArrInstanceLabels(arr);
  const { data: settings } = useSystemSettings();
  const { data, isEnabled } = useMediaSources();
  const sources = useMemo(
    () =>
      isEnabled
        ? (data?.data ?? []).filter((source) =>
            source.libraries.some((library) => library.content_kind === kind),
          )
        : [],
    [data, isEnabled, kind],
  );
  const removed =
    includeRemovedSources && data?.availability?.[kind]?.removed === true;
  const enabledArr = settings?.general?.[`use_${arr}`] === true;
  const arrConfigured =
    enabledArr &&
    (arrLabels.enabledOptions.length > 0 ||
      (arrLabels.options.length === 0 &&
        arr !== "sportarr" &&
        Boolean(
          settings?.[arr]?.ip?.trim() && settings?.[arr]?.apikey?.trim(),
        )));
  const labels = useMemo(() => {
    const names = new Map<MediaSourceKey, string>();
    if (enabledArr) {
      for (const option of arrLabels.enabledOptions) {
        names.set(`arr:${option.value}`, option.label);
      }
    }
    for (const source of sources) {
      if (
        includeDisabledSources ||
        (source.enabled &&
          source.libraries.some(
            (library) => library.enabled && library.content_kind === kind,
          ))
      ) {
        names.set(`source:${source.id}`, source.name);
      }
    }
    if (removed) names.set("source:removed", "Removed sources");
    return names;
  }, [
    enabledArr,
    removed,
    arrLabels.enabledOptions,
    sources,
    kind,
    includeDisabledSources,
  ]);

  const legacy = params.get("instance");
  const sourceParam = params.get("source");
  const key = sourceParam ?? (legacy ? `arr:${legacy}` : null);
  const selected = isSourceKey(key) ? key : null;

  const sourceKeys = useMemo<MediaSourceKey[]>(() => {
    if (selected) {
      return !arrConfigured && sources.length > 0 && selected.startsWith("arr:")
        ? ["source:removed"]
        : [selected];
    }
    // The API keeps its legacy Arr-only response when a folder has no scanned rows.
    // On a folder-only page, send the configured source keys so that legacy Arr rows stay out.
    return arrConfigured
      ? []
      : [
          ...sources.map<MediaSourceKey>((source) => `source:${source.id}`),
          ...(removed ? ["source:removed" as const] : []),
        ];
  }, [arrConfigured, removed, selected, sources]);

  return {
    includeSources: true,
    showBadge: labels.size > 1,
    labels,
    sourceKeys,
    arrActions: sources.length === 0 || arrConfigured,
    arrNames: arrLabels.nameById,
    defaultArr: arrLabels.defaultId,
    selected,
    onChange(value: string | null) {
      const next = new URLSearchParams(params);
      next.delete("instance");
      next.delete("page");
      clearOnChange.forEach((param) => next.delete(param));
      if (isSourceKey(value)) next.set("source", value);
      else next.delete("source");
      setParams(next);
    },
  };
}
