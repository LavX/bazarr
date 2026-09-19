/* eslint-disable camelcase -- API parameters retain their transport names. */
import type {
  DigitalReleaseFeed,
  DiscoverCopyOffer,
  DiscoverDownloadIdentity,
  DiscoverPreviewData,
  DiscoverSearchProgress,
  DiscoverSearchSnapshot,
  DiscoverSelection,
  DiscoverSummary,
  MetadataResponse,
  RecentEpisodeFeed,
  TrendingFeed,
  TrendingMediaType,
} from "@/types/discover";
import BaseApi from "./base";
import client from "./client";

class DiscoverApi extends BaseApi {
  constructor() {
    super("/discover");
  }

  async search(
    context: DiscoverSelection,
    refresh = false,
    progressId?: string,
  ) {
    const response = await this.postRaw<DiscoverSearchSnapshot>(
      "/search",
      {
        ...context,
        refresh,
      },
      undefined,
      progressId ? { "X-Discover-Progress": progressId } : undefined,
    );
    return response.data;
  }

  async searchProgress(progressId: string, signal: AbortSignal) {
    const response = await client.axios.get<DiscoverSearchProgress>(
      this.prefix + "/search",
      {
        params: { progress_id: progressId },
        signal,
      },
    );
    return response.data;
  }

  async summary(signal: AbortSignal) {
    const response = await client.axios.get<DiscoverSummary>(
      this.prefix + "/summary",
      { signal },
    );
    return response.data;
  }

  async trending(mediaType: TrendingMediaType, signal: AbortSignal) {
    const response = await client.axios.get<TrendingFeed>(
      this.prefix + "/feeds/trending",
      { params: { media_type: mediaType }, signal },
    );
    return response.data;
  }

  async digitalReleases(region: string, signal: AbortSignal) {
    const response = await client.axios.get<DigitalReleaseFeed>(
      this.prefix + "/feeds/digital",
      { params: { region }, signal },
    );
    return response.data;
  }

  async copies(params: LooseObject, signal: AbortSignal) {
    const response = await client.axios.get<DiscoverCopyOffer>(
      this.prefix + "/copies",
      { params, signal },
    );
    return response.data;
  }

  async recentEpisodes(signal: AbortSignal) {
    const response = await client.axios.get<RecentEpisodeFeed>(
      this.prefix + "/feeds/recent-episodes",
      { signal },
    );
    return response.data;
  }

  async metadata(path: string, signal: AbortSignal, params?: LooseObject) {
    const response = await client.axios.get<{ data: MetadataResponse }>(
      this.prefix + "/metadata/" + path,
      { params, signal },
    );
    return response.data.data;
  }

  async testMetadata(token: string | undefined, signal: AbortSignal) {
    const response = await client.axios.post<{ data: MetadataResponse }>(
      this.prefix + "/metadata/test",
      token === undefined ? {} : { token },
      { signal },
    );
    return response.data.data;
  }

  async preview({ resultId, searchId }: DiscoverDownloadIdentity) {
    const response = await client.axios.get<DiscoverPreviewData>(
      this.prefix + "/preview",
      {
        params: { result_id: resultId, search_id: searchId },
      },
    );
    return response.data;
  }

  download({ resultId, searchId }: DiscoverDownloadIdentity) {
    return this.getBlob("/download", {
      result_id: resultId,
      search_id: searchId,
    });
  }
}

export default new DiscoverApi();
