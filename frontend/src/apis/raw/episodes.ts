import BaseApi from "./base";

class EpisodeApi extends BaseApi {
  constructor() {
    super("/episodes");
  }

  async bySeriesId(ids: number[]) {
    // Fetch by the local series id (#156); backend dual-accepts series_id[] and
    // the legacy seriesid[]. series_id == sonarrSeriesId single-instance.
    const response = await this.get<DataWrapper<Item.Episode[]>>("", {
      series_id: ids,
    });
    return response.data;
  }

  async byEpisodeId(ids: number[]) {
    // Fetch by the local episode id (#156); backend dual-accepts id[] + episodeid[].
    const response = await this.get<DataWrapper<Item.Episode[]>>("", {
      id: ids,
    });
    return response.data;
  }

  async wanted(params: Parameter.Range) {
    const response = await this.get<DataWrapperWithTotal<Wanted.Episode>>(
      "/wanted",
      params,
    );
    return response;
  }

  async wantedBy(episodeid: number[]) {
    const response = await this.get<DataWrapperWithTotal<Wanted.Episode>>(
      "/wanted",
      { episodeid },
    );
    return response;
  }

  async history(params: Parameter.Range) {
    const response = await this.get<DataWrapperWithTotal<History.Episode>>(
      "/history",
      params,
    );
    return response;
  }

  async historyBy(id: number) {
    const response = await this.get<DataWrapperWithTotal<History.Episode>>(
      "/history",
      { id },
    );
    return response.data;
  }

  async downloadSubtitles(
    seriesid: number,
    episodeid: number,
    form: FormType.Subtitle,
    arrInstanceId?: number,
  ) {
    // arr_instance_id (#156) routes the search/download to the owning instance.
    await this.patch("/subtitles", form, {
      seriesid,
      episodeid,
      arr_instance_id: arrInstanceId,
    });
  }

  async uploadSubtitles(
    seriesid: number,
    episodeid: number,
    form: FormType.UploadSubtitle,
    arrInstanceId?: number,
  ) {
    // arr_instance_id (#156) scopes the action to the owning instance; the
    // backend treats it as optional (None = legacy/single-instance).
    await this.post("/subtitles", form, {
      seriesid,
      episodeid,
      arr_instance_id: arrInstanceId,
    });
  }

  async deleteSubtitles(
    seriesid: number,
    episodeid: number,
    form: FormType.DeleteSubtitle,
    arrInstanceId?: number,
  ) {
    await this.delete("/subtitles", form, {
      seriesid,
      episodeid,
      arr_instance_id: arrInstanceId,
    });
  }

  async blacklist() {
    const response =
      await this.get<DataWrapper<Blacklist.Episode[]>>("/blacklist");
    return response.data;
  }

  async addBlacklist(
    seriesid: number,
    episodeid: number,
    form: FormType.AddBlacklist,
  ) {
    await this.post("/blacklist", form, { seriesid, episodeid });
  }

  async deleteBlacklist(all?: boolean, form?: FormType.DeleteBlacklist) {
    await this.delete("/blacklist", form, { all });
  }
}

const episodeApi = new EpisodeApi();
export default episodeApi;
