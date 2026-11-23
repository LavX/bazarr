import BaseApi from "./base";
import type {
  ProviderHubCatalogEntry,
  ProviderHubCatalogSource,
  ProviderHubInstallation,
  ProviderHubJob,
  ProviderHubJobRef,
} from "./providerHub";

/** The kinds Bazarr+ defines. The server answers for the ones this build has. */
export const SUBTITLE_PROVIDER_KIND = "subtitle_provider";

/** What a person is told about a permission: whether this host's sandbox holds the plugin to it. */
export type PluginPermissionMark = "enforced" | "declared";

export interface PluginPermission {
  name: string;
  mark: PluginPermissionMark;
}

export interface PluginBinary {
  artifact: string;
  process: string;
}

/** Where a plugin comes from, as Bazarr+'s own record of the source says, never the manifest's. */
export interface PluginSourceInfo {
  id: string | null;
  name: string | null;
  official: boolean;
  trusted: boolean;
  development?: boolean;
  signature?: unknown;
}

export interface PluginConsentMember {
  id: string;
  name: string;
  version: string;
  kind: string;
  /** Why it is in the set: "root" for the plugin asked for, else what pulled it in. */
  reason: string;
  source: PluginSourceInfo;
  permissions: PluginPermission[];
  destinations: string[];
  binaries: PluginBinary[];
  /** Bytes, or null while the manifest carries no size. */
  download_size: number | null;
  /** Bytes, or null when the plugin asks for no more than its kind's default. */
  memory_request: number | null;
}

/** What installing a plugin and everything it needs would add, and the digest to send back with the install. */
export interface PluginConsentSummary {
  plugin: { id: string; name: string; version: string; kind: string };
  members: PluginConsentMember[];
  already_installed: Array<Record<string, string>>;
  external_requirements: string[];
  permissions: PluginPermission[];
  destinations: string[];
  binaries: PluginBinary[];
  download_size: number | null;
  memory_request: number | null;
  /** True when this explicit install would replace a provider and add access. */
  needs_consent?: boolean;
  digest: string;
}

export interface PluginAddedMember {
  id: string;
  name: string;
  version: string;
  kind: string;
  permissions: PluginPermission[];
  destinations: string[];
  binaries: PluginBinary[];
}

/** What an update adds to what is installed, whether that needs the person's consent, and its digest. */
export interface PluginUpdateDelta {
  plugin: {
    id: string;
    name: string;
    from_version: string | null;
    to_version: string;
    kind: string;
  };
  needs_consent: boolean;
  added: {
    destinations: string[];
    destination_fields: string[];
    permissions: PluginPermission[];
    binaries: PluginBinary[];
    plugins: PluginAddedMember[];
  };
  digest: string;
}

export interface PluginCatalogEntry extends ProviderHubCatalogEntry {
  /** Which list of the source holds the entry: "plugins" for a kind of its own. */
  catalog_list?: string;
  development_source?: boolean;
}

/** A catalog source as Bazarr+ records it, with what the signature check of the official one came to. */
export interface PluginCatalogSource extends ProviderHubCatalogSource {
  /** "verified" for a signed catalog, "unsigned (legacy)" for the old one. */
  catalog_signature?: string | null;
  signature_verified?: boolean;
  catalog_channel?: string | null;
  /** When the signed catalog stops being installable from, as an ISO time. */
  catalog_expires_at?: string | null;
  development_source?: boolean;
}

export type PluginCatalog = {
  sources: PluginCatalogSource[];
  entries: PluginCatalogEntry[];
};

/**
 * How a plugin's worker is doing: starting, healthy (running a request), idle (up and waiting),
 * backing_off (the last start or run failed and requests wait out a delay), broken, stopping or stopped.
 */
export type PluginRuntimePhase =
  | "starting"
  | "healthy"
  | "idle"
  | "backing_off"
  | "broken"
  | "stopping"
  | "stopped";

export interface PluginRuntime {
  phase: PluginRuntimePhase;
  in_flight: number;
  /** Seconds until a request can be tried again, while backing off. */
  retry_after: number | null;
  last_error: string | null;
  starts: number;
  crashes: number;
  memory_bytes: number;
}

