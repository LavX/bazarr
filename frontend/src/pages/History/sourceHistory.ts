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
  const key = params.get("source");
  const selected =
    includeSources &&
    isSourceKey(key) &&
    (!sourceOnly || key.startsWith("source:"))
      ? key
      : null;
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
      ? undefined
      : selected
        ? [selected]
        : sourceOnly
          ? [removedKey, ...sourceNames.keys()]
          : undefined,
  };

  const observePage = useCallback(
    (data: History.Page<History.SourceRow | Wanted.ArrRow>) => {
      const fallback = data.source_keys === undefined;
      setNativeScope(fallback ? scope : null);
      if (fallback && selected) {
        setParams(
          (current) => {
            const next = new URLSearchParams(current);
            next.delete("source");
            next.delete("page");
            return next;
          },
          { replace: true },
        );
      }
    },
    [scope, selected, setParams],
  );

  return {
    options,
    observePage,
    nativeFallback,
    enabled:
      !settings.isLoading &&
      !instances.isLoading &&
      (!sourceOnly || !sources.isLoading),
    selected,
    sourceOnly,
    arrOwners,
    getPresentation(
      data: History.Page<History.SourceRow | Wanted.ArrRow> | undefined,
    ) {
      const labels = new Map<MediaSourceKey, string>();
      const nativeFallback = !includeSources || data?.source_keys === undefined;
      if (nativeFallback) return { labels, nativeFallback, selected: null };
      for (const key of data?.source_keys ?? []) {
        if (key === removedKey) {
          labels.set(key, "Removed sources");
        } else {
          labels.set(key, sourceNames.get(key) ?? "Media source");
        }
      }
      if (!sourceOnly && labels.size) {
        for (const [id, name] of arrOwners.names) labels.set(`arr:${id}`, name);
      }
      if (selected && !labels.has(selected)) {
        const label = selected.startsWith("arr:")
          ? (arrOwners.names.get(Number(selected.slice(4))) ?? "Arr instance")
          : selected === removedKey
            ? "Removed sources"
            : (sourceNames.get(selected) ?? "Media source");
        labels.set(selected, label);
      }
      return { labels, nativeFallback, selected };
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

  return sources.getPresentation(data);
}
