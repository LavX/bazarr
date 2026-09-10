/* eslint-disable camelcase */

import BaseApi from "./base";

export type MediaServerKind = "emby" | "silo";
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
};
export type MediaServerUpdate = Partial<
  Pick<
    MediaServerInstance,
    "name" | "enabled" | "url" | "verify_ssl" | "path_mappings"
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
export type SiloLibrary = {
  id: string;
  name: string;
  type: "movies" | "series";
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
    !["emby", "silo"].includes(row.kind) ||
    typeof row.name !== "string" ||
    typeof row.enabled !== "boolean" ||
    typeof row.url !== "string" ||
    typeof row.verify_ssl !== "boolean" ||
    typeof row.api_key_set !== "boolean" ||
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

  private probe(path: string, input: ConnectionInput | ConnectionOverrides) {
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

  testConnection(kind: MediaServerKind, input: ConnectionInput) {
    return this.probe(`/${kind}/test-connection`, input);
  }

  testExisting(id: string, input: ConnectionOverrides = {}) {
    return this.probe(`${itemPath(id)}/test-connection`, input);
  }

  private loadLibraries(
    path: string,
    input: ConnectionInput | ConnectionOverrides,
  ) {
    return safeRequest(async () => {
      const response = await this.postRaw<{
        data: SiloLibrary[];
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
            !["movies", "series"].includes(library.type) ||
            !Array.isArray(library.paths) ||
            library.paths.some((path) => typeof path !== "string"),
        )
      )
        throw new Error();
      return data.map((library) => ({
        id: library.id,
        name: library.name,
        type: library.type,
        paths: library.paths,
      }));
    }, "Could not load Silo libraries");
  }

  libraries(input: ConnectionInput) {
    return this.loadLibraries("/silo/libraries", input);
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
