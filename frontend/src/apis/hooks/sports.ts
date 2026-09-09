import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { usePaginationQuery } from "@/apis/queries/hooks";
import { QueryKeys } from "@/apis/queries/keys";
import sports, { SportsFilters, SportsLeague } from "@/apis/raw/sports";
import { useArrInstances } from "./arrInstances";
// Imported from the defining module, not the barrel: index re-exports this
// file, so going through "." would close an import cycle.
import { useSystemSettings } from "./system";

export function useSportsAvailability() {
  const query = useArrInstances();
  const { data: settings } = useSystemSettings();
  const useSportarr = settings?.general?.use_sportarr ?? false;
  const instances =
    query.data?.filter(
      (instance) => instance.kind === "sportarr" && instance.enabled,
    ) ?? [];
  return {
    instances,
    // Both conditions matter: the master toggle is the operator's intent, and
    // an enabled instance is what there is to actually query.
    enabled: useSportarr && instances.length > 0,
    isLoading: query.isLoading,
  };
}
// A league in the shape the shared ItemView table expects, so Sports renders
// through the same component as Series and Movies instead of a bespoke grid.
// The sports API predates that view: it sends audio_language as bare codes and
// omits the fields Item.Base carries for an arr-managed title, so the gap is
// filled here rather than by widening the shared type for one caller.
export type SportsLeagueRow = Omit<
  SportsLeague,
  "audio_language" | "path" | "poster" | "fanart" | "overview"
> &
  Item.Base;

export function toSportsLeagueRow(league: SportsLeague): SportsLeagueRow {
  return {
    ...league,
    path: league.path ?? "",
    poster: league.poster ?? "",
    fanart: league.fanart ?? "",
    overview: league.overview ?? "",
    imdbId: "",
    alternativeTitles: [],
    year: "",
    audio_language: (league.audio_language ?? []).map((code) => ({
      code2: code,
      name: code,
    })),
  };
}

// The table-backed leagues query. Paginates through the same start/length
// contract the series and movies endpoints use, so usePaginationQuery drives it
// unchanged.
export function useSportsLeaguesPagination() {
  const { instances, enabled } = useSportsAvailability();
  return usePaginationQuery(
    [QueryKeys.Sports, "leagues", instances.map((instance) => instance.id)],
    async (param) => {
      // Guarded inside the fetcher rather than by an enabled flag: the page
      // still has to call this hook unconditionally, and returning an empty
      // page here means no request is made for an install with no enabled
      // Sportarr instead of a pointless 200 on every render.
      if (!enabled) {
        return { data: [], total: 0 };
      }
      const response = await sports.list(undefined, param.start, param.length);
      return {
        data: response.data.map(toSportsLeagueRow),
        total: response.total,
      };
    },
    false,
  );
}

export function useSportsLeagues(owner?: number, page = 1) {
  const { enabled, instances } = useSportsAvailability();
  return useQuery({
    queryKey: [
      QueryKeys.Sports,
      "leagues",
      owner,
      page,
      instances.map((instance) => instance.id),
    ],
    queryFn: () => sports.list(owner, (page - 1) * 100),
    enabled:
      enabled &&
      (owner === undefined ||
        instances.some((instance) => instance.id === owner)),
  });
}
export function useSportsLeague(id: number, owner?: number) {
  const { enabled, instances } = useSportsAvailability();
  return useQuery({
    queryKey: [
      QueryKeys.Sports,
      "league",
      id,
      owner,
      instances.map((instance) => instance.id),
    ],
    queryFn: () => sports.getOne(id, owner),
    enabled:
      enabled &&
      Number.isInteger(id) &&
      id > 0 &&
      (owner === undefined ||
        instances.some((instance) => instance.id === owner)),
  });
}
export function useSportsProfile() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      owner,
      profileId,
    }: {
      id: number;
      owner: number;
      profileId: number | null;
    }) => sports.assignProfile(id, owner, profileId),
    onSuccess: () => client.invalidateQueries({ queryKey: [QueryKeys.Sports] }),
  });
}
export function useSportsEvents(id: number, owner?: number, page = 1) {
  const { instances } = useSportsAvailability();
  return useQuery({
    queryKey: [
      QueryKeys.Sports,
      "events",
      id,
      owner,
      page,
      instances.map((instance) => instance.id),
    ],
    queryFn: () => sports.events(id, owner!, (page - 1) * 100),
    enabled:
      Number.isInteger(id) &&
      id > 0 &&
      instances.some((instance) => instance.id === owner),
  });
}
export function useSyncSports() {
  return useMutation({
    mutationFn: (owners: number[]) =>
      Promise.all(owners.map((owner) => sports.sync(owner))),
  });
}

export function useIndexSportsSubtitles() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, owner }: { id: number; owner: number }) =>
      sports.indexSubtitles(id, owner),
    onSettled: () => client.invalidateQueries({ queryKey: [QueryKeys.Sports] }),
  });
}

export function useSportsActivity(
  kind: "wanted" | "history" | "blacklist",
  filters: SportsFilters,
) {
  const { enabled, instances } = useSportsAvailability();
  return useQuery({
    queryKey: [
      QueryKeys.Sports,
      kind,
      filters,
      instances.map((instance) => instance.id),
    ],
    queryFn: () => sports.activity(kind, filters),
    enabled:
      enabled &&
      (filters.owner === undefined ||
        instances.some((instance) => instance.id === filters.owner)),
  });
}
export function useSportsAction() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ path, owner }: { path: string; owner: number }) =>
      sports.runAction(path, owner),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: [QueryKeys.Sports] });
    },
  });
}
export function useRemoveSportsExclusion() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ owner, id }: { owner: number; id?: number }) =>
      sports.removeExclusion(owner, id),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: [QueryKeys.Sports] });
    },
  });
}

export function useSportsJob(id?: number | null, owner?: number) {
  const { instances } = useSportsAvailability();
  return useQuery({
    queryKey: [QueryKeys.Sports, "job", id, owner],
    queryFn: () => sports.jobStatus(id!, owner!),
    enabled:
      !!id && !!owner && instances.some((instance) => instance.id === owner),
    refetchInterval: (query) =>
      query.state.data?.status === "completed" ||
      query.state.data?.status === "failed"
        ? false
        : 1000,
  });
}
