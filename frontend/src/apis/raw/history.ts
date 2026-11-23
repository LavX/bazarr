import BaseApi from "./base";

class HistoryApi extends BaseApi {
  constructor() {
    super("/history");
  }

  async stats(
    timeFrame?: History.TimeFrameOptions,
    action?: History.ActionOptions,
    provider?: string,
    language?: Language.CodeType,
  ) {
    const response = await this.get<History.Stat>("/stats", {
      timeFrame,
      action,
      provider,
      language,
      sources: 1,
    });
    return response;
  }

  async metrics(
    timeFrame?: History.TimeFrameOptions,
    action?: History.ActionOptions,
    provider?: string,
    language?: Language.CodeType,
  ) {
    const response = await this.get<History.Metrics>("/metrics", {
      timeFrame,
      action,
      provider,
      language,
      sources: 1,
    });
    return response;
  }
}

const historyApi = new HistoryApi();
export default historyApi;
