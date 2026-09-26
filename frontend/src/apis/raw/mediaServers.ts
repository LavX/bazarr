/* eslint-disable camelcase */

import BaseApi from "./base";

export type MediaServerKind = "emby" | "jellyfin" | "plex" | "silo";
export const MEDIA_SERVER_KINDS: MediaServerKind[] = [
  "emby",
  "jellyfin",
  "plex",
  "silo",
];
// Emby and Silo resolve a publication by its path, so they need mappings.
// Jellyfin and Plex resolve the item themselves and never have.
export const KINDS_WITH_PATH_MAPPINGS: MediaServerKind[] = ["emby", "silo"];
export const KINDS_WITH_LIBRARIES: MediaServerKind[] = ["jellyfin", "plex"];
// What each kind keeps in its own options blob, by the media type it scopes.
export const LIBRARY_OPTION_KEYS: Record<
  "jellyfin" | "plex",
  { movie: string; series: string; sports: string }
> = {
  jellyfin: {
    movie: "movie_library_ids",
    series: "series_library_ids",
    sports: "sports_library_ids",
  },
  plex: {
    movie: "movie_libraries",
    series: "series_libraries",
    sports: "sports_libraries",
  },
};
export type MediaServerOptions = {
  movie_library_ids?: string[];
  series_library_ids?: string[];
  sports_library_ids?: string[];
  refresh_method?: "immediate" | "async";
  movie_libraries?: string[];
  series_libraries?: string[];
  sports_libraries?: string[];
};
const LIBRARY_LIST_KEYS = [
  ...Object.values(LIBRARY_OPTION_KEYS.jellyfin),
  ...Object.values(LIBRARY_OPTION_KEYS.plex),
];
export type PathMapping = {
  local_path: string;
  remote_path: string;
  library_id?: string;
};
export type MediaServerInstance = {
  id: string;
  kind: MediaServerKind;
  name: string;
  enabled: boolean;
  url: string;
  verify_ssl: boolean;
  api_key_set: boolean;
  path_mappings: PathMapping[];
  refresh_movies: boolean;
  refresh_episodes: boolean;
  options: MediaServerOptions;
  /** The signed-in Plex account's own row, removed by disconnecting from Plex. */
  account_owned?: boolean;
};
export type MediaServerUpdate = Partial<
  Pick<
    MediaServerInstance,
    | "name"
    | "enabled"
    | "url"
    | "verify_ssl"
    | "path_mappings"
    | "refresh_movies"
    | "refresh_episodes"
    | "options"
  >
> & { api_key?: string; clear_api_key?: boolean };
export type MediaServerCreate = MediaServerUpdate & {
  kind: MediaServerKind;
  name: string;
  url: string;
};
export type ConnectionInput = {
  url: string;
  apikey: string;
  verify_ssl: boolean;
};
export type ConnectionOverrides = {
  url?: string;
  api_key?: string;
  clear_api_key?: boolean;
  verify_ssl?: boolean;
};
export type MediaServerLibrary = {
  id: string;
  name: string;
  type: string;
  // Silo is the only kind that reports the roots a library holds, and the only
  // one that needs them: its path mappings are checked against them.
  paths: string[];
};
export type RefreshStatus = {
  pending: number;
  state: "idle" | "pending" | "requested" | "confirmed" | "unconfirmed";
  error_code: string | null;
};
export type ConnectionTestResult = {
  success: boolean;
  server_name?: string;
  server_id?: string;
  version?: string;
  error_code?: string;
};

const instancesPath = "/system/media-server-instances";
const itemPath = (id: string) => `${instancesPath}/${encodeURIComponent(id)}`;

// Transport errors can retain request bodies, including write-only credentials.
// Never expose them to query/mutation state or callers.
async function safeRequest<T>(request: () => Promise<T>, message: string) {
  try {
    return await request();
  } catch {
    throw new Error(message);
  }
}

function safeInstance(row: MediaServerInstance): MediaServerInstance {
  if (
    !row ||
    typeof row.id !== "string" ||
    !MEDIA_SERVER_KINDS.includes(row.kind) ||
    typeof row.name !== "string" ||
    typeof row.enabled !== "boolean" ||
    typeof row.url !== "string" ||
    typeof row.verify_ssl !== "boolean" ||
    typeof row.api_key_set !== "boolean" ||
    typeof row.refresh_movies !== "boolean" ||
    typeof row.refresh_episodes !== "boolean" ||
    !row.options ||
    typeof row.options !== "object" ||
    Array.isArray(row.options) ||
    // A library handle reaches a picker and comes back on the next save, so a
    // non-string here would be written back as one.
    LIBRARY_LIST_KEYS.some((key) => {
      const value = (row.options as Record<string, unknown>)[key];
      return (
        value !== undefined &&
        (!Array.isArray(value) ||
          value.some((handle) => typeof handle !== "string"))
      );
    }) ||
    !Array.isArray(row.path_mappings) ||
    row.path_mappings.some(
      (mapping) =>
        !mapping ||
        typeof mapping.local_path !== "string" ||
        typeof mapping.remote_path !== "string" ||
        !(
          mapping.library_id === undefined ||
          typeof mapping.library_id === "string"
        ),
    )
  ) {
    throw new Error("Invalid instance response");
  }
  return {
    id: row.id,
    kind: row.kind,
    name: row.name,
    enabled: row.enabled,
    url: row.url,
    verify_ssl: row.verify_ssl,
    api_key_set: row.api_key_set,
    path_mappings: row.path_mappings.map((mapping) => ({
      local_path: mapping.local_path,
      remote_path: mapping.remote_path,
      ...(mapping.library_id === undefined
        ? {}
        : { library_id: mapping.library_id }),
    })),
    refresh_movies: row.refresh_movies,
    refresh_episodes: row.refresh_episodes,
    options: row.options,
    ...(row.account_owned === true ? { account_owned: true } : {}),
  };
}

