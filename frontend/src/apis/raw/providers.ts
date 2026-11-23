import type { QueuedSearch } from "@/utilities/jobs";
import BaseApi from "./base";

class ProviderApi extends BaseApi {
  constructor() {
    super("/providers");
  }

  async providers(history = false, includeSources = false) {
    const response = await this.get<DataWrapper<System.Provider[]>>("", {
      history,
      sources: includeSources ? 1 : undefined,
    });
    return response.data;
  }

  /** The effective FlareSolverr address, and a sibling container if one answered. */
  async flaresolverr() {
    const response =
      await this.get<DataWrapper<System.FlareSolverrStatus>>("/flaresolverr");
    return response.data;
  }

  /** Look for the sibling again. Reports only: it never saves anything. */
  async detectFlaresolverr() {
    const response =
      await this.post<DataWrapper<System.FlareSolverrStatus>>("/flaresolverr");
    return response.data.data;
  }

  /** Test an address as typed, saved or not. Reports only: it never saves anything. */
  async testFlaresolverr(url: string) {
    const response = await this.post<
      DataWrapper<System.FlareSolverrTestResult>
    >("/flaresolverr/test", { url });
    return response.data.data;
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
