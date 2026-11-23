/* eslint-disable camelcase */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { isAxiosError } from "axios";
import { refreshWhenJobFinishes } from "@/apis/hooks/jobWatch";
import { useMediaSources } from "@/apis/hooks/mediaSources";
import { QueryKeys } from "@/apis/queries/keys";
import BaseApi from "@/apis/raw/base";
import { withInlineErrors } from "@/apis/raw/inlineWrites";
import mediaSources from "@/apis/raw/mediaSources";
import type { QueuedSearch } from "@/utilities/jobs";

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
  audio_language?: (string | null)[];
  server_missing?: boolean;
}

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
      season?: number;
      episode?: number;
      round?: number;
      event_date?: string;
      part_name?: string | null;
    };

export const sourceDetailKeys = {
  root: ["source-detail"] as const,
  group: (id: number, kind: SourceGroup["kind"] = "show") =>
    ["source-detail", "groups", kind, id] as const,
  item: (id: number, kind: SourceItem["kind"] = "movie") =>
    ["source-detail", "items", kind, id] as const,
  episodes: (id: number) => ["source-detail", "groups", id, "items"] as const,
};

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
  ) {
    const page = await this.get<{ data: RawSourceItem[]; total: number }>(
      "/items",
      { group_id: id, kind: "episode", start, length },
      signal,
    );
    return { ...page, data: page.data.map(normalizeSourceItem) };
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

  removeSubtitle(id: number, subtitle: Subtitle) {
    return this.delete(`/items/${id}/subtitles`, {
      subtitles_path: subtitle.path,
      language: subtitle.code2,
      hi: subtitle.hi,
      forced: subtitle.forced,
    });
  }

  excludeSubtitle(
    id: number,
    sourceId: number,
    path: string,
    key: string,
    provider: string,
    releaseId: string,
  ) {
    const language = languageInfo(key);
    return this.post(`/items/${id}/blacklist`, {
      source_id: sourceId,
      subtitles_path: path,
      language: language.code2,
      hi: language.hi,
      forced: language.forced,
      provider,
      subs_id: releaseId,
    });
  }
}

export const sourceDetailApi = new SourceDetailApi();

export function useSourceOwnerAvailable(
  item: Pick<SourceGroup, "source_id" | "library_id">,
) {
  const { data, isEnabled } = useMediaSources();
  const source = data?.data.find((value) => value.id === item.source_id);
  return (
    isEnabled &&
    source?.enabled === true &&
    source.libraries.some(
      (library) => library.id === item.library_id && library.enabled,
    )
  );
}

export function useSourceGroup(id: number, kind: SourceGroup["kind"] = "show") {
  return useQuery({
    queryKey: sourceDetailKeys.group(id, kind),
    queryFn: ({ signal }) =>
      withInlineErrors(() => sourceDetailApi.group(id, signal, kind)),
    enabled: Number.isSafeInteger(id) && id > 0,
    placeholderData: undefined,
    retry: false,
  });
}

export function useSourceItem(id: number, kind: SourceItem["kind"] = "movie") {
  return useQuery({
    queryKey: sourceDetailKeys.item(id, kind),
    queryFn: ({ signal }) =>
      withInlineErrors(() => sourceDetailApi.item(id, signal, kind)),
    enabled: Number.isSafeInteger(id) && id > 0,
    placeholderData: undefined,
    retry: false,
  });
}

export function useSourceLeagueEvents(id: number) {
  return useQuery({
    queryKey: [...sourceDetailKeys.group(id, "league"), "items"],
    queryFn: ({ signal }) =>
      withInlineErrors(() => sourceDetailApi.leagueEvents(id, signal)),
    enabled: Number.isSafeInteger(id) && id > 0,
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
      client.invalidateQueries({ queryKey: sourceDetailKeys.root }),
      client.invalidateQueries({ queryKey: [QueryKeys.Series, "library"] }),
      client.invalidateQueries({ queryKey: [QueryKeys.Movies, "library"] }),
      client.invalidateQueries({ queryKey: [QueryKeys.Sports] }),
      client.invalidateQueries({
        queryKey: [QueryKeys.System, QueryKeys.Badges],
      }),
      client.invalidateQueries({
        queryKey: [QueryKeys.Series, QueryKeys.Wanted],
      }),
      client.invalidateQueries({
        queryKey: [QueryKeys.Movies, QueryKeys.Wanted],
      }),
      client.invalidateQueries({
        queryKey: [QueryKeys.Series, QueryKeys.Episodes, QueryKeys.History],
      }),
      client.invalidateQueries({
        queryKey: [QueryKeys.Movies, QueryKeys.History],
      }),
    ]);
  };
}

export function useSourceWrite() {
  const refresh = useRefreshSourceDetails();
  return useMutation({
    mutationFn: (write: () => Promise<unknown>) => withInlineErrors(write),
    onSuccess: refresh,
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
  if (
    isAxiosError<{ message?: string }>(error) &&
    error.response?.data?.message
  )
    return error.response.data.message;
  return error instanceof Error ? error.message : "The request failed.";
}
