import BaseApi from "./base";

class BadgesApi extends BaseApi {
  constructor() {
    super("/badges");
  }

  async all(includeSources = false) {
    const response = await this.get<Badge>("", {
      sources: includeSources ? 1 : undefined,
    });
    return response;
  }
}

const badgesApi = new BadgesApi();

export default badgesApi;
