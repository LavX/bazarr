import BaseApi from "./base";
import client from "./client";

export type SourceContentKind = "movie" | "series" | "sports";

export interface SourceLibrary {
  id: number;
  locator: string;
  content_kind: SourceContentKind;
  profile_id: number | null;
  enabled: boolean;
  last_scan_status: string | null;
  last_scan_message: string | null;
}

export interface MediaSource {
  id: number;
  kind: "folder" | "jellyfin" | "emby";
  name: string;
  enabled: boolean;
  scan_frequency_hours: number;
  libraries: SourceLibrary[];
}

export type SourceAvailability = Record<
  SourceContentKind,
  {
    browsable: boolean;
    searchable: boolean;
    retained: boolean;
    removed?: boolean;
  }
>;

interface MediaSourceList {
  data: MediaSource[];
  availability: SourceAvailability;
}

interface PathValidation {
  ok: boolean;
  errors: { code: string; message: string }[];
  warnings: string[];
}

export interface SourceLibraryDraft {
  path: string;
  content_kind: SourceContentKind;
  profile_id: number | null;
}

type SourceSettings = Pick<
  MediaSource,
  "name" | "enabled" | "scan_frequency_hours"
>;

type LibrarySettings = Pick<SourceLibrary, "profile_id" | "enabled">;

class MediaSourcesApi extends BaseApi {
  constructor() {
    super("/system/media-sources");
  }

  list(signal?: AbortSignal) {
    return this.get<MediaSourceList>("", { availability: 1 }, signal);
  }

  async validatePath(
    path: string,
    contentKind: SourceContentKind,
    signal?: AbortSignal,
  ) {
    const { data } = await client.axios.post<PathValidation>(
      `${this.prefix}/validate-path`,
      { path, content_kind: contentKind },
      { signal },
    );
    return data;
  }

  create(payload: SourceSettings & { library: SourceLibraryDraft }) {
    return this.postRaw<MediaSource>("", payload);
  }

  update(id: number, payload: SourceSettings) {
    return this.patchRaw<MediaSource>(`/${id}`, payload);
  }

  remove(id: number) {
    return this.delete(`/${id}`);
  }

  addLibrary(id: number, payload: SourceLibraryDraft) {
    return this.postRaw<SourceLibrary>(`/${id}/libraries`, payload);
  }

  updateLibrary(id: number, libraryId: number, payload: LibrarySettings) {
    return this.patchRaw<SourceLibrary>(
      `/${id}/libraries/${libraryId}`,
      payload,
    );
  }

  removeLibrary(id: number, libraryId: number) {
    return this.delete(`/${id}/libraries/${libraryId}`);
  }

  async scanLibrary(sourceId: number, libraryId: number) {
    const { data } = await this.postRaw<{ job_id?: number | null }>(
      `/${sourceId}/libraries/${libraryId}/scan`,
    );
    return data.job_id ?? null;
  }

  async scan(id: number) {
    const { data } = await this.postRaw<{ job_id?: number | null }>(
      `/${id}/scan`,
    );
    return data.job_id ?? null;
  }
}

export default new MediaSourcesApi();
