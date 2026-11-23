/* eslint-disable camelcase */

import BaseApi from "./base";
import client from "./client";

export type SourceContentKind = "movie" | "series" | "sports";

export interface ServerRootMapping {
  server_path: string;
  local_path: string;
}

export interface ServerLibraryChoice {
  id: string;
  name: string;
  content_kind: SourceContentKind | null;
  roots: (Omit<ServerRootMapping, "local_path"> & {
    local_path: string | null;
  })[];
}

export interface ServerLibraryDraft {
  server_library_id: string;
  content_kind: SourceContentKind;
  profile_id: number | null;
  roots: ServerRootMapping[];
}

export interface SourceLibrary {
  id: number;
  locator: string;
  name?: string | null;
  roots?: ServerRootMapping[];
  content_kind: SourceContentKind;
  profile_id: number | null;
  enabled: boolean;
  last_scan_status: string | null;
  last_scan_message: string | null;
}

export interface MediaSource {
  id: number;
  kind: "folder" | ServerSourceKind;
  media_server_instance_id?: string | null;
  name: string;
  enabled: boolean;
  scan_frequency_hours: number;
  libraries: SourceLibrary[];
}

export type ServerSourceKind = "jellyfin" | "emby";

export function isServerSourceKind(kind: string): kind is ServerSourceKind {
  return kind === "jellyfin" || kind === "emby";
}

export type SourceAvailability = Record<
  SourceContentKind,
  {
    browsable: boolean;
    searchable: boolean;
    retained: boolean;
    removed?: boolean;
    retained_source_ids?: number[];
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

  serverLibraries(instanceId: string, signal?: AbortSignal) {
    return this.get<{ data: ServerLibraryChoice[] }>(
      "/server-libraries",
      { media_server_instance_id: instanceId },
      signal,
    );
  }

  createServerSource(
    kind: ServerSourceKind,
    instanceId: string,
    name: string,
    library: ServerLibraryDraft,
  ) {
    return this.postRaw<MediaSource>("", {
      kind,
      media_server_instance_id: instanceId,
      name,
      library,
    });
  }

  addServerLibrary(id: number, payload: ServerLibraryDraft) {
    return this.postRaw<SourceLibrary>(`/${id}/libraries`, payload);
  }

  async validateServerRoots(
    roots: ServerRootMapping[],
    contentKind: SourceContentKind,
    signal?: AbortSignal,
  ) {
    const { data } = await client.axios.post<Record<string, never>>(
      `${this.prefix}/server-roots/validate`,
      { roots, content_kind: contentKind },
      { signal },
    );
    return data;
  }

  async previewServerRoots(
    id: number,
    libraryId: number,
    roots: ServerRootMapping[],
    signal?: AbortSignal,
  ) {
    const { data } = await client.axios.post<{ removed_items: number }>(
      `${this.prefix}/${id}/libraries/${libraryId}/roots/preview`,
      { roots },
      { signal },
    );
    return data.removed_items;
  }

  updateServerRoots(
    id: number,
    libraryId: number,
    roots: ServerRootMapping[],
    confirmedRemoved: number,
  ) {
    return this.patchRaw<SourceLibrary>(`/${id}/libraries/${libraryId}`, {
      roots,
      confirmed_removed: confirmedRemoved,
    });
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
