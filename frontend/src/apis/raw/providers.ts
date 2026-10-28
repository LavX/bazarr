import type { QueuedSearch } from "@/utilities/jobs";
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

  /**
   * Queue the manual search for a movie as a job. The rows are the job's
   * result, read once it finishes.
   */
  async searchMovie(id: number): Promise<QueuedSearch> {
    const response = await this.post<QueuedSearch>(
      "/movies/search",
      undefined,
      {
        radarrid: id,
      },
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

  /** Queue the manual search for an episode as a job. */
  async searchEpisode(id: number): Promise<QueuedSearch> {
    const response = await this.post<QueuedSearch>(
      "/episodes/search",
      undefined,
      { episodeid: id },
    );
    return response.data;
  }

  async downloadEpisodeSubtitle(
    seriesid: number,
    episodeid: number,
    form: FormType.ManualDownload,
    arrInstanceId?: number,
  ) {
    const response = await this.post<{ job_id: number | null } | undefined>(
      "/episodes",
      form,
      {
        seriesid,
        episodeid,
        arr_instance_id: arrInstanceId,
      },
    );
    return response.data;
  }
}

const providerApi = new ProviderApi();
export default providerApi;
