import BaseApi from "./base";

export interface BatchTranslateItem {
  type: "episode" | "movie";
  sonarrSeriesId?: number;
  sonarrEpisodeId?: number;
  radarrId?: number;
  sourceLanguage: string;
  targetLanguage: string;
  subtitlePath?: string;
  forced?: boolean;
  hi?: boolean;
}

export interface BatchTranslateResponse {
  queued: number;
  skipped: number;
  errors: string[];
}

export interface BatchSyncItem {
  type: "episode" | "movie" | "series";
  sonarrSeriesId?: number;
  sonarrEpisodeId?: number;
  radarrId?: number;
}

export interface BatchSyncOptions {
  max_offset_seconds: number;
  no_fix_framerate: boolean;
  gss: boolean;
  force_resync: boolean;
}

export interface BatchSyncResponse {
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

  async batchTranslate(
    items: BatchTranslateItem[],
  ): Promise<BatchTranslateResponse> {
    const response = await this.postRaw<BatchTranslateResponse>(
      "/translate/batch",
      { items },
    );
    return response.data;
  }

  async batchSync(
    items: BatchSyncItem[],
    options: BatchSyncOptions,
  ): Promise<BatchSyncResponse> {
    const response = await this.postRaw<BatchSyncResponse>(
      "/sync/batch",
      { items, options },
    );
    return response.data;
  }
}

const subtitlesApi = new SubtitlesApi();
export default subtitlesApi;
