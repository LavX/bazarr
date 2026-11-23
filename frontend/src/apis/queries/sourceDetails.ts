import type { QueryClient, QueryKey } from "@tanstack/react-query";
import type { SportsFilters } from "@/apis/raw/sports";
import { QueryKeys } from "./keys";

export const sourceDetailKeys = {
  root: ["source-detail"] as const,
  group: (id: number, kind: "show" | "league" = "show") =>
    ["source-detail", "groups", kind, id] as const,
  item: (id: number, kind: "movie" | "episode" | "sports_event" = "movie") =>
    ["source-detail", "items", kind, id] as const,
  episodes: (id: number) => ["source-detail", "groups", id, "items"] as const,
  leagueEvents: (id: number) =>
    ["source-detail", "groups", "league", id, "items"] as const,
};

export interface SourceDetailScope {
  sourceId: number;
  libraryId?: number;
}

export async function invalidateSourceSportsReleaseActivity(
  client: QueryClient,
) {
  const queries = {
    queryKey: [QueryKeys.Sports],
    predicate: ({ queryKey }: { queryKey: QueryKey }) => {
      const [, activity, value] = queryKey;
      if (activity !== QueryKeys.History && activity !== QueryKeys.Blacklist)
        return false;
      const filters = value as SportsFilters | undefined;
      return (
        filters?.includeSources === true &&
        filters.owner === undefined &&
        filters.eventId === undefined &&
        filters.leagueId === undefined &&
        (!filters.sourceKeys?.length ||
          filters.sourceKeys.some((key) => key.startsWith("source:")))
      );
    },
  };
  await client.cancelQueries(queries);
  await client.invalidateQueries(queries);
}

function matchesOwner(data: unknown, scope: SourceDetailScope) {
  if (typeof data !== "object" || data === null || !("source_id" in data))
    return undefined;
  if (data.source_id !== scope.sourceId) return false;
  if (scope.libraryId === undefined) return true;
  return "library_id" in data ? data.library_id === scope.libraryId : undefined;
}

function isSportsDetail(key: QueryKey) {
  return (
    (key[1] === "groups" && key[2] === "league") ||
    (key[1] === "items" && key[2] === "sports_event")
  );
}

export async function invalidateSourceDetails(
  client: QueryClient,
  scope?: SourceDetailScope,
) {
  const filters = {
    queryKey: sourceDetailKeys.root,
    predicate: scope
      ? (query: { queryKey: QueryKey; state: { data: unknown } }) => {
          const key = query.queryKey;
          if (!isSportsDetail(key)) return false;
          const data = query.state.data;
          if (Array.isArray(data)) {
            const owners = data.map((row) => matchesOwner(row, scope));
            if (owners.some((owner) => owner === true)) return true;
            if (owners.length && owners.every((owner) => owner === false))
              return false;
          } else {
            const owner = matchesOwner(data, scope);
            if (owner !== undefined) return owner;
          }
          if (key[1] === "groups" && typeof key[3] === "number") {
            const group = client.getQueryData(
              sourceDetailKeys.group(key[3], "league"),
            );
            const owner = matchesOwner(group, scope);
            if (owner !== undefined) return owner;
          }
          // Empty member lists and first fetches have no owner payload yet.
          // These prefixes contain only standalone sports, so retire them too.
          return true;
        }
      : undefined,
  };
  await client.cancelQueries(filters);
  await client.invalidateQueries(filters);
}

export async function invalidateSportsSource(
  client: QueryClient,
  scope: SourceDetailScope & { removed?: boolean },
) {
  const sports = {
    queryKey: [QueryKeys.Sports],
    predicate: ({ queryKey }: { queryKey: QueryKey }) => {
      const filters = queryKey[2];
      if (
        typeof filters !== "object" ||
        filters === null ||
        !("includeSources" in filters) ||
        filters.includeSources !== true
      )
        return false;
      const keys = "sourceKeys" in filters ? filters.sourceKeys : undefined;
      return (
        !Array.isArray(keys) ||
        keys.length === 0 ||
        keys.includes(`source:${scope.sourceId}`) ||
        (scope.removed === true && keys.includes("source:removed"))
      );
    },
  };
  const badges = {
    queryKey: [QueryKeys.System, QueryKeys.Badges, true],
    exact: true,
  };
  await Promise.all([
    client.cancelQueries(sports),
    client.cancelQueries(badges),
  ]);
  await Promise.all([
    client.invalidateQueries(sports),
    client.invalidateQueries(badges),
    invalidateSourceDetails(client, scope),
  ]);
}
