import BaseApi from "./base";

export type ProviderHubManifest = LooseObject;

export interface ProviderHubCatalogSource {
  id?: string;
  name: string;
  type?: string;
  url: string;
  enabled?: boolean;
  trusted: boolean;
  last_checked_at?: string | null;
  last_error?: string | null;
  resolved_commit?: string | null;
  dev_ref?: string | null;
}

export interface ProviderHubCatalogEntry {
  source?: string;
  source_name?: string;
  provider_id: string;
  name?: string;
  version: string;
  trusted: boolean;
  manifest?: ProviderHubManifest | string | null;
  manifest_json?: ProviderHubManifest | string | null;
  resolved_commit?: string | null;
}

export interface ProviderHubCatalog {
  sources: ProviderHubCatalogSource[];
  entries: ProviderHubCatalogEntry[];
}

export interface ProviderHubInstallation {
  provider_id: string;
  name?: string;
  active_version?: string | null;
  staged_version?: string | null;
  state: string;
  pending_restart?: boolean;
  trusted?: boolean;
  active_path?: string | null;
  staged_path?: string | null;
  python_path?: string | null;
  staged_python_path?: string | null;
  last_error?: string | null;
  installed_at?: string | null;
  activated_at?: string | null;
  manifest?: ProviderHubManifest;
}

export interface ProviderHubInstallRequest {
  manifest: ProviderHubManifest;
}

export interface ProviderHubJob {
  id?: string;
  action?: string;
  state?: string;
  message?: string | null;
  target_kind?: "provider" | "source" | "system" | null;
  target_id?: string | null;
  target_name?: string | null;
  source_id?: string | null;
  source_name?: string | null;
  from_version?: string | null;
  to_version?: string | null;
  error?: string | null;
  details?: Record<string, unknown> | null;
  duration_ms?: number | null;
  created_at?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  updated_at?: string | null;
}

export interface ProviderHubTestResult {
  provider_id: string;
  ok: boolean;
  status: string;
  message: string;
  details?: LooseObject;
}

class ProviderHubApi extends BaseApi {
  constructor() {
    super("/provider-hub");
  }

  catalog() {
    return this.get<ProviderHubCatalog>("/catalog");
  }

  async refreshCatalog() {
    await this.postRaw("/catalog/refresh");
  }

  async addCatalogSource(name: string, url: string) {
    const response = await this.postRaw<LooseObject>("/catalog/sources", {
      name,
      url,
    });
    return response.data;
  }

  async removeCatalogSource(name: string) {
    await this.delete(`/catalog/sources/${name}`);
  }

  async patchCatalogSource(name: string, payload: { dev_ref?: string | null }) {
    const response = await this.patchRaw<ProviderHubCatalogSource>(
      `/catalog/sources/${name}`,
      payload,
    );
    return response.data;
  }

  async providers() {
    const response =
      await this.get<DataWrapper<ProviderHubInstallation[]>>("/providers");
    return response.data;
  }

  async install(manifest: ProviderHubManifest) {
    const response = await this.postRaw<ProviderHubInstallation>(
      "/installations",
      { manifest },
    );
    return response.data;
  }

  async uninstall(providerId: string) {
    await this.delete(`/installations/${providerId}`);
  }

  async test(providerId: string) {
    const response = await this.postRaw<ProviderHubTestResult>(
      `/providers/${providerId}/test`,
    );
    return response.data;
  }

  async checkUpdates() {
    const response = await this.postRaw<LooseObject>("/updates/check");
    return response.data;
  }

  async applyUpdate(providerId: string) {
    const params: LooseObject = {};
    params["provider_id"] = providerId;
    const response = await this.postRaw<ProviderHubInstallation>(
      "/updates/apply",
      undefined,
      params,
    );
    return response.data;
  }

  async jobs() {
    const response = await this.get<DataWrapper<ProviderHubJob[]>>("/jobs");
    return response.data;
  }
}

const providerHubApi = new ProviderHubApi();
export default providerHubApi;
