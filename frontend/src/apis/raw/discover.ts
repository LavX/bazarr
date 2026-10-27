/* eslint-disable camelcase -- API parameters retain their transport names. */
import type {
  DigitalReleaseFeed,
  DiscoverCopyOffer,
  DiscoverDownloadIdentity,
  DiscoverPreviewData,
  DiscoverSelection,
  DiscoverSummary,
  MetadataResponse,
  RecentEpisodeFeed,
  TrendingFeed,
  TrendingMediaType,
} from "@/types/discover";
import type { QueuedSearch } from "@/utilities/jobs";
import BaseApi from "./base";
import client from "./client";

class DiscoverApi extends BaseApi {
  constructor() {
    super("/discover");
  }

  /**
   * Queue a search as a job. The providers heard from, the rows built so far
   * and finally the whole snapshot are the job's result, read from
   * `system/jobs/result` while it runs and once it has finished.
   */
  async search(context: DiscoverSelection, refresh = false) {
    const response = await this.postRaw<QueuedSearch>("/search", {
      ...context,
      refresh,
    });
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

  /**
   * Queue the download of one exact result as a standard job. Answers at once
   * with the job id, which is also the ticket the finished file is saved by,
   * and the backend run that owns the job.
   */
  async download({ resultId, searchId }: DiscoverDownloadIdentity) {
    const response = await this.postRaw<{ job_id: number; session?: string }>(
      "/download",
      {
        result_id: resultId,
        search_id: searchId,
      },
    );
    return { jobId: response.data.job_id, session: response.data.session };
  }

  /** The file a finished download job kept, fetched by its ticket. */
  downloadTicket(ticket: number) {
    return this.getBlob("/download", { job: ticket });
  }
}

export default new DiscoverApi();
