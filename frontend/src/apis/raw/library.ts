export type LibraryKind = "series" | "movies";

type LibraryArrIds = "arr_instance_id" | "sonarrSeriesId" | "radarrId";

export type SourceLibraryRow<T extends { id: number }> = Omit<
  T,
  LibraryArrIds
> &
  Record<Extract<keyof T, LibraryArrIds>, null> &
  MediaSourceOwner & {
    arr_instance_id: null;
    source_id: number | null;
    library_id: number;
    identity_state: "identified" | "parsed" | "ambiguous" | "unmatched";
    shadowed_by: string | null;
    needs_attention: boolean;
  };

export type LibraryRow<T extends { id: number }> =
  | (T & ArrRowOwner)
  | SourceLibraryRow<T>;

export interface LibraryScope {
  includeSources: boolean;
  sourceKeys: MediaSourceKey[];
  needsAttention: boolean;
}

export function libraryParams(
  range: Parameter.Range,
  includeScores: boolean,
  scope: LibraryScope,
) {
  return {
    ...range,
    scores: includeScores ? 1 : undefined,
    sources: scope.includeSources ? 1 : undefined,
    source_key: scope.sourceKeys.length ? scope.sourceKeys : undefined,
    needs_attention: scope.needsAttention ? 1 : undefined,
  };
}

export function getLibraryRowKey(row: {
  id: number;
  origin?: "arr" | "source";
}) {
  return `${row.origin ?? "arr"}:${row.id}`;
}

export function getLibraryOwnerKey(row: {
  source_key?: MediaSourceKey | null;
  arr_instance_id?: number | null;
}): MediaSourceKey | undefined {
  return (
    row.source_key ??
    (row.arr_instance_id == null ? undefined : `arr:${row.arr_instance_id}`)
  );
}

export function libraryItemPath(
  kind: LibraryKind,
  row: { id: number; origin?: "arr" | "source" },
) {
  return `/${kind}/${row.origin === "source" ? "source/" : ""}${row.id}`;
}
