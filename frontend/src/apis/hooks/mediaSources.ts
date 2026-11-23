import { useMemo } from "react";
import { useSearchParams } from "react-router";
import {
  QueryClient,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { isAxiosError } from "axios";
import { QueryKeys } from "@/apis/queries/keys";
import {
  invalidateSourceLibraryViews,
  invalidateSportsSource,
  type SportsSourceScope,
} from "@/apis/queries/sourceDetails";
import api from "@/apis/raw";
import { withInlineErrors } from "@/apis/raw/inlineWrites";
import type {
  MediaSource,
  ServerRootMapping,
  SourceContentKind,
  SourceLibrary,
} from "@/apis/raw/mediaSources";
import { useArrInstanceLabels } from "./arrInstances";
import { refreshWhenJobFinishes } from "./jobWatch";
import { useSystemSettings } from "./system";

export const mediaSourcesKey = [QueryKeys.System, "media-sources"] as const;

export function serverSourceLibraryChoicesKey(instanceId: string) {
  return [...mediaSourcesKey, "server-libraries", instanceId] as const;
}

const mediaSourcesQuery = {
  queryKey: mediaSourcesKey,
  queryFn: ({ signal }: { signal: AbortSignal }) =>
    withInlineErrors(() => api.mediaSources.list(signal)),
};

export function useMediaSources() {
  return useQuery(mediaSourcesQuery);
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

function sourceOwnsSports(
  client: QueryClient,
  source: MediaSource,
  includeUnknown = false,
) {
  if (source.libraries.some((library) => library.content_kind === "sports"))
    return true;
  const sports =
    client.getQueryData<Awaited<ReturnType<typeof api.mediaSources.list>>>(
      mediaSourcesKey,
    )?.availability?.sports;
  if (!sports) return includeUnknown;
  return sports.retained_source_ids?.includes(source.id) ?? false;
}

export async function refreshMediaSources(
  client: QueryClient,
  sports?: SportsSourceScope,
) {
  await Promise.all([
    client.invalidateQueries({ queryKey: mediaSourcesKey, exact: true }),
    ...(sports ? [invalidateSportsSource(client, sports)] : []),
  ]);
}

export function refreshAfterSourceScan(
  client: QueryClient,
  jobId: number | null,
) {
  // The job does not report a former owner when a scan takes over a file.
  // A handover can affect any standalone library, including other media kinds.
  return refreshWhenJobFinishes(client, jobId, async () => {
    await Promise.all([
      refreshMediaSources(client, {}),
      invalidateSourceLibraryViews(client),
    ]);
  });
}

function sportsRemovalScope(
  client: QueryClient,
  source: MediaSource,
  library?: SourceLibrary,
): SportsSourceScope | undefined {
  // A source can own retained activity after its last sports library is gone.
  const ownsSports = library
    ? library.content_kind === "sports"
    : sourceOwnsSports(client, source, true);
  return ownsSports
    ? {
        owner: { sourceId: source.id, libraryId: library?.id },
        includeRemoved: !library,
      }
    : undefined;
}

export async function linkedServerRemovalScope(
  client: QueryClient,
  instanceId: string,
): Promise<SportsSourceScope> {
  // Capture the linked source before the connection DELETE cascades its row.
  const list = await client
    .fetchQuery({ ...mediaSourcesQuery, staleTime: 0, retry: false })
    .catch(() => undefined);
  const source = list?.data.find(
    (row) => row.media_server_instance_id === instanceId,
  );
  // The 204 carries no owner. A failed or incomplete read needs the wider scope.
  return {
    owner: source ? { sourceId: source.id } : undefined,
    includeRemoved: true,
  };
}

export function useUpdateServerSourceRoots() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({
      sourceId,
      libraryId,
      roots,
      confirmedRemoved,
    }: {
      sourceId: number;
      libraryId: number;
      roots: ServerRootMapping[];
      confirmedRemoved: number;
    }) =>
      (
        await withInlineErrors(() =>
          api.mediaSources.updateServerRoots(
            sourceId,
            libraryId,
            roots,
            confirmedRemoved,
          ),
        )
      ).data,
    onSuccess: async (updated, { sourceId }) => {
      const scope =
        updated.content_kind === "sports"
          ? { owner: { sourceId, libraryId: updated.id } }
          : undefined;
      // A root edit can remove indexed items immediately and discover new ones in its scan.
      await refreshMediaSources(client, scope);
      if (typeof updated.job_id === "number") {
        refreshAfterSourceScan(client, updated.job_id);
      }
    },
  });
}

export function useWriteMediaSource({
  source,
  library,
  contentKind,
  refreshSports = true,
}: {
  source?: MediaSource;
  library?: SourceLibrary;
  contentKind?: SourceContentKind;
  refreshSports?: boolean;
} = {}) {
  const client = useQueryClient();
  const mutation = useMutation({
    mutationFn: ({
      write,
    }: {
      write: () => Promise<unknown>;
      sportsScope?: SportsSourceScope;
    }) => withInlineErrors(write),
    onSuccess: (_, { sportsScope }) => refreshMediaSources(client, sportsScope),
  });
  // The form can change while a write is pending. Keep its submitted owner.
  const variables = (write: () => Promise<unknown>) => {
    const sportsSource =
      source &&
      (library || contentKind
        ? (library?.content_kind ?? contentKind) === "sports"
        : sourceOwnsSports(client, source));
    return {
      write,
      sportsScope:
        source && sportsSource && refreshSports
          ? { owner: { sourceId: source.id, libraryId: library?.id } }
          : undefined,
    };
  };
  return {
    ...mutation,
    mutate: (
      write: () => Promise<unknown>,
      options?: Parameters<typeof mutation.mutate>[1],
    ) => mutation.mutate(variables(write), options),
    mutateAsync: (
      write: () => Promise<unknown>,
      options?: Parameters<typeof mutation.mutateAsync>[1],
    ) => mutation.mutateAsync(variables(write), options),
  };
}

export function useRemoveMediaSource() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({
      source,
      library,
    }: {
      source: MediaSource;
      library?: SourceLibrary;
    }) =>
      withInlineErrors(() =>
        library
          ? api.mediaSources.removeLibrary(source.id, library.id)
          : api.mediaSources.remove(source.id),
      ),
    onSuccess: (_, { source, library }) =>
      refreshMediaSources(client, sportsRemovalScope(client, source, library)),
  });
}

export function useScanMediaSource() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (source: MediaSource) =>
      withInlineErrors(() => api.mediaSources.scan(source.id)),
    onSuccess: (jobId) => refreshAfterSourceScan(client, jobId),
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
    includeRetainedSources = false,
    clearOnChange = [] as string[],
  } = {},
) {
  const [params, setParams] = useSearchParams();
  const arrLabels = useArrInstanceLabels(arr);
  const { data: settings } = useSystemSettings();
  const { data, isEnabled } = useMediaSources();
  const retained = includeRetainedSources
    ? data?.availability?.[kind]
    : undefined;
  const retainedIds = retained?.retained_source_ids;
  const sources = useMemo(
    () =>
      isEnabled
        ? (data?.data ?? []).filter(
            (source) =>
              retainedIds?.includes(source.id) ||
              source.libraries.some((library) => library.content_kind === kind),
          )
        : [],
    [data, isEnabled, kind, retainedIds],
  );
  const removed = retained?.removed === true;
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
    // On a source-only page, send current and retained owners so that legacy Arr rows stay out.
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
