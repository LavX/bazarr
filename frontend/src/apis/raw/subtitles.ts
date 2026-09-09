import type { SubtitleSyncStatus } from "@/utilities/subtitles";
import BaseApi from "./base";
import client from "./client";

export interface SubtitleContentResponse {
  exists?: boolean;
  content: string;
  encoding: string;
  format: string;
  language: string;
  size: number;
  lastModified: number;
  mediaTitle?: string;
  mediaId?: number;
  mediaUpstreamId?: number;
  episodeId?: number;
  episodeUpstreamId?: number;
  arrInstanceId?: number;
  episodeTitle?: string;
}

export type BatchAction =
  | "sync"
  | "translate"
  | "OCR_fixes"
  | "common"
  | "remove_HI"
  | "remove_tags"
  | "fix_uppercase"
  | "reverse_rtl"
  | "scan-disk"
  | "search-missing"
  | "upgrade";

export interface BatchItem {
  // "sports" names an event, as it does everywhere else in the sports code,
  // and "sportsLeague" names a league the way "series" names a show.
  type: "episode" | "movie" | "series" | "sports" | "sportsLeague";
  sonarrSeriesId?: number;
  sonarrEpisodeId?: number;
  radarrId?: number;
  // Local ids, not upstream ones: sports rows are addressed by primary key.
  sportsEventId?: number;
  sportsLeagueId?: number;
  // Owning Sonarr/Radarr/Sportarr instance id (#156) so the batch routes per
  // instance. Required for sports: a sports path mapping is always per
  // instance, with no global mapping to fall back on.
  arr_instance_id?: number;
}

export interface BatchOptions {
  maxOffsetSeconds?: number;
  noFixFramerate?: boolean;
  gss?: boolean;
  forceResync?: boolean;
  outputMode?: "overwrite" | "keep_all";
  fromLang?: string;
  toLang?: string;
}

/* eslint-disable camelcase -- backend API contract */
interface BatchPayload {
  max_offset_seconds?: number;
  no_fix_framerate?: boolean;
  gss?: boolean;
  force_resync?: boolean;
  output_mode?: "overwrite" | "keep_all";
  from_lang?: string;
  to_lang?: string;
}

function toBatchPayload(options: BatchOptions): BatchPayload {
  return {
    max_offset_seconds: options.maxOffsetSeconds,
    no_fix_framerate: options.noFixFramerate,
    gss: options.gss,
    force_resync: options.forceResync,
    output_mode: options.outputMode,
    from_lang: options.fromLang,
    to_lang: options.toLang,
  };
}
/* eslint-enable camelcase */

export interface BatchResponse {
  queued: number;
  skipped: number;
  errors: string[];
  job_id?: number;
}

interface UpgradableMovieKey {
  radarrId: number;
  arr_instance_id?: number | null;
}

interface UpgradableSeriesKey {
  sonarrSeriesId: number;
  arr_instance_id?: number | null;
}

export interface UpgradableResponse {
  movies: number[];
  series: number[];
  movieKeys?: UpgradableMovieKey[];
  seriesKeys?: UpgradableSeriesKey[];
}

export interface ArchiveExtractedFile {
  name: string;
  // base64-encoded file content
  content: string;
}

export interface ArchiveExtractResponse {
  files: ArchiveExtractedFile[];
  count: number;
}

// One place mapping a media type to its subtitle route segment. It used to be
// an inline "episode ? episodes : movies" in each caller, which silently sent
// a sports event to the movies route.
const SUBTITLE_ROUTE_BASE: Record<string, string> = {
  episode: "episodes",
  movie: "movies",
  sports: "sports/events",
};

class SubtitlesApi extends BaseApi {
  constructor() {
    super("/subtitles");
  }

  // Upload a compressed archive (.zip/.rar/.7z) and get back the contained
  // subtitle files (non-subtitle entries discarded) so they can be added to
  // the manual-upload flow. See LavX/bazarr issue #233.
  async extractArchive(file: File): Promise<ArchiveExtractResponse> {
    const response = await this.post<ArchiveExtractResponse>("/archive", {
      file,
    });
    return response.data;
  }

  async getRefTracksByEpisodeId(
    subtitlesPath: string,
    sonarrEpisodeId: number,
    arrInstanceId?: number,
  ) {
    const response = await this.get<DataWrapper<Item.RefTracks>>("", {
      subtitlesPath,
      sonarrEpisodeId,
      arr_instance_id: arrInstanceId,
    });
    return response.data;
  }

  async getRefTracksByMovieId(
    subtitlesPath: string,
    radarrMovieId?: number | undefined,
    arrInstanceId?: number,
  ) {
    const response = await this.get<DataWrapper<Item.RefTracks>>("", {
      subtitlesPath,
      radarrMovieId,
      arr_instance_id: arrInstanceId,
    });
    return response.data;
  }

