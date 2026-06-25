import BaseApi from "./base";

class ProviderApi extends BaseApi {
  constructor() {
    super("/providers");
  }

  async providers(history = false) {
    const response = await this.get<DataWrapper<System.Provider[]>>("", {
      history,
    });
    return response.data;
  }

  async reset() {
    await this.post("", { action: "reset" });
  }

  async movies(id: number) {
    const response = await this.get<DataWrapper<SearchResultType[]>>(
      "/movies",
      { radarrid: id },
    );
    return response.data;
  }

  async downloadMovieSubtitle(
    radarrid: number,
    form: FormType.ManualDownload,
    arrInstanceId?: number,
  ) {
    // arr_instance_id (#156) scopes the download to the owning instance; the
    // backend treats it as optional (None = legacy/single-instance).
    await this.post("/movies", form, {
      radarrid,
      arr_instance_id: arrInstanceId,
    });
  }

  async episodes(episodeid: number) {
    const response = await this.get<DataWrapper<SearchResultType[]>>(
      "/episodes",
      {
        episodeid,
      },
    );
    return response.data;
  }

  async downloadEpisodeSubtitle(
    seriesid: number,
    episodeid: number,
    form: FormType.ManualDownload,
    arrInstanceId?: number,
  ) {
    await this.post("/episodes", form, {
      seriesid,
      episodeid,
      arr_instance_id: arrInstanceId,
    });
  }
}

const providerApi = new ProviderApi();
export default providerApi;