class MediaServersApi extends BaseApi {
  constructor() {
    super("");
  }

  list(kind: MediaServerKind) {
    return safeRequest(async () => {
      const response = await this.get<{ data: MediaServerInstance[] }>(
        instancesPath,
        { kind },
      );
      return response.data.map(safeInstance);
    }, "Could not load media server instances");
  }

  getOne(id: string) {
    return safeRequest(
      async () =>
        safeInstance(await this.get<MediaServerInstance>(itemPath(id))),
      "Could not load media server instance",
    );
  }

  create(input: MediaServerCreate) {
    return safeRequest(
      async () =>
        safeInstance(
          (await this.postRaw<MediaServerInstance>(instancesPath, input)).data,
        ),
      "Could not save media server instance",
    );
  }

  update(id: string, input: MediaServerUpdate) {
    return safeRequest(
      async () =>
        safeInstance(
          (await this.patchRaw<MediaServerInstance>(itemPath(id), input)).data,
        ),
      "Could not save media server instance",
    );
  }

  remove(id: string) {
    return safeRequest(async () => {
      await this.delete(itemPath(id));
    }, "Could not delete media server instance");
  }

  private probe(
    path: string,
    input: (ConnectionInput | ConnectionOverrides) & { kind?: MediaServerKind },
  ) {
    return safeRequest(async (): Promise<ConnectionTestResult> => {
      const { data } = await this.postRaw<ConnectionTestResult>(path, input);
      if (!data || typeof data.success !== "boolean") throw new Error();
      // Failure detail is intentionally generic, including in mutation state.
      return data.success
        ? {
            success: true,
            ...(typeof data.server_name === "string"
              ? { server_name: data.server_name }
              : {}),
            ...(typeof data.version === "string"
              ? { version: data.version }
              : {}),
          }
        : { success: false };
    }, "Connection test failed");
  }

  // One route for every kind: which server is being added is a value in the
  // request, not a different endpoint. The per-kind routes that predate the
  // destination layer still answer for API compatibility.
  testConnection(kind: MediaServerKind, input: ConnectionInput) {
    return this.probe(`${instancesPath}/probe`, { ...input, kind });
  }

  testExisting(id: string, input: ConnectionOverrides = {}) {
    return this.probe(`${itemPath(id)}/test-connection`, input);
  }

  private loadLibraries(
    path: string,
    input: (ConnectionInput | ConnectionOverrides) & { kind?: MediaServerKind },
  ) {
    return safeRequest(async () => {
      const response = await this.postRaw<{
        data: MediaServerLibrary[];
        error_code: string | null;
      }>(path, input);
      const { data, error_code: errorCode } = response.data;
      if (
        errorCode ||
        !Array.isArray(data) ||
        data.some(
          (library) =>
            !library ||
            typeof library.id !== "string" ||
            !library.id ||
            typeof library.name !== "string" ||
            typeof library.type !== "string" ||
            // Only Silo reports roots, so their absence is not a bad response.
            (library.paths !== undefined &&
              (!Array.isArray(library.paths) ||
                library.paths.some((path) => typeof path !== "string"))),
        )
      )
        throw new Error();
      return data.map((library) => ({
        id: library.id,
        name: library.name,
        type: library.type,
        paths: library.paths ?? [],
      }));
    }, "Could not load media server libraries");
  }

  libraries(kind: MediaServerKind, input: ConnectionInput) {
    return this.loadLibraries(`${instancesPath}/probe-libraries`, {
      ...input,
      kind,
    });
  }

  librariesExisting(id: string, input: ConnectionOverrides = {}) {
    return this.loadLibraries(`${itemPath(id)}/libraries`, input);
  }

  status(id: string): Promise<RefreshStatus> {
    return safeRequest(async () => {
      const data = await this.get<RefreshStatus>(`${itemPath(id)}/status`);
      if (
        !data ||
        !Number.isInteger(data.pending) ||
        data.pending < 0 ||
        !["idle", "pending", "requested", "confirmed", "unconfirmed"].includes(
          data.state,
        ) ||
        !(data.error_code === null || typeof data.error_code === "string")
      )
        throw new Error();
      return {
        pending: data.pending,
        state: data.state,
        error_code: data.error_code,
      };
    }, "Refresh status unavailable");
  }

  refreshLibraries(id: string) {
    return safeRequest(async () => {
      const { data } = await this.postRaw<{
        requested: number;
        failed?: number;
        error_code?: string;
      }>(`${itemPath(id)}/refresh-libraries`, {});
      if (!data || !Number.isInteger(data.requested)) throw new Error();
      return {
        requested: data.requested,
        failed: Number.isInteger(data.failed) ? (data.failed as number) : 0,
      };
    }, "Could not refresh libraries");
  }

  retryPending(id: string) {
    return safeRequest(async () => {
      const { data } = await this.postRaw<{ queued: number }>(
        `${itemPath(id)}/retry-pending`,
        {},
      );
      if (!data || !Number.isInteger(data.queued) || data.queued < 0)
        throw new Error();
      return { queued: data.queued };
    }, "Could not queue pending refreshes");
  }
}

export default new MediaServersApi();
