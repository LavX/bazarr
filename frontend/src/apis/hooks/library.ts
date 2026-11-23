import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router";
import { usePaginationQuery } from "@/apis/queries/hooks";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import {
  getLibraryOwnerKey,
  getLibraryRowKey,
  LibraryKind,
  LibraryRow,
  LibraryScope,
} from "@/apis/raw/library";
import {
  isArrMediaRouteEnabled,
  useArrInstanceLabels,
  useArrInstances,
} from "./arrInstances";
import { useMediaSources } from "./mediaSources";
import { useSystemSettings } from "./system";

function isOwnerKey(value: string): value is MediaSourceKey {
  return (
    value === "source:removed" ||
    (/^(arr|source):[1-9][0-9]{0,9}$/.test(value) &&
      Number(value.split(":")[1]) <= 2_147_483_647)
  );
}

function useLibraryScope(kind: LibraryKind) {
  const [params, setParams] = useSearchParams();
  const arrKind = kind === "series" ? "sonarr" : "radarr";
  const contentKind = kind === "series" ? "series" : "movie";
  const { data: settings } = useSystemSettings();
  const { data: instances } = useArrInstances();
  const arr = useArrInstanceLabels(arrKind);
  const { data: sources } = useMediaSources();
  const configuredSources = useMemo(
    () =>
      (sources?.data ?? []).filter((source) =>
        source.libraries.some(
          (library) => library.content_kind === contentKind,
        ),
      ),
    [contentKind, sources?.data],
  );
  const arrEnabled = isArrMediaRouteEnabled(arrKind, settings, instances);
  const sourceOnly = configuredSources.length > 0 && !arrEnabled;
  const selected = useMemo(() => {
    const values = params.getAll("source");
    const legacy = params.get("instance");
    if (values.length === 0 && legacy) values.push(`arr:${legacy}`);
    return [...new Set(values.filter(isOwnerKey))].filter(
      (key) => key.startsWith("source:") || !sourceOnly,
    );
  }, [params, sourceOnly]);
  const needsAttention = params.get("needs_attention") === "1";
  const scope = useMemo<LibraryScope>(
    () => ({
      includeSources: true,
      sourceKeys:
        selected.length > 0
          ? [...selected].sort()
          : sourceOnly
            ? configuredSources.map<MediaSourceKey>(
                (source) => `source:${source.id}`,
              )
            : [],
      needsAttention,
    }),
    [selected, sourceOnly, configuredSources, needsAttention],
  );

  const update = (keys: string[], attention: boolean) => {
    const next = new URLSearchParams(params);
    next.delete("page");
    next.delete("instance");
    next.delete("source");
    for (const key of keys.filter(isOwnerKey)) next.append("source", key);
    if (attention) next.set("needs_attention", "1");
    else next.delete("needs_attention");
    setParams(next);
  };

  return { arr, scope, selected, configuredSources, sourceOnly, update };
}

function seriesCacheKey(row: LibraryRow<Item.Series>) {
  return row.origin === "source"
    ? [QueryKeys.Series, "library-row", getLibraryRowKey(row)]
    : [QueryKeys.Series, row.id];
}

function movieCacheKey(row: LibraryRow<Item.Movie>) {
  return row.origin === "source"
    ? [QueryKeys.Movies, "library-row", getLibraryRowKey(row)]
    : [QueryKeys.Movies, row.id];
}

function useLibraryPresentation<T extends { id: number }>(
  rows: LibraryRow<T>[] | undefined,
  {
    arr,
    scope,
    selected,
    configuredSources,
    sourceOnly,
    update,
  }: ReturnType<typeof useLibraryScope>,
) {
  const labels = useMemo(() => {
    const result = new Map<MediaSourceKey, string>();
    if (!sourceOnly) {
      for (const { value, label } of arr.options)
        result.set(`arr:${Number(value)}`, label);
    }
    for (const source of configuredSources)
      result.set(`source:${source.id}`, source.name);
    for (const row of rows ?? []) {
      const key = getLibraryOwnerKey(row);
      if (
        key &&
        row.origin === "source" &&
        row.source_name &&
        !result.has(key)
      ) {
        result.set(key, row.source_name);
      }
    }
    return result;
  }, [arr.options, configuredSources, rows, sourceOnly]);
  const options = useMemo(
    () => Array.from(labels, ([value, label]) => ({ value, label })),
    [labels],
  );
  const hasSources =
    options.some(({ value }) => value.startsWith("source:")) ||
    (rows?.some((row) => row.origin === "source") ?? false) ||
    selected.some((key) => key.startsWith("source:"));
  const showAttention =
    scope.needsAttention ||
    (rows?.some((row) => row.origin === "source" && row.needs_attention) ??
      false);

  const canUseSourceLibrary = useCallback(
    (row: { source_id: number | null; library_id: number }) =>
      configuredSources.some(
        (source) =>
          source.id === row.source_id &&
          source.enabled &&
          source.libraries.some(
            (library) => library.id === row.library_id && library.enabled,
          ),
      ),
    [configuredSources],
  );

  return {
    canUseSourceLibrary,
    labels,
    arrNames: arr.nameById,
    defaultArr: arr.defaultId,
    hasSources,
    showBadge: hasSources || arr.multiInstance,
    showAttention,
    ownerFilter: {
      options,
      values: selected,
      onChange: (keys: string[]) => update(keys, scope.needsAttention),
      label: hasSources ? ("Source" as const) : ("Instance" as const),
      showSingle: hasSources,
    },
    needsAttention: scope.needsAttention,
    onNeedsAttentionChange: (attention: boolean) => update(selected, attention),
    clearSourceFilters: () => update([], false),
  };
}

function useLibraryQueryKey(
  kind: QueryKeys.Series | QueryKeys.Movies,
  scope: LibraryScope,
) {
  return useMemo(
    () =>
      scope.includeSources || scope.sourceKeys.length > 0
        ? [kind, "library", scope]
        : [kind],
    [kind, scope],
  );
}

export function useSeriesLibrary(includeScores: boolean) {
  const owners = useLibraryScope("series");
  const { scope } = owners;
  const queryKey = useLibraryQueryKey(QueryKeys.Series, scope);
  const query = usePaginationQuery(
    queryKey,
    (range) => api.series.library(range, includeScores, scope),
    true,
    true,
    true,
    includeScores,
    true,
    seriesCacheKey,
  );
  return {
    query,
    ...useLibraryPresentation<Item.Series>(query.data?.data, owners),
  };
}

export function useMovieLibrary(includeScores: boolean) {
  const owners = useLibraryScope("movies");
  const { scope } = owners;
  const queryKey = useLibraryQueryKey(QueryKeys.Movies, scope);
  const query = usePaginationQuery(
    queryKey,
    (range) => api.movies.library(range, includeScores, scope),
    true,
    true,
    true,
    includeScores,
    true,
    movieCacheKey,
  );
  return {
    query,
    ...useLibraryPresentation<Item.Movie>(query.data?.data, owners),
  };
}