  async getRefTracksBySportsEventId(
    subtitlesPath: string,
    sportsEventId: number,
    arrInstanceId?: number,
  ) {
    const response = await this.get<DataWrapper<Item.RefTracks>>("", {
      subtitlesPath,
      sportsEventId,
      arr_instance_id: arrInstanceId,
    });
    return response.data;
  }

  async info(names: string[]) {
    const response = await this.get<DataWrapper<SubtitleInfo[]>>(`/info`, {
      filenames: names,
    });
    return response.data;
  }

  async modify(action: string, form: FormType.ModifySubtitle) {
    await this.patch("", form, { action });
  }

  async batch(
    items: BatchItem[],
    action: BatchAction,
    options?: BatchOptions,
  ): Promise<BatchResponse> {
    const response = await this.postRaw<BatchResponse>("/batch", {
      items,
      action,
      options: options ? toBatchPayload(options) : undefined,
    });
    return response.data;
  }

  async upgradable(): Promise<UpgradableResponse> {
    const response = await this.get<UpgradableResponse>("/upgradable");
    return response;
  }

  async downloadFile(
    mediaType: "episode" | "movie" | "sports",
    mediaId: number,
    language: string,
    arrInstanceId?: number,
  ) {
    // language is the viewer/editor language key ("en", "en:hi", ...).
    // mediaId is the upstream id for episodes and movies, the local event id
    // for sports.
    const base = SUBTITLE_ROUTE_BASE[mediaType];
    return client.axios.get<Blob>(
      `/${base}/${mediaId}/subtitles/${encodeURIComponent(language)}/download`,
      { params: { arr_instance_id: arrInstanceId }, responseType: "blob" },
    );
  }

  async getContent(
    mediaType: string,
    mediaId: number,
    language: string,
    arrInstanceId?: number,
  ) {
    const base = SUBTITLE_ROUTE_BASE[mediaType] ?? "movies";
    const url = `/${base}/${mediaId}/subtitles/${encodeURIComponent(language)}/content`;
    const response = await client.axios.get<SubtitleContentResponse>(url, {
      params: { arr_instance_id: arrInstanceId },
    });
    return {
      data: response.data,
      etag: response.headers["etag"] as string | undefined,
    };
  }

  async saveContent(
    mediaType: string,
    mediaId: number,
    language: string,
    content: string,
    encoding: string,
    etag?: string,
    arrInstanceId?: number,
  ) {
    const base = SUBTITLE_ROUTE_BASE[mediaType] ?? "movies";
    const url = `/${base}/${mediaId}/subtitles/${encodeURIComponent(language)}/content`;
    const headers: Record<string, string> = {};
    if (etag) {
      headers["If-Match"] = etag;
    }
    const response = await client.axios.put(
      url,
      { content, encoding },
      { headers, params: { arr_instance_id: arrInstanceId } },
    );
    return { etag: response.headers["etag"] as string | undefined };
  }

  async promoteSyncOutput(
    mediaType: string,
    mediaId: number,
    targetLanguage: string,
    sourceLanguage: string,
    arrInstanceId?: number,
  ) {
    const base = SUBTITLE_ROUTE_BASE[mediaType] ?? "movies";
    const url = `/${base}/${mediaId}/subtitles/${encodeURIComponent(targetLanguage)}/promote`;
    const response = await client.axios.post<{
      sourceLanguage: string;
      targetLanguage: string;
      targetPath: string;
    }>(url, { sourceLanguage }, { params: { arr_instance_id: arrInstanceId } });
    return response.data;
  }

  async getSyncStatus(
    mediaType: string,
    mediaId: number,
    language: string,
    arrInstanceId?: number,
  ): Promise<SubtitleSyncStatus> {
    const base = SUBTITLE_ROUTE_BASE[mediaType] ?? "movies";
    const url = `/${base}/${mediaId}/subtitles/${encodeURIComponent(language)}/sync-status`;
    const response = await client.axios.get<SubtitleSyncStatus>(url, {
      params: { arr_instance_id: arrInstanceId },
    });
    return response.data;
  }

  async createSubtitle(
    mediaType: string,
    mediaId: number,
    content: string,
    language: string,
    format: string,
    forced: boolean,
    hi: boolean,
    arrInstanceId?: number,
  ) {
    const base = SUBTITLE_ROUTE_BASE[mediaType] ?? "movies";
    // /sports/events/<id>/subtitles is the indexer's own POST, so creating a
    // subtitle there needs its own path rather than a second handler on that one.
    const suffix = mediaType === "sports" ? "subtitles/create" : "subtitles";
    const url = `/${base}/${mediaId}/${suffix}`;
    const response = await client.axios.post<{
      path: string;
      language: string;
    }>(
      url,
      {
        content,
        language,
        format,
        forced,
        hi,
      },
      {
        params: { arr_instance_id: arrInstanceId },
      },
    );
    return response.data;
  }

  async contents(subtitlePath: string) {
    const response = await this.get<DataWrapper<SubtitleContents.Line[]>>(
      "/contents",
      {
        subtitlePath,
      },
    );
    return response.data;
  }
}

const subtitlesApi = new SubtitlesApi();
export default subtitlesApi;
