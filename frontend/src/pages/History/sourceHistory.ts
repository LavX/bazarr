import { useMemo } from "react";
import { useSearchParams } from "react-router";
import {
  isArrMediaRouteEnabled,
  useArrInstances,
} from "@/apis/hooks/arrInstances";
import { isSourceKey, useMediaSources } from "@/apis/hooks/mediaSources";
import { useSystemSettings } from "@/apis/hooks/system";

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
    sourceKeys: selected
      ? [selected]
      : sourceOnly
        ? [removedKey, ...sourceNames.keys()]
        : undefined,
  };

  return {
    options,
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
      if (nativeFallback) return { labels, nativeFallback };
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
      return { labels, nativeFallback };
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
