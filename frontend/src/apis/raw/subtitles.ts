import BaseApi from "./base";
import client from "./client";

export interface SubtitleContentResponse {
  content: string;
  encoding: string;
  format: string;
  language: string;
  size: number;
  lastModified: number;
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
  max_offset_seconds?: number;
  no_fix_framerate?: boolean;
  gss?: boolean;
  force_resync?: boolean;
  from_lang?: string;
  to_lang?: string;
}

export interface BatchResponse {
  queued: number;
  skipped: number;
  errors: string[];
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
      options,
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
    return response.data;
  }
}

const subtitlesApi = new SubtitlesApi();
export default subtitlesApi;
