import BaseApi from "./base";

class SeriesApi extends BaseApi {
  constructor() {
    super("/series");
  }

  async series(ids?: number[]) {
    // Fetch by the canonical local id (#156); backend dual-accepts id[] and the
    // legacy seriesid[]. id == sonarrSeriesId on a single default instance.
    const response = await this.get<DataWrapperWithTotal<Item.Series>>("", {
      id: ids,
    });
    return response.data;
  }

  async seriesBy(params: Parameter.Range) {
    const response = await this.get<DataWrapperWithTotal<Item.Series>>(
      "",
      params,
    );
    return response;
  }

  async modify(form: FormType.ModifyItem) {
    await this.post("", { id: form.id, profileid: form.profileid });
  }

  async action(form: FormType.SeriesAction) {
    await this.patch("", form);
  }

  async downloadSubtitlesArchive(
    seriesid: number,
    options: {
      season?: number;
      language?: string;
      arrInstanceId?: number;
    } = {},
  ) {
    // Zip of the series' external subtitle files, optionally narrowed to one
    // season and/or one base language.
    return this.getBlob(`/${seriesid}/subtitles/download`, {
      season: options.season,
      language: options.language,
      arr_instance_id: options.arrInstanceId,
    });
  }
}

const seriesApi = new SeriesApi();
export default seriesApi;
