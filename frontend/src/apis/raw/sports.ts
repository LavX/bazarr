/* eslint-disable camelcase */
import BaseApi from "./base";
import client from "./client";

export interface SportsLeague {
  id: number;
  arr_instance_id: number;
  sportarrLeagueId: number;
  externalId: string | null;
  title: string;
  sortTitle: string | null;
  overview: string | null;
  path: string | null;
  poster: string | null;
  fanart: string | null;
  sport: string | null;
  monitored: boolean;
  tags: string[];
  audio_language: string[];
  profileId: number | null;
  eventCount: number;
  eventFileCount: number;
}

export interface SportsEventReference {
  id: number;
  arr_instance_id: number;
  league_id: number;
  sportarrEventId: number;
  file_id: number;
  partNumber: number | null;
  partName: string | null;
  season: number | null;
  episode: number | null;
  path: string;
}

export interface SportsEvent extends SportsEventReference {
  sceneName?: string;
  title: string;
  eventDate: string | null;
  broadcastDate: string | null;
  hasFile: boolean;
  mapped_path: string;
  file_size: number;
  profileId: number | null;
  subtitles: [language: string, path: string | null, size: number | null][];
  missing_subtitles: string[];
}

export interface SportsPublication {
  published: boolean;
  status: "published" | "published_with_warnings";
  processing: "not_started" | "completed" | "failed" | "cancelled";
  artifact: "not_started" | "completed" | "failed" | "cancelled";
  history: "not_started" | "committed" | "failed" | "uncertain";
  index:
    | "not_attempted"
    | "completed"
    | "failed"
    | "cancelled"
    | "owner_changed";
  refresh_attempts: number;
  refresh_queued: false;
  failed_phase: string | null;
  cancelled: boolean;
  message: string;
}
export interface SportsDownloadResult {
  event: SportsEvent | null;
  publication: SportsPublication;
}

export interface SportsRecord {
  id: number;
  arr_instance_id: number;
  league_id: number;
  event_id: number;
  title: string;
  /** Relative form for the column, as the episodes and movies endpoints send. */
  timestamp: string | null;
  /** Exact date, shown in the column's popover. */
  parsed_timestamp?: string | null;
  language: string | null;
  provider: string | null;
  subs_id: string | null;
  action?: number;
  description?: string;
  score?: number | null;
  score_out_of?: number | null;
  subtitles_path?: string | null;
}
export interface SportsJob {
  queued: boolean;
  job_id: number | null;
  message: string;
}
export interface SportsJobStatus {
  job_id: number;
  arr_instance_id: number;
  status: "pending" | "running" | "completed" | "failed";
  cancelled: boolean;
  message: string;
  result: {
    message?: string;
    file_status?: "deleted" | "absent" | "preserved";
    replacement?: { message?: string };
    data?: {
      status?: string;
      message?: string;
      downloads?: number;
      publication?: SportsPublication;
      cancelled?: boolean;
    }[];
  } | null;
}
export interface SportsFilters {
  owner?: number;
  eventId?: number;
  language?: string;
  provider?: string;
  action?: string;
  page?: number;
}

class SportsApi extends BaseApi {
  constructor() {
    super("/sports");
  }
  jobStatus(id: number, owner: number) {
    return this.get<SportsJobStatus>(`/jobs/${id}`, { arr_instance_id: owner });
  }
  // start/length rather than a page number, which is the contract every other
  // paginated endpoint speaks and what usePaginationQuery sends. length -1 asks
  // for the whole list, which the shared views use for library-wide filtering.
  activity(
    kind: "wanted" | "history" | "blacklist",
    filters: SportsFilters,
    start = 0,
    length = 100,
  ) {
    return this.get<{ data: (SportsEvent | SportsRecord)[]; total: number }>(
      `/${kind}`,
      {
        arr_instance_id: filters.owner,
        event_id: filters.eventId,
        language: filters.language || undefined,
        provider: filters.provider || undefined,
        action: filters.action || undefined,
        start,
        length,
      },
    );
  }
  async runAction(path: string, owner: number) {
    return (await this.postRaw<SportsJob>(path, { arr_instance_id: owner }))
      .data;
  }
  async removeExclusion(owner: number, id?: number) {
    return (
      await this.delete<{ removed: boolean | number }>(
        id ? `/blacklist/${id}` : "/blacklist",
        undefined,
        { arr_instance_id: owner },
      )
    ).data;
  }
  list(owner?: number, start = 0, length = 100) {
    return this.get<{ data: SportsLeague[]; total: number }>("/leagues", {
      arr_instance_id: owner,
      start,
      length,
    });
  }
  getOne(id: number, owner?: number) {
    return this.get<SportsLeague>(`/leagues/${id}`, { arr_instance_id: owner });
  }
  events(id: number, owner: number, start = 0, length = 100) {
    return this.get<{ data: SportsEvent[]; total: number }>(
      `/leagues/${id}/events`,
      {
        arr_instance_id: owner,
        start,
        length,
      },
    );
  }
  assignProfile(id: number, owner: number, profileId: number | null) {
    return this.patchRaw(`/leagues/${id}`, {
      arr_instance_id: owner,
      profileId,
    });
  }
  sync(owner: number) {
    return this.postRaw("/leagues/sync", { arr_instance_id: owner });
  }
  async searchSubtitles(
    event: SportsEventReference,
    language: string,
    hi: boolean,
    forced: boolean,
  ) {
    const response = await this.postRaw<{ data: SearchResultType[] }>(
      `/events/${event.id}/search`,
      {
        arr_instance_id: event.arr_instance_id,
        language,
        hi,
        forced,
      },
    );
    return response.data.data;
  }
  async downloadSubtitle(
    event: SportsEventReference,
    candidate: SearchResultType,
  ) {
    const response = await this.postRaw<SportsDownloadResult>(
      `/events/${event.id}/download`,
      {
        arr_instance_id: event.arr_instance_id,
        candidate,
      },
    );
    return response.data;
  }
  // Removing a subtitle Bazarr placed on an event. DELETE on the indexer's own
  // path, which owns it: a second Resource registered there would shadow the
  // index POST.
  async removeSubtitle(
    id: number,
    owner: number,
    form: { language: string; path: string; hi?: boolean; forced?: boolean },
  ) {
    // client.axios directly rather than the BaseApi helper: that one encodes
    // its body as FormData, and this route reads a JSON object.
    await client.axios.delete(`/sports/events/${id}/subtitles`, {
      data: { arr_instance_id: owner, ...form },
    });
  }

  indexSubtitles(id: number, owner: number) {
    return this.postRaw<SportsEvent>(`/events/${id}/subtitles`, {
      arr_instance_id: owner,
    });
  }
}
export default new SportsApi();
