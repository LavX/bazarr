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
}

const seriesApi = new SeriesApi();
export default seriesApi;
