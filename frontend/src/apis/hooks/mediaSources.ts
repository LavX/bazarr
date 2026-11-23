import { useMemo } from "react";
import { useSearchParams } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { isAxiosError } from "axios";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import { withInlineErrors } from "@/apis/raw/inlineWrites";
import type {
  ServerRootMapping,
  SourceContentKind,
} from "@/apis/raw/mediaSources";
import { useArrInstanceLabels } from "./arrInstances";
import { refreshWhenJobFinishes } from "./jobWatch";
import { useSystemSettings } from "./system";

export const mediaSourcesKey = [QueryKeys.System, "media-sources"] as const;

export function serverSourceLibraryChoicesKey(instanceId: string) {
  return [...mediaSourcesKey, "server-libraries", instanceId] as const;
}

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

export function useServerSourceLibraryChoices(
  instanceId: string,
  enabled: boolean,
) {
  return useQuery({
    queryKey: serverSourceLibraryChoicesKey(instanceId),
    queryFn: ({ signal }) =>
      withInlineErrors(() =>
        api.mediaSources.serverLibraries(instanceId, signal),
      ),
    enabled,
    placeholderData: undefined,
    staleTime: 0,
  });
}

export function usePreviewServerSourceRoots() {
  return useMutation({
    mutationFn: ({
      sourceId,
      libraryId,
      roots,
    }: {
      sourceId: number;
      libraryId: number;
      roots: ServerRootMapping[];
    }) =>
      withInlineErrors(() =>
        api.mediaSources.previewServerRoots(sourceId, libraryId, roots),
      ),
  });
}

export function useValidateServerSourceRoots(
  {
    instanceId,
    locator,
    roots,
    contentKind,
    sourceId,
    libraryId,
  }: {
    instanceId: string;
    locator: string;
    roots: ServerRootMapping[];
    contentKind: SourceContentKind;
    sourceId?: number;
    libraryId?: number;
  },
  enabled: boolean,
) {
  return useQuery({
    queryKey: [
      ...mediaSourcesKey,
      "validate-server-roots",
      instanceId,
      sourceId,
      libraryId,
      locator,
      contentKind,
      roots,
    ],
    queryFn: ({ signal }) =>
      withInlineErrors(async () => {
        if (sourceId !== undefined && libraryId !== undefined) {
          await api.mediaSources.previewServerRoots(
            sourceId,
            libraryId,
            roots,
            signal,
          );
        } else {
          await api.mediaSources.validateServerRoots(
            roots,
            contentKind,
            signal,
          );
        }
        return true;
      }),
    enabled,
    placeholderData: undefined,
    staleTime: 0,
    retry: false,
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
  arr: "sonarr" | "radarr",
  { includeDisabledSources = false } = {},
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
  const includeSources = settings?.media_sources?.visible === true;
  const enabledArr = settings?.general?.[`use_${arr}`] === true;
  const arrConfigured =
    enabledArr &&
    (arrLabels.enabledOptions.length > 0 ||
      (arrLabels.options.length === 0 &&
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
    return names;
  }, [
    enabledArr,
    arrLabels.enabledOptions,
    sources,
    kind,
    includeDisabledSources,
  ]);

  const legacy = params.get("instance");
  const sourceParam = params.get("source");
  const scopedSource =
    includeSources || sourceParam?.startsWith("arr:") ? sourceParam : null;
  const key = scopedSource ?? (legacy ? `arr:${legacy}` : null);
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
      : sources.map<MediaSourceKey>((source) => `source:${source.id}`);
  }, [arrConfigured, selected, sources]);

  return {
    includeSources,
    showBadge: includeSources ? labels.size > 1 : arrLabels.multiInstance,
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
      if (isSourceKey(value)) next.set("source", value);
      else next.delete("source");
      setParams(next);
    },
  };
}
