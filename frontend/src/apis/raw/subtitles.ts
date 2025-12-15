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
}

const subtitlesApi = new SubtitlesApi();
export default subtitlesApi;
