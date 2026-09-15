/* eslint-disable camelcase -- transport names. */
import type {
  SeerrMediaResponse,
  SeerrRequestBody,
  SeerrRequestOutcome,
  SeerrTestResult,
} from "@/types/seerr";
import BaseApi from "./base";
import client from "./client";

class SeerrApi extends BaseApi {
  constructor() {
    super("/seerr");
  }

  async testConnection(url: string, apikey: string, verifySsl?: boolean) {
    const body: Record<string, string> = { url, apikey };
    if (verifySsl !== undefined) {
      body.verify_ssl = verifySsl ? "true" : "false";
    }
    const response = await this.post<SeerrTestResult>("/test-connection", body);
    return response.data;
  }

  // Bypass BaseApi.get: it has no signal parameter, and the title page's
  // media lookup needs to cancel in flight when the user navigates away.
  async media(mediaType: "movie" | "tv", tmdbId: number, signal?: AbortSignal) {
    const response = await client.axios.get<SeerrMediaResponse>(
      this.prefix + `/media/${mediaType}/${tmdbId}`,
      { signal },
    );
    return response.data;
  }

  async mediaByTvdb(tvdbId: number, signal?: AbortSignal) {
    const response = await client.axios.get<SeerrMediaResponse>(
      this.prefix + `/media/tv/by-tvdb/${tvdbId}`,
      { signal },
    );
    return response.data;
  }

  // postRaw, not post: the backend reads this body with request.get_json(),
  // not reqparse, so it must go as JSON rather than through BaseApi.post's
  // form encoding. No signal here, unlike the reads above: a request that
  // reached Seerr has already dispatched a download, so a navigation must not
  // cancel it.
  async request(body: SeerrRequestBody) {
    const response = await this.postRaw<SeerrRequestOutcome>("/request", body);
    return response.data;
  }
}

const seerr = new SeerrApi();
export default seerr;
