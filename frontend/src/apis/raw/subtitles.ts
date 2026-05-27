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
  type: "episode" | "movie" | "series";
  sonarrSeriesId?: number;
  sonarrEpisodeId?: number;
  radarrId?: number;
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

class SubtitlesApi extends BaseApi {
  constructor() {
    super("/subtitles");
  }

  async getRefTracksByEpisodeId(
    subtitlesPath: string,
    sonarrEpisodeId: number,
  ) {
    const response = await this.get<DataWrapper<Item.RefTracks>>("", {
      subtitlesPath,
      sonarrEpisodeId,
    });
    return response.data;
  }

  async getRefTracksByMovieId(
    subtitlesPath: string,
    radarrMovieId?: number | undefined,
  ) {
    const response = await this.get<DataWrapper<Item.RefTracks>>("", {
      subtitlesPath,
      radarrMovieId,
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

  async upgradable(): Promise<{ movies: number[]; series: number[] }> {
    const response = await this.get<{ movies: number[]; series: number[] }>(
      "/upgradable",
    );
    return response;
  }

  async getContent(mediaType: string, mediaId: number, language: string) {
    const base = mediaType === "episode" ? "episodes" : "movies";
    const url = `/${base}/${mediaId}/subtitles/${encodeURIComponent(language)}/content`;
    const response = await client.axios.get<SubtitleContentResponse>(url);
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
  ) {
    const base = mediaType === "episode" ? "episodes" : "movies";
    const url = `/${base}/${mediaId}/subtitles/${encodeURIComponent(language)}/content`;
    const headers: Record<string, string> = {};
    if (etag) {
      headers["If-Match"] = etag;
    }
    const response = await client.axios.put(
      url,
      { content, encoding },
      { headers },
    );
    return { etag: response.headers["etag"] as string | undefined };
  }

  async promoteSyncOutput(
    mediaType: string,
    mediaId: number,
    targetLanguage: string,
    sourceLanguage: string,
  ) {
    const base = mediaType === "episode" ? "episodes" : "movies";
    const url = `/${base}/${mediaId}/subtitles/${encodeURIComponent(targetLanguage)}/promote`;
    const response = await client.axios.post<{
      sourceLanguage: string;
      targetLanguage: string;
      targetPath: string;
    }>(url, { sourceLanguage });
    return response.data;
  }

  async getSyncStatus(
    mediaType: string,
    mediaId: number,
    language: string,
  ): Promise<SubtitleSyncStatus> {
    const base = mediaType === "episode" ? "episodes" : "movies";
    const url = `/${base}/${mediaId}/subtitles/${encodeURIComponent(language)}/sync-status`;
    const response = await client.axios.get<SubtitleSyncStatus>(url);
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
  ) {
    const base = mediaType === "episode" ? "episodes" : "movies";
    const url = `/${base}/${mediaId}/subtitles`;
    const response = await client.axios.post<{
      path: string;
      language: string;
    }>(url, {
      content,
      language,
      format,
      forced,
      hi,
    });
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
