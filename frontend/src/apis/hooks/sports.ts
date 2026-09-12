import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { usePaginationQuery } from "@/apis/queries/hooks";
import { QueryKeys } from "@/apis/queries/keys";
import sports, {
  SportsEvent,
  SportsEventReference,
  SportsFilters,
  SportsLeague,
  SportsRecord,
} from "@/apis/raw/sports";
import { useArrInstances } from "./arrInstances";
// Imported from the defining module, not the barrel: index re-exports this
// file, so going through "." would close an import cycle.
import { useSystemSettings } from "./system";

export function useSportsAvailability() {
  const query = useArrInstances();
  const { data: settings, isLoading: settingsLoading } = useSystemSettings();
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
    isLoading: query.isLoading || settingsLoading,
  };
}
// A league in the shape the shared ItemView table expects, so Sports renders
// through the same component as Series and Movies instead of a bespoke grid.
// The sports API predates that view: it sends audio_language as bare codes and
// omits the fields Item.Base carries for an arr-managed title, so the gap is
// filled here rather than by widening the shared type for one caller.
export type SportsLeagueRow = Omit<
  SportsLeague,
  "audio_language" | "tags" | "path" | "poster" | "fanart" | "overview"
> &
  Item.Base;

export function toSportsLeagueRow(league: SportsLeague): SportsLeagueRow {
  return {
    ...league,
    // ItemOverview reads item.tags.length unguarded, so a league synced
    // without tags would crash the whole detail page.
    tags: league.tags ?? [],
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
export function useSportsLeaguesPagination(fetchAll = false) {
  const { instances, enabled } = useSportsAvailability();
  return usePaginationQuery(
    [QueryKeys.Sports, "leagues", instances.map((instance) => instance.id)],
    async (param) => {
      const response = await sports.list(undefined, param.start, param.length);
      return {
        data: response.data.map(toSportsLeagueRow),
        total: response.total,
      };
    },
    false,
    // The library page filters by title, audio language and instance on the
    // client, over query.data only. Without fetch-all those filters silently
    // searched one page, exactly as the Series and Movies pages would if they
    // did not request every row while a filter is active.
    fetchAll,
    enabled,
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
export function useSportsProfiles() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (
      assignments: { id: number; owner: number; profileId: number | null }[],
    ) => sports.assignProfiles(assignments),
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
    // Every event, not the first hundred. The detail page never exposed a page
    // control, and its table, upload picker, subtitle-tools payload and
    // archive language/season choices are all derived from this one list, so a
    // league with more than one page of playable files silently lost the rest
    // from every one of them. -1 is the fetch-all length the shared endpoints
    // already speak.
    queryFn: () => sports.events(id, owner!, (page - 1) * 100, -1),
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

// A sports event in the shape the shared WantedView table expects. The sports
// API sends missing_subtitles as bare language keys ("en", "hu:hi") while
// Wanted.Base carries Subtitle objects, so the gap is closed here rather than by
// widening the shared type for one caller.
export type SportsWantedRow = Wanted.Base &
  SportsEventReference & {
    title: string;
    // Carried so a row can open the manual search, which offers languages from
    // the event's profile.
    profileId: number | null;
    // Bare code2s, not the raw names the sports API stores: the indexer reads
    // audio tracks off the file as names ("Hungarian") so the audio profile
    // rules can resolve them, while the wanted filters and the Audio column
    // speak code2. toSportsWantedRow translates through the library-wide
    // language catalogue; an unmapped name is kept as-is so no entry silently
    // vanishes.
    audio_language: string[];
  };

// The empty default keeps the mapper usable without a catalogue, and the
// ReadonlyMap shape matches what useAudioLanguages-derived maps provide.
const EMPTY_AUDIO_LANGUAGE_MAP: ReadonlyMap<string, string> = new Map();

function toSubtitle(key: string): Subtitle {
  const [code2, ...modifiers] = key.split(":");
  const lower = modifiers.map((modifier) => modifier.toLowerCase());
  return {
    code2,
    name: code2,
    hi: lower.includes("hi"),
    forced: lower.includes("forced"),
    // A wanted language names a file that does not exist yet.
    path: null,
  };
}

export function toSportsWantedRow(
  event: SportsEvent,
  nameToCode: ReadonlyMap<string, string> = EMPTY_AUDIO_LANGUAGE_MAP,
): SportsWantedRow {
  return {
    ...event,
    title: event.title,
    monitored: true,
    tags: [],
    sceneName: event.sceneName ?? undefined,
    hearing_impaired: false,
    missing_subtitles: (event.missing_subtitles ?? []).map(toSubtitle),
    audio_language: (event.audio_language ?? []).map(
      (name) => nameToCode.get(name) ?? name,
    ),
    release_mismatch: (event as SportsEvent & { release_mismatch?: boolean })
      .release_mismatch,
  };
}

// A sports history or exclusion record in the shape the shared HistoryView and
// blacklist tables expect.
export type SportsActivityRow = Omit<
  SportsRecord,
  "language" | "description" | "provider" | "score" | "subs_id"
> &
  History.Base & {
    league_id: number;
    event_id: number;
    title: string;
    score_out_of?: number | null;
    /** The raw indexed key ("en", "en:hi"), which the filter matches on. */
    language_key: string | null;
    /** The raw provider score, which the percentage column divides. */
    score_value: number | null;
  };

export function toSportsActivityRow(record: SportsRecord): SportsActivityRow {
  return {
    ...record,
    action: record.action ?? -1,
    description: record.description ?? "",
    // The shared History columns render a Language.Info, while the sports
    // tables store the indexed key. Both are kept: the badge needs the object,
    // the filter matches the key.
    language: record.language ? toSubtitle(record.language) : undefined,
    language_key: record.language,
    provider: record.provider ?? undefined,
    // History.Base types score as the display string; the raw number stays
    // beside it because the sports column shows a percentage of score_out_of.
    score:
      record.score != null && record.score_out_of
        ? `${Math.round((record.score * 100) / record.score_out_of)}%`
        : undefined,
    score_value: record.score ?? null,
    subs_id: record.subs_id ?? undefined,
    // The API sends these two now: blacklisted is an exact match against the
    // sports exclusion table on (provider, subs_id, owner), and upgradable is
    // the display-level check the upgrade run refines. They were hardcoded
    // false, so an already-excluded entry still offered an active Blacklist
    // action and re-posting it queued a duplicate exclusion.
    blacklisted: record.blacklisted ?? false,
    upgradable: record.upgradable ?? false,
    // The API parses the stored criteria reprs into these lists, matching the
    // episodes and movies endpoints; the shared Match cell reads them.
    matches: record.matches ?? [],
    dont_matches: record.dont_matches ?? [],
    // The rest the sports tables genuinely do not record. Stated rather than
    // left undefined so a shared column reaching for a `.length` cannot crash.
    monitored: true,
    tags: [],
    parsed_timestamp: record.parsed_timestamp ?? "",
    timestamp: record.timestamp ?? "",
    subtitles_path: record.subtitles_path ?? "",
  };
}

function useSportsActivityPagination<T extends object>(
  kind: "wanted" | "history" | "blacklist",
  filters: SportsFilters,
  map: (row: never) => T,
  fetchAll = false,
  keySignatures: unknown[] = [],
) {
  const { enabled, instances } = useSportsAvailability();
  const ownerKnown =
    filters.owner === undefined ||
    instances.some((instance) => instance.id === filters.owner);
  return usePaginationQuery<T>(
    [
      QueryKeys.Sports,
      kind,
      filters,
      ...keySignatures,
      instances.map((instance) => instance.id),
    ],
    async (param) => {
      const response = await sports.activity(
        kind,
        filters,
        param.start,
        param.length,
      );
      return {
        data: response.data.map(map as (row: unknown) => T),
        total: response.total,
      };
    },
    false,
    fetchAll,
    enabled && ownerKnown,
  );
}

export function useSportsWantedPagination(
  filters: SportsFilters,
  fetchAll = false,
  nameToCode: ReadonlyMap<string, string> = EMPTY_AUDIO_LANGUAGE_MAP,
) {
  // toSportsWantedRow derives code2s at fetch time, so the derivation is part
  // of the cached rows. On a cold cache the wanted rows can resolve before
  // /system/languages/audio lands; the signature changes when the catalogue
  // does, re-fetching the rows through the real map instead of caching
  // unmapped names until the next refetch.
  const nameToCodeSignature = Array.from(nameToCode.entries()).sort();
  return useSportsActivityPagination<SportsWantedRow>(
    "wanted",
    filters,
    ((event) => toSportsWantedRow(event, nameToCode)) as (
      row: never,
    ) => SportsWantedRow,
    fetchAll,
    // A plain-data signature rather than the Map object: same content, same
    // cache key across renders, and a change to the catalogue is a new key.
    [nameToCodeSignature],
  );
}

export function useSportsHistoryPagination(filters: SportsFilters) {
  return useSportsActivityPagination<SportsActivityRow>(
    "history",
    filters,
    toSportsActivityRow as (row: never) => SportsActivityRow,
  );
}

export function useSportsBlacklistPagination(filters: SportsFilters) {
  return useSportsActivityPagination<SportsActivityRow>(
    "blacklist",
    filters,
    toSportsActivityRow as (row: never) => SportsActivityRow,
  );
}

export function useSportsAction() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({
      path,
      owner,
      language,
    }: {
      path: string;
      owner: number;
      language?: string;
    }) => sports.runAction(path, owner, language),
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

// The per-subtitle actions the shared Subtitle Tools modal needs: search for a
// specific language, and remove a file Bazarr placed. Named to match
// useEpisodeSubtitleModification and useMovieSubtitleModification, because the
// modal picks one of the three by media type.
export function useSportsSubtitleModification() {
  const client = useQueryClient();
  const invalidate = () => {
    void client.invalidateQueries({ queryKey: [QueryKeys.Sports] });
  };
  return {
    download: useMutation({
      mutationKey: [QueryKeys.Sports, QueryKeys.Subtitles, "download"],
      mutationFn: ({ eventId, owner }: { eventId: number; owner: number }) =>
        sports.runAction(`/events/${eventId}/automatic`, owner),
      onSuccess: invalidate,
    }),
    upload: useMutation({
      mutationKey: [QueryKeys.Sports, QueryKeys.Subtitles, "upload"],
      mutationFn: ({
        eventId,
        owner,
        form,
      }: {
        eventId: number;
        owner: number;
        form: { file: File; language: string; hi: boolean; forced: boolean };
      }) => sports.uploadSubtitle(eventId, owner, form),
      onSuccess: invalidate,
    }),
    remove: useMutation({
      mutationKey: [QueryKeys.Sports, QueryKeys.Subtitles, "remove"],
      mutationFn: ({
        eventId,
        owner,
        form,
      }: {
        eventId: number;
        owner: number;
        form: {
          language: string;
          path: string;
          hi?: boolean;
          forced?: boolean;
        };
      }) => sports.removeSubtitle(eventId, owner, form),
      onSuccess: invalidate,
    }),
  };
}

export function useSportsJob(id?: number | null, owner?: number) {
  const { instances } = useSportsAvailability();
  return useQuery({
    queryKey: [QueryKeys.Sports, "job", id, owner],
    queryFn: () => sports.jobStatus(id!, owner!),
    enabled:
      !!id && !!owner && instances.some((instance) => instance.id === owner),
    refetchInterval: (query) =>
      query.state.status === "error" ||
      query.state.data?.status === "completed" ||
      query.state.data?.status === "failed"
        ? false
        : 1000,
  });
}
