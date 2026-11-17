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

export interface PluginInstallation extends ProviderHubInstallation {
  /** Absent on a row written before kinds existed, which is a subtitle provider. */
  kind?: string;
  enabled?: boolean;
  development_source?: boolean;
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
}

const pluginsApi = new PluginsApi();
export default pluginsApi;