export interface PluginInstallation extends ProviderHubInstallation {
  /** Absent on a row written before kinds existed, which is a subtitle provider. */
  kind?: string;
  enabled?: boolean;
  development_source?: boolean;
  /** Absent while the plugin has no worker, or nothing is bound to run one. */
  runtime?: PluginRuntime;
  /** Ids of the installed plugins that depend on this one, by the server's own check: what a removal refuses over. */
  dependents?: string[];
  /** The same, counting only the plugins that are switched on: what switching it off refuses over. */
  enabled_dependents?: string[];
  /** True while a plugin installed for the person has a review notice they have not dismissed. */
  review_pending?: boolean;
  /** The previous release and the server's reason when it cannot be restored. */
  rollback?: PluginRollbackAvailability;
}

export interface PluginRollbackAvailability {
  previous_version: string | null;
  blocked_reason: string | null;
}

export interface PluginRollbackPreview extends PluginRollbackAvailability {
  consent: PluginUpdateDelta | null;
}

export interface PluginRollbackRequest {
  version: string;
  consent?: string | null;
}

export interface PluginRollbackResult {
  status: "activated" | "waiting";
  version: string;
}

/** A plugin's recent output, secrets already scrubbed. */
export interface PluginLog {
  lines: string[];
  last_error: string | null;
}

export type PluginJob = ProviderHubJob;

/** An install names a catalog entry, and carries the digest of the summary the person agreed to. */
export interface PluginInstallRequest {
  source: string;
  id: string;
  version: string;
  consent?: string;
}

export interface PluginUpdateApplyRequest {
  id: string;
  consent?: string;
}

class PluginsApi extends BaseApi {
  constructor() {
    super("/plugins");
  }

  catalog(kind?: string) {
    return this.get<PluginCatalog>("/catalog", kind ? { kind } : undefined);
  }

  async refreshCatalog() {
    const response = await this.postRaw<ProviderHubJobRef>("/catalog/refresh");
    return response.data;
  }

  async installations(kind?: string) {
    const response = await this.get<{ data: PluginInstallation[] }>(
      "/installations",
      kind ? { kind } : undefined,
    );
    return response.data;
  }

  installation(id: string) {
    return this.get<PluginInstallation>(`/installations/${id}`);
  }

  installConsent(source: string, id: string, version: string) {
    return this.get<PluginConsentSummary>("/catalog/consent", {
      source,
      id,
      version,
    });
  }

  updateConsent(id: string) {
    return this.get<PluginUpdateDelta>("/updates/consent", { id });
  }

  rollbackPreview(id: string) {
    return this.get<PluginRollbackPreview>(`/installations/${id}/rollback`);
  }

  async rollback(id: string, request: PluginRollbackRequest) {
    const response = await this.postRaw<PluginRollbackResult>(
      `/installations/${id}/rollback`,
      request,
    );
    return response.data;
  }

  async install(request: PluginInstallRequest) {
    const response = await this.postRaw<ProviderHubJobRef>(
      "/installations",
      request,
    );
    return response.data;
  }

  async applyUpdate(request: PluginUpdateApplyRequest) {
    const response = await this.postRaw<ProviderHubJobRef>(
      "/updates/apply",
      request,
    );
    return response.data;
  }

  async checkUpdates() {
    const response = await this.postRaw<LooseObject>("/updates/check");
    return response.data;
  }

  async jobs() {
    const response = await this.get<{ data: PluginJob[] }>("/jobs");
    return response.data;
  }

  async setEnabled(id: string, enabled: boolean) {
    const response = await this.patchRaw<PluginInstallation>(
      `/installations/${id}`,
      { enabled },
    );
    return response.data;
  }

  async repair(id: string) {
    const response = await this.postRaw<ProviderHubJobRef>(
      `/installations/${id}/repair`,
    );
    return response.data;
  }

  /** `keepData` keeps the plugin's saved settings and data folder, and false deletes them. */
  async remove(id: string, keepData: boolean) {
    const response = await this.delete<ProviderHubJobRef>(
      `/installations/${id}`,
      undefined,
      // eslint-disable-next-line camelcase -- transport name
      { keep_data: keepData },
    );
    return response.data;
  }

  log(id: string) {
    return this.get<PluginLog>(`/installations/${id}/log`);
  }

  async dismissReview(id: string) {
    await this.postRaw(`/installations/${id}/review/dismiss`);
  }
}

const pluginsApi = new PluginsApi();
export default pluginsApi;
