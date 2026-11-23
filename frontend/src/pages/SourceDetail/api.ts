/* eslint-disable camelcase */
import { showNotification } from "@mantine/notifications";
import {
  type QueryFunctionContext,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { isAxiosError } from "axios";
import { refreshWhenJobFinishes } from "@/apis/hooks/jobWatch";
import { mediaSourcesKey, useMediaSources } from "@/apis/hooks/mediaSources";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import {
  invalidateSourceDetails,
  sourceDetailKeys,
} from "@/apis/queries/sourceDetails";
import { expireSourceSubtitleDocument } from "@/apis/queries/sourceSubtitleDocument";
import BaseApi from "@/apis/raw/base";
import { withInlineErrors } from "@/apis/raw/inlineWrites";
import mediaSources from "@/apis/raw/mediaSources";
import { notification } from "@/modules/notification";
import type { QueuedSearch } from "@/utilities/jobs";
import { buildSubtitleLanguageKey } from "@/utilities/subtitles";

export interface IdentityCandidate {
  id: number;
  title: string;
  year: string | null;
}

export interface SourceGroup {
  id: number;
  source_id: number;
  library_id: number;
  kind: "show" | "league";
  title: string;
  parsed_title: string | null;
  year: string | null;
  poster: string | null;
  imdb_id: string | null;
  profile_id: number | null;
  effective_profile_id?: number | null;
  monitored: boolean;
  identity_state: string;
  identity_candidates: IdentityCandidate[];
  path?: string;
}

export interface SourceItem extends Omit<SourceGroup, "kind"> {
  kind: "movie" | "episode" | "sports_event";
  group_id: number | null;
  path: string;
  scene_name: string | null;
  season: number | null;
  episode: number | null;
  event_date?: string | null;
  part_name?: string | null;
  subtitles: Subtitle[];
  missing_subtitles: Language.Info[];
  arr_shadow: boolean;
  shadowed_by?: string | null;
  audio_language?: (string | null)[];
  server_missing?: boolean;
  upgradable?: boolean;
}

export type SourceItemTarget = Pick<
  SourceItem,
  | "id"
  | "kind"
  | "source_id"
  | "library_id"
  | "group_id"
  | "path"
  | "server_missing"
  | "arr_shadow"
>;

type StoredSubtitle = [
  string,
  string | null,
  number | null,
  (string | null)?,
  (string | null)?,
];
type RawSourceItem = Omit<SourceItem, "subtitles" | "missing_subtitles"> & {
  subtitles: (Subtitle | StoredSubtitle)[];
  missing_subtitles: (Language.Info | string)[];
  subtitle_details?: Subtitle[];
  missing_subtitle_details?: Language.Info[];
};

function languageInfo(
  key: string,
): Language.Info & { hi: boolean; forced: boolean } {
  const [code2, ...flags] = key.split(":");
  return {
    code2,
    name: code2,
    hi: flags.includes("hi"),
    forced: flags.includes("forced"),
  };
}

export function normalizeSourceItem(item: RawSourceItem): SourceItem {
  return {
    ...item,
    subtitles:
      item.subtitle_details ??
      (item.subtitles ?? []).map((subtitle) =>
        Array.isArray(subtitle)
          ? {
              ...languageInfo(subtitle[0]),
              language: subtitle[0],
              path: subtitle[1],
              codec: subtitle[3] ?? null,
              ...(subtitle[4] ? { title: subtitle[4] } : {}),
            }
          : subtitle,
      ),
    missing_subtitles:
      item.missing_subtitle_details ??
      (item.missing_subtitles ?? []).map((language) =>
        typeof language === "string" ? languageInfo(language) : language,
      ),
  };
}

export type IdentityCorrection =
  | { candidate_id: number }
  | {
      title: string;
      year?: number;
      season?: number | null;
      episode?: number;
      round?: number | null;
      event_date?: string | null;
      part_name?: string | null;
    };

export { sourceDetailKeys };

class SourceDetailApi extends BaseApi {
  constructor() {
    super("/sources");
  }

  group(id: number, signal?: AbortSignal, kind: SourceGroup["kind"] = "show") {
    return this.get<SourceGroup>(`/groups/${id}`, { kind }, signal);
  }

  async item(
    id: number,
    signal?: AbortSignal,
    kind: SourceItem["kind"] = "movie",
  ) {
    return normalizeSourceItem(
      await this.get<RawSourceItem>(`/items/${id}`, { kind }, signal),
    );
  }

  async leagueEvents(id: number, signal?: AbortSignal) {
    const result = await this.get<{ data: RawSourceItem[]; total: number }>(
      `/groups/${id}/items`,
      undefined,
      signal,
    );
    return result.data.map(normalizeSourceItem);
  }

  async episodes(
    id: number,
    start: number,
    length: number,
    signal?: AbortSignal,
    includeScores = false,
  ) {
    const page = await this.get<{ data: RawSourceItem[]; total: number }>(
      "/items",
      {
        group_id: id,
        kind: "episode",
        start,
        length,
        scores: includeScores ? 1 : undefined,
      },
      signal,
    );
    return { ...page, data: page.data.map(normalizeSourceItem) };
  }

  async requireCurrentItem(
    item: SourceItemTarget,
    subtitle?: Subtitle,
    allowShadow = false,
  ) {
    if (!sourceItemHasFile(item, allowShadow)) {
      throw new Error("The source item is no longer available.");
    }
    const current = await withInlineErrors(() =>
      this.item(item.id, undefined, item.kind),
    );
    if (
      !sourceItemHasFile(current, allowShadow) ||
      !sameSourceItem(item, current)
    ) {
      throw new Error("The source item moved or is no longer available.");
    }
    if (
      subtitle &&
      !current.subtitles.some(
        (row) =>
          row.path === subtitle.path &&
          buildSubtitleLanguageKey(row) === buildSubtitleLanguageKey(subtitle),
      )
    ) {
      throw new Error("This subtitle is no longer listed for the source item.");
    }
    const owners = await queryClient.fetchQuery({
      queryKey: mediaSourcesKey,
      queryFn: ({ signal }) =>
        withInlineErrors(() => mediaSources.list(signal)),
      staleTime: 0,
      retry: false,
    });
    if (!sourceOwnerAvailable(owners.data, current)) {
      throw new Error("The source or library is no longer available.");
    }
    return current;
  }

  requireCurrentManualItem(item: SourceItemTarget) {
    return this.requireCurrentItem(
      item,
      undefined,
      item.kind === "sports_event",
    );
  }

  async updateProfile(
    kind: "groups" | "items",
    id: number,
    profileId: number | null,
    expectedKind: SourceGroup["kind"] | SourceItem["kind"] = kind === "groups"
      ? "show"
      : "movie",
  ) {
    const { data } = await this.patchRaw<SourceGroup | SourceItem>(
      `/${kind}/${id}`,
      { profile_id: profileId },
      { kind: expectedKind },
    );
    return data;
  }

  async identify(
    kind: "groups" | "items",
    id: number,
    correction: IdentityCorrection,
    expectedKind: SourceGroup["kind"] | SourceItem["kind"] = kind === "groups"
      ? "show"
      : "movie",
  ) {
    const { data } = await this.patchRaw<SourceGroup | SourceItem>(
      `/${kind}/${id}`,
      correction,
      { kind: expectedKind },
    );
    return data;
  }

  async search(id: number) {
    const { data } = await this.postRaw<QueuedSearch>(`/items/${id}/search`);
    return data;
  }

  async searchGroup(id: number) {
    const { data } = await this.postRaw<QueuedSearch>(`/groups/${id}/search`);
    return data;
  }

  async scanLibrary(sourceId: number, libraryId: number) {
    return { job_id: await mediaSources.scanLibrary(sourceId, libraryId) };
  }

  async manualSearch(id: number) {
    const { data } = await this.postRaw<QueuedSearch>(
      `/items/${id}/manual/search`,
    );
    return data;
  }

  async download(id: number, result: SearchResultType) {
    const { data } = await this.post<QueuedSearch>(`/items/${id}/manual`, {
      language: result.language,
      hi: result.hearing_impaired,
      forced: result.forced,
      provider: result.provider,
      subtitle: result.subtitle,
      original_format: result.original_format,
    });
    return data;
  }

  async upload(
    id: number,
    file: File,
    language: string,
    hi: boolean,
    forced: boolean,
  ) {
    const form = new FormData();
    form.append("file", file, file.name);
    form.append("language", language);
    form.append("hi", String(hi));
    form.append("forced", String(forced));
    const { data } = await this.postRaw<QueuedSearch>(
      `/items/${id}/subtitles`,
      form,
      undefined,
      { "Content-Type": "multipart/form-data" },
    );
    return data;
  }

  async removeSubtitle(id: number, subtitle: Subtitle) {
    await withInlineErrors(() =>
      this.delete(`/items/${id}/subtitles`, {
        subtitles_path: subtitle.path,
        language: subtitle.code2,
        hi: subtitle.hi,
        forced: subtitle.forced,
      }),
    );
    void expireSourceSubtitleDocument(
      queryClient,
      id,
      buildSubtitleLanguageKey(subtitle),
    );
  }
}

export const sourceDetailApi = new SourceDetailApi();

export function sameSourceItem(item: SourceItemTarget, current: SourceItem) {
  return (
    current.id === item.id &&
    current.kind === item.kind &&
    current.source_id === item.source_id &&
    current.library_id === item.library_id &&
    current.group_id === item.group_id &&
    current.path === item.path
  );
}

export function sourceItemHasFile(
  item: Pick<SourceItemTarget, "path" | "server_missing" | "arr_shadow">,
  allowShadow = false,
) {
  return Boolean(
    item.path && !item.server_missing && (allowShadow || !item.arr_shadow),
  );
}

type SourceOwner = Pick<SourceGroup, "source_id" | "library_id">;
type UnavailableSportsDetail = Partial<SourceOwner> & { unavailable: true };
type SportsDetail<T> = T | UnavailableSportsDetail;

async function readSportsDetail<T extends SourceOwner | SourceOwner[]>(
  { client, queryKey }: QueryFunctionContext,
  call: () => Promise<T>,
): Promise<SportsDetail<T>> {
  try {
    return await withInlineErrors(call);
  } catch (error) {
    if (!isAxiosError(error) || error.response?.status !== 404) throw error;
    // Keep only the owner so invalidation stays scoped after removal.
    const cached =
      client.getQueryData<SportsDetail<SourceOwner | SourceOwner[]>>(queryKey);
    const owner = Array.isArray(cached) ? cached[0] : cached;
    return owner?.source_id === undefined
      ? { unavailable: true }
      : {
          unavailable: true,
          source_id: owner.source_id,
          library_id: owner.library_id,
        };
  }
}

function sportsDetailOptions<T extends SourceOwner | SourceOwner[]>() {
  return {
    select: (data: SportsDetail<T>): T | null =>
      "unavailable" in data ? null : data,
    refetchOnMount: ({
      state,
    }: {
      state: { data: SportsDetail<T> | undefined };
    }): true | "always" =>
      state.data && "unavailable" in state.data ? "always" : true,
  };
}

export function useSourceOwnerAvailable(
  item: Pick<SourceGroup, "source_id" | "library_id">,
) {
  const { data, isEnabled } = useMediaSources();
  return isEnabled && sourceOwnerAvailable(data?.data, item);
}

export function sourceOwnerAvailable(
  sources: Awaited<ReturnType<typeof mediaSources.list>>["data"] | undefined,
  item: Pick<SourceGroup, "source_id" | "library_id">,
) {
  return (
    sources?.some(
      (source) =>
        source.id === item.source_id &&
        source.enabled &&
        source.libraries.some(
          (library) => library.id === item.library_id && library.enabled,
        ),
    ) === true
  );
}

export function useSourceGroup(id: number, kind: SourceGroup["kind"] = "show") {
  return useQuery({
    ...sportsDetailOptions<SourceGroup>(),
    queryKey: sourceDetailKeys.group(id, kind),
    queryFn: (context) => {
      const read = () => sourceDetailApi.group(id, context.signal, kind);
      return kind === "league"
        ? readSportsDetail(context, read)
        : withInlineErrors(read);
    },
    enabled: Number.isSafeInteger(id) && id > 0,
    placeholderData: undefined,
    retry: false,
  });
}

export function useSourceItem(id: number, kind: SourceItem["kind"] = "movie") {
  return useQuery({
    ...sportsDetailOptions<SourceItem>(),
    queryKey: sourceDetailKeys.item(id, kind),
    queryFn: (context) => {
      const read = () => sourceDetailApi.item(id, context.signal, kind);
      return kind === "sports_event"
        ? readSportsDetail(context, read)
        : withInlineErrors(read);
    },
    enabled: Number.isSafeInteger(id) && id > 0,
    placeholderData: undefined,
    retry: false,
  });
}

export function useSourceLeagueEvents(id: number) {
  return useQuery({
    ...sportsDetailOptions<SourceItem[]>(),
    queryKey: sourceDetailKeys.leagueEvents(id),
    queryFn: (context) =>
      readSportsDetail(context, () =>
        sourceDetailApi.leagueEvents(id, context.signal),
      ),
    enabled: Number.isSafeInteger(id) && id > 0,
    placeholderData: undefined,
    retry: false,
  });
}

export function useSourceEpisodes(id: number, page: number) {
  return useQuery({
    queryKey: [...sourceDetailKeys.episodes(id), page],
    queryFn: ({ signal }) =>
      withInlineErrors(() =>
        sourceDetailApi.episodes(id, (page - 1) * 50, 50, signal),
      ),
    enabled: Number.isSafeInteger(id) && id > 0,
    placeholderData: undefined,
    retry: false,
  });
}

export function useRefreshSourceDetails() {
  const client = useQueryClient();
  return async () => {
    await Promise.all([
      invalidateSourceDetails(client),
      client.invalidateQueries({ queryKey: [QueryKeys.Series, "library"] }),
      client.invalidateQueries({ queryKey: [QueryKeys.Movies, "library"] }),
      client.invalidateQueries({ queryKey: [QueryKeys.Sports] }),
      client.invalidateQueries({
        queryKey: [QueryKeys.System, QueryKeys.Badges],
      }),
      client.invalidateQueries({
        predicate: ({ queryKey }) =>
          (queryKey[0] === QueryKeys.Series ||
            queryKey[0] === QueryKeys.Movies) &&
          queryKey.some(
            (part) =>
              typeof part === "object" &&
              part !== null &&
              "includeSources" in part &&
              part.includeSources === true,
          ),
      }),
    ]);
  };
}

export function useSourceWrite({ notifyError = false } = {}) {
  const refresh = useRefreshSourceDetails();
  return useMutation({
    mutationFn: (write: () => Promise<unknown>) => withInlineErrors(write),
    onSuccess: refresh,
    onError: notifyError
      ? (error) =>
          showNotification(
            notification.error("Subtitle action failed", sourceError(error)),
          )
      : undefined,
  });
}

export function useSourceSearch(kind: "items" | "groups" = "items") {
  const client = useQueryClient();
  const refresh = useRefreshSourceDetails();
  return useMutation({
    mutationFn: (id: number) =>
      withInlineErrors(() =>
        kind === "groups"
          ? sourceDetailApi.searchGroup(id)
          : sourceDetailApi.search(id),
      ),
    onSuccess: (job) => refreshWhenJobFinishes(client, job.job_id, refresh),
  });
}

export function useSourceScan() {
  const client = useQueryClient();
  const refresh = useRefreshSourceDetails();
  return useMutation({
    mutationFn: (item: Pick<SourceGroup, "source_id" | "library_id">) =>
      withInlineErrors(() =>
        sourceDetailApi.scanLibrary(item.source_id, item.library_id),
      ),
    onSuccess: (job) => refreshWhenJobFinishes(client, job.job_id, refresh),
  });
}

export function sourceError(error: unknown) {
  if (isAxiosError<{ message?: string } | string>(error)) {
    const data = error.response?.data;
    if (typeof data === "string" && data) return data;
    if (typeof data === "object" && data?.message) return data.message;
  }
  return error instanceof Error ? error.message : "The request failed.";
}
