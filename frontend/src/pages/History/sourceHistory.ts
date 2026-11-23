import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router";
import {
  isArrMediaRouteEnabled,
  useArrInstances,
} from "@/apis/hooks/arrInstances";
import { isSourceKey, useMediaSources } from "@/apis/hooks/mediaSources";
import { useSystemSettings } from "@/apis/hooks/system";
import type { UsePaginationQueryResult } from "@/apis/queries/hooks";

const removedKey = "source:removed";

export function useHistorySources(arrKind: "sonarr" | "radarr") {
  const [params, setParams] = useSearchParams();
  const settings = useSystemSettings();
  const includeSources = settings.data?.media_sources?.visible === true;
  const master = settings.data?.general?.[`use_${arrKind}`] === true;
  const instances = useArrInstances(includeSources && master);
  const sources = useMediaSources();
  const sourceOnly =
    includeSources &&
    !isArrMediaRouteEnabled(arrKind, settings.data, instances.data);
  const scope = `${arrKind}:${includeSources}:${sourceOnly}`;
  const [nativeScope, setNativeScope] = useState<string | null>(null);
  const nativeFallback = nativeScope === scope;
  const savedKeys = useMemo(
    () => (includeSources ? params.getAll("source").filter(isSourceKey) : []),
    [includeSources, params],
  );
  const arrKeys = savedKeys.filter((key) => key.startsWith("arr:"));
  const sourceNames = useMemo(() => {
    const names = new Map<MediaSourceKey, string>();
    for (const source of sources.data?.data ?? []) {
      names.set(`source:${source.id}`, source.name);
    }
    return names;
  }, [sources.data]);
  const arrOwners = useMemo(() => {
    const owners =
      instances.data?.filter((instance) => instance.kind === arrKind) ?? [];
    return {
      names: new Map(owners.map(({ id, name }) => [id, name])),
      defaultId:
        owners.find(({ is_default: isDefault }) => isDefault)?.id ?? null,
    };
  }, [arrKind, instances.data]);

  const options: History.PageOptions = {
    includeSources,
    sourceKeys: nativeFallback
      ? arrKeys.length
        ? arrKeys
        : undefined
      : savedKeys.length
        ? savedKeys
        : sourceOnly
          ? [removedKey, ...sourceNames.keys()]
          : undefined,
  };

  const observePage = useCallback(
    (data: History.Page<History.SourceRow | Wanted.ArrRow>) => {
      const fallback = data.source_keys === undefined;
      setNativeScope(fallback ? scope : null);
      if (fallback && savedKeys.some((key) => key.startsWith("source:"))) {
        setParams(
          (current) => {
            const next = new URLSearchParams(current);
            const retained = current
              .getAll("source")
              .filter((key) => !isSourceKey(key) || key.startsWith("arr:"));
            next.delete("source");
            retained.forEach((key) => next.append("source", key));
            // Folder keys already match no Arr rows in a native response.
            // Removing them changes the filter only if no Arr keys remain.
            if (
              !retained.some(
                (key) => isSourceKey(key) && key.startsWith("arr:"),
              )
            ) {
              next.delete("page");
            }
            return next;
          },
          { replace: true },
        );
      }
    },
    [scope, savedKeys, setParams],
  );

  return {
    options,
    observePage,
    nativeFallback,
    enabled:
      !settings.isLoading &&
      !instances.isLoading &&
      (!sourceOnly || !sources.isLoading),
    sourceOnly,
    arrOwners,
    getPresentation(
      data: History.Page<History.SourceRow | Wanted.ArrRow> | undefined,
    ) {
      const labels = new Map<MediaSourceKey, string>();
      const native = !includeSources || data?.source_keys === undefined;
      const selected = [
        ...new Set(native || nativeFallback ? arrKeys : savedKeys),
      ];
      if (!native) {
        for (const key of data?.source_keys ?? []) {
          labels.set(
            key,
            key === removedKey
              ? "Removed sources"
              : (sourceNames.get(key) ?? "Media source"),
          );
        }
      }
      if (
        (native && selected.length) ||
        (!native && !sourceOnly && labels.size)
      ) {
        for (const [id, name] of arrOwners.names) labels.set(`arr:${id}`, name);
      }
      for (const key of selected) {
        if (labels.has(key)) continue;
        const id = Number(key.split(":")[1]);
        const label = key.startsWith("arr:")
          ? (arrOwners.names.get(id) ?? `Arr instance ${id}`)
          : key === removedKey
            ? "Removed sources"
            : (sourceNames.get(key) ?? `Media source ${id}`);
        labels.set(key, label);
      }
      return { labels, nativeFallback: native, selected };
    },
    onChange(value: string | null) {
      const next = new URLSearchParams(params);
      next.delete("page");
      if (isSourceKey(value)) next.set("source", value);
      else next.delete("source");
      setParams(next);
    },
  };
}

export function useHistorySourcePresentation<
  T extends History.SourceRow | Wanted.ArrRow,
>(
  sources: ReturnType<typeof useHistorySources>,
  query: UsePaginationQueryResult<T>,
) {
  const data: History.Page<T> | undefined = query.data;
  const {
    isSuccess,
    isPlaceholderData,
    isFetching,
    isFetchedAfterMount,
    refetch,
  } = query;
  const { observePage, nativeFallback, enabled } = sources;
  const includeSources = sources.options.includeSources === true;

  useEffect(() => {
    if (
      !enabled ||
      !includeSources ||
      !isSuccess ||
      isPlaceholderData ||
      isFetching ||
      !data
    ) {
      return;
    }
    if (!isFetchedAfterMount) {
      // Cached pages from another query scope must not change the fallback mode.
      if (nativeFallback || data.source_keys === undefined) void refetch();
      return;
    }
    observePage(data);
  }, [
    data,
    enabled,
    includeSources,
    isFetchedAfterMount,
    isFetching,
    isPlaceholderData,
    isSuccess,
    nativeFallback,
    observePage,
    refetch,
  ]);

  const restoringSourceOnly =
    sources.sourceOnly &&
    nativeFallback &&
    sources.options.sourceKeys === undefined &&
    data?.source_keys !== undefined;

  return {
    ...sources.getPresentation(data),
    query:
      restoringSourceOnly && (query.isSuccess || query.isRefetchError)
        ? {
            ...query,
            data: { ...query.data, data: [], total: 0 },
            paginationStatus: {
              ...query.paginationStatus,
              totalCount: 0,
              pageCount: 0,
              isPageLoading: true,
            },
          }
        : query,
  };
}
