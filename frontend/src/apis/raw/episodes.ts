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

  async history(params: Parameter.Range & { include_embedded?: boolean }) {
    const response = await this.get<DataWrapperWithTotal<History.Episode>>(
      "/history",
      params,
    );
    return response;
  }

  async historyBy(id: number) {
    const response = await this.get<DataWrapperWithTotal<History.Episode>>(
      "/history",
      // Detail views need the Embedded Source rows the paginated history
      // hides by default: the movie table reads their score and provider.
      { id, include_embedded: true, length: -1 },
    );
    return response.data;
  }

  async historyBySeriesId(seriesId: number, signal?: AbortSignal) {
    const response = await this.get<DataWrapperWithTotal<History.Episode>>(
      "/history",
      // The series detail table reads score + provider for every episode's
      // subtitles in one request, including the Embedded Source rows the
      // paginated history hides by default. It never reads upgradable, so
      // the full-library upgrade scan is skipped for this call.
      {
        series_id: seriesId,
        include_embedded: true,
        length: -1,
        include_upgradable: false,
      },
      signal,
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
    const response = await this.patch<{ job_id: number | null } | undefined>(
      "/subtitles",
      form,
      {
        seriesid,
        episodeid,
        arr_instance_id: arrInstanceId,
      },
    );
    return response.data;
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
    const response = await this.post<{ job_id: number | null } | undefined>(
      "/blacklist",
      form,
      { seriesid, episodeid },
    );
    return response.data;
  }

  async deleteBlacklist(all?: boolean, form?: FormType.DeleteBlacklist) {
    await this.delete("/blacklist", form, { all });
  }
}

const episodeApi = new EpisodeApi();
export default episodeApi;
