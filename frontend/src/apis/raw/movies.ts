import BaseApi from "./base";

class MovieApi extends BaseApi {
  constructor() {
    super("/movies");
  }

  async blacklist() {
    const response =
      await this.get<DataWrapper<Blacklist.Movie[]>>("/blacklist");
    return response.data;
  }

  async addBlacklist(radarrid: number, form: FormType.AddBlacklist) {
    await this.post("/blacklist", form, { radarrid });
  }

  async deleteBlacklist(all?: boolean, form?: FormType.DeleteBlacklist) {
    await this.delete("/blacklist", form, { all });
  }

  async movies(ids?: number[]) {
    // Fetch by the canonical local id (#156); the backend dual-accepts id[] and
    // the legacy radarrid[]. id == radarrId on a single default instance.
    const response = await this.get<DataWrapperWithTotal<Item.Movie>>("", {
      id: ids,
    });
    return response.data;
  }

  async moviesBy(params: Parameter.Range) {
    const response = await this.get<DataWrapperWithTotal<Item.Movie>>(
      "",
      params,
    );
    return response;
  }

  async modify(form: FormType.ModifyItem) {
    await this.post("", { radarrid: form.id, profileid: form.profileid });
  }

  async wanted(params: Parameter.Range) {
    const response = await this.get<DataWrapperWithTotal<Wanted.Movie>>(
      "/wanted",
      params,
    );
    return response;
  }

  async wantedBy(radarrid: number[]) {
    const response = await this.get<DataWrapperWithTotal<Wanted.Movie>>(
      "/wanted",
      {
        radarrid,
      },
    );
    return response;
  }

  async history(params: Parameter.Range) {
    const response = await this.get<DataWrapperWithTotal<History.Movie>>(
      "/history",
      params,
    );
    return response;
  }

  async historyBy(radarrid: number) {
    const response = await this.get<DataWrapperWithTotal<History.Movie>>(
      "/history",
      { radarrid },
    );
    return response.data;
  }

  async action(action: FormType.MoviesAction) {
    await this.patch("", action);
  }

  async downloadSubtitles(radarrid: number, form: FormType.Subtitle) {
    await this.patch("/subtitles", form, { radarrid });
  }

  async uploadSubtitles(
    radarrid: number,
    form: FormType.UploadSubtitle,
    arrInstanceId?: number,
  ) {
    // arr_instance_id (#156) scopes the action to the owning instance; the
    // backend treats it as optional (None = legacy/single-instance).
    await this.post("/subtitles", form, {
      radarrid,
      arr_instance_id: arrInstanceId,
    });
  }

  async deleteSubtitles(
    radarrid: number,
    form: FormType.DeleteSubtitle,
    arrInstanceId?: number,
  ) {
    await this.delete("/subtitles", form, {
      radarrid,
      arr_instance_id: arrInstanceId,
    });
  }
}

const movieApi = new MovieApi();
export default movieApi;
